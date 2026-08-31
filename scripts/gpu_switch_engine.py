#!/usr/bin/env python3
"""
GPU Selecta Engine
Global default-renderer selection, live telemetry, and per-app GPU pinning
for multi-GPU Linux systems via Mesa or NVIDIA PRIME render offload.

This is NOT a hardware GPU mux — on a muxless Optimus-style laptop the
internal panel has no display path from the discrete GPU, so it never
changes owners. An external output wired to the discrete GPU (common for
HDMI/DP on gaming laptops) is a separate matter: the discrete GPU still
owns that connector's scanout regardless of any selection here, even though
the frame is rendered elsewhere and handed to it via dma-buf. What this
controls is only which GPU newly-launched apps render (and decode) on:

- Global toggle: writes an Omarchy Hyprland "toggle" file
  (~/.local/state/omarchy/toggles/hypr/) that sets PRIME offload env vars via
  hl.env(). Omarchy's toggle loader re-applies hl.env() on every
  `hyprctl reload`, so this takes effect live for new app launches — no
  Hyprland restart or logout required. It cannot affect already-running
  processes.
- Per-app pinning: rewrites a .desktop launcher entry in
  ~/.local/share/applications/ (the standard XDG override mechanism — this is
  exactly how GNOME's own "Launch using Discrete Graphics Card" works), so
  each Exec= line points at a small generated wrapper script that sets (or
  unsets) the PRIME offload env vars and then execs the real binary. A
  wrapper is used instead of prefixing Exec= directly with `env VAR=val ...`
  because some launchers (e.g. Omarchy's own browser-open shortcut) only
  look at the first whitespace-delimited token of Exec= to find the real
  executable — with an `env ...` prefix that token is literally "env",
  which such launchers then run with no command, not the app. Routing
  through a wrapper keeps the first token a real, directly-executable
  path. TryExec= is never touched, since the desktop spec requires it to
  stay a bare executable path for existence checks.

The default (no-arg) run only does cheap telemetry reads, safe to poll every
few seconds. The full installed-app catalog (--list-apps) walks every
.desktop file on the system and is meant to be fetched on demand (e.g. when
the Apps tab is opened), not on every poll tick.
"""

import sys
import os
import re
import json
import shlex
import subprocess
import tomllib
from pathlib import Path

HOME = Path.home()

RENDER_TOGGLE_DIR = HOME / ".local" / "state" / "omarchy" / "toggles" / "hypr"
RENDER_TOGGLE_FILE = RENDER_TOGGLE_DIR / "gpu-switch-render-default.lua"
RENDER_TARGET_MARKER = "-- gpu-selecta-target: "

USER_APPLICATIONS_DIR = HOME / ".local" / "share" / "applications"
SYSTEM_APPLICATIONS_DIR = Path("/usr/share/applications")
APP_GPU_STATE_FILE = HOME / ".config" / "omarchy" / "gpu-switch" / "apps.json"
APP_GPU_BACKUP_DIR = HOME / ".config" / "omarchy" / "gpu-switch" / "backups"
APP_GPU_WRAPPER_DIR = HOME / ".config" / "omarchy" / "gpu-switch" / "wrappers"
APP_GPU_MARKER = "# Written by the GPU Switch plugin — per-app GPU override\n"
TELEMETRY_EXTREMA_FILE = (
    HOME / ".local" / "state" / "omarchy" / "gpu-selecta" / "telemetry-extrema.json"
)
THEME_COLORS_FILE = HOME / ".local" / "state" / "omarchy" / "current" / "theme" / "colors.toml"

# Standard XDG desktop-entry field codes — kept in place (appended after the
# wrapper path) so launchers that do proper Exec= parsing still see them and
# substitute file/url args in.
FIELD_CODES = {"%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%i", "%c", "%k", "%v", "%m"}

# Omarchy's default Hyprland config (default/hypr/nvidia.lua) sets
# LIBVA_DRIVER_NAME=nvidia session-wide whenever an NVIDIA GPU is present,
# regardless of which GPU actually renders the desktop — it assumes the
# NVIDIA GPU is the render target. On a hybrid laptop where AMD/Intel is the
# selected render GPU, that leftover var still forces VA-API video decode
# onto NVIDIA. Chromium then decodes on NVIDIA but composites on the other
# GPU, and importing the decoded frame across GPUs as a shared dma-buf is
# what intermittently fails (EGL_BAD_MATCH, vaEndPicture errors, black video
# with a wedged GPU process). Pinning LIBVA_DRIVER_NAME to match the selected
# Mesa GPU keeps decode and composite on the same device.
LIBVA_DRIVER_BY_KERNEL_DRIVER = {
    "amdgpu": "radeonsi",
    "radeon": "radeonsi",
    "i915": "iHD",
    "xe": "iHD",
}


def read_sysfs(path):
    try:
        p = Path(path)
        if p.is_file():
            return p.read_text().strip()
    except Exception:
        pass
    return None


def read_theme_bar_colors():
    """Read bar colors from the active Omarchy theme without adding dependencies."""
    try:
        with THEME_COLORS_FILE.open("rb") as colors_file:
            colors = tomllib.load(colors_file)
    except (OSError, tomllib.TOMLDecodeError):
        return {}

    palette = {}
    for output_name, theme_name in {
        "green": "green",
        "yellow": "yellow",
        "cyan": "cyan",
        "urgent": "red",
    }.items():
        value = colors.get(theme_name)
        if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value):
            palette[output_name] = value
    return palette


def _is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def _fdinfo_bytes(value):
    """Parse a DRM fdinfo memory value such as ``164136 KiB``."""
    match = re.match(r"^([0-9]+)\s*(B|KiB|MiB|GiB)?$", value.strip())
    if not match:
        return 0
    amount = int(match.group(1))
    multiplier = {None: 1, "B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    return amount * multiplier[match.group(2)]


def _process_name(pid, fallback=""):
    try:
        name = (Path("/proc") / str(pid) / "comm").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    return fallback or f"PID {pid}"


def _is_system_gpu_process(name):
    return name.lower() in {
        "hyprland", "xwayland", "quickshell", "omarchy-shell",
        "kwin_wayland", "gnome-shell", "weston",
    }


def scan_drm_processes():
    """Return cumulative per-process DRM counters grouped by PCI address.

    Multiple file descriptors can expose the same DRM client, so client IDs
    are deduplicated before their engine and memory counters are summed.
    Counter deltas are converted to percentages by the long-running QML panel.
    """
    by_gpu = {}
    seen_clients = set()

    for proc_dir in Path("/proc").glob("[0-9]*"):
        pid = int(proc_dir.name)
        fdinfo_dir = proc_dir / "fdinfo"
        try:
            fdinfo_files = list(fdinfo_dir.iterdir())
        except OSError:
            continue

        for fdinfo_path in fdinfo_files:
            try:
                fields = {}
                for line in fdinfo_path.read_text().splitlines():
                    if ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    if key.startswith("drm-"):
                        fields[key] = value.strip()
            except OSError:
                continue

            pci_address = fields.get("drm-pdev")
            if not pci_address:
                continue
            client_id = fields.get("drm-client-id", fdinfo_path.name)
            client_key = (pid, pci_address, client_id)
            if client_key in seen_clients:
                continue
            seen_clients.add(client_key)

            gpu_processes = by_gpu.setdefault(pci_address, {})
            process = gpu_processes.setdefault(pid, {
                "pid": pid,
                "name": _process_name(pid),
                "system": False,
                "source": "drm",
                "engineGfxNs": 0,
                "engineComputeNs": 0,
                "vramMb": 0.0,
                "gttMb": 0.0,
            })
            process["system"] = _is_system_gpu_process(process["name"])
            process["engineGfxNs"] += int(fields.get("drm-engine-gfx", "0").split()[0])
            process["engineComputeNs"] += int(fields.get("drm-engine-compute", "0").split()[0])
            process["vramMb"] += _fdinfo_bytes(fields.get("drm-memory-vram", "0")) / (1024 * 1024)
            process["gttMb"] += _fdinfo_bytes(fields.get("drm-memory-gtt", "0")) / (1024 * 1024)

    result = {}
    for pci_address, processes in by_gpu.items():
        rows = list(processes.values())
        for process in rows:
            process["vramMb"] = round(process["vramMb"], 1)
            process["gttMb"] = round(process["gttMb"], 1)
        result[pci_address] = rows
    return result


def scan_nvidia_processes():
    """Read NVIDIA's instantaneous per-process utilization when available."""
    by_index = {}
    try:
        res = subprocess.run(
            ["nvidia-smi", "pmon", "-c", "1", "-s", "um"],
            capture_output=True, text=True, timeout=2.0,
        )
        if res.returncode != 0:
            return by_index
        for line in res.stdout.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split(None, 11)
            if len(parts) < 12 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            gpu_index = int(parts[0])
            pid = int(parts[1])
            process_type = parts[2]

            def percent(value):
                return int(value) if value.isdigit() else None

            sm_percent = percent(parts[3])
            row = {
                "pid": pid,
                "name": _process_name(pid, parts[11]),
                "system": _is_system_gpu_process(_process_name(pid, parts[11])),
                "source": "nvidia",
                "processType": process_type,
                "gfxPercent": sm_percent if "G" in process_type else None,
                "computePercent": sm_percent if "C" in process_type else None,
                "memoryPercent": percent(parts[4]),
                "encoderPercent": percent(parts[5]),
                "decoderPercent": percent(parts[6]),
                "vramMb": float(parts[9]) if _is_number(parts[9]) else None,
            }
            by_index.setdefault(gpu_index, []).append(row)
    except (OSError, subprocess.SubprocessError):
        pass
    return by_index


def friendly_gpu_name(vendor, model, index):
    """Return a compact, user-facing identity without relying on card order."""
    model = (model or "").strip()
    bracketed = re.search(r"\[([^\]]+)\]", model)
    if bracketed:
        marketing_name = bracketed.group(1).split("/")[0].strip()
        if marketing_name:
            model = marketing_name
    model = re.sub(r"\s*\(rev [^)]+\)\s*$", "", model, flags=re.IGNORECASE).strip()
    model = re.sub(rf"^{re.escape(vendor)}\s+", "", model, flags=re.IGNORECASE).strip()
    if model and not model.lower().startswith("graphics (unknown"):
        return f"{vendor} {model}" if vendor != "Generic" else model
    return f"GPU {index + 1}"


def compact_gpu_name(display_name, vendor):
    """Short selector label; full display_name remains available elsewhere."""
    name = re.sub(rf"^{re.escape(vendor)}\s+", "", display_name, flags=re.IGNORECASE)
    name = re.sub(r"\b(?:GeForce|Radeon)\b\s*", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\b(?:Laptop GPU|Mobile|Series|Graphics|Max-Q)\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip(" -/")
    if vendor and vendor != "Generic" and name:
        return f"{vendor} {name}"
    return name or vendor or display_name


def add_gpu_identity(gpus):
    """Add display names, optional roles, and safe routing keys.

    The boot VGA is the default path. Mesa GPUs use PCI-addressed DRI_PRIME;
    one proprietary NVIDIA GPU uses NVIDIA PRIME render offload variables.
    """
    for index, gpu in enumerate(gpus):
        gpu["displayName"] = friendly_gpu_name(gpu.get("vendor", "Generic"), gpu.get("model"), index)
        gpu["shortName"] = compact_gpu_name(gpu["displayName"], gpu.get("vendor", "Generic"))
        gpu["role"] = None
        gpu["routeKey"] = None

    if not gpus:
        return gpus

    primary_candidates = [gpu for gpu in gpus if gpu.get("bootVga")]
    primary = primary_candidates[0] if len(primary_candidates) == 1 else gpus[0]
    primary["role"] = "Integrated" if len(gpus) > 1 and primary.get("vendor") in ("AMD", "Intel") else "Primary"
    primary["routeKey"] = "amd"  # Legacy key retained: means system/default GPU.

    proprietary_nvidia = [gpu for gpu in gpus if gpu.get("driver") == "nvidia"]
    mesa_drivers = {"amdgpu", "radeon", "i915", "xe", "nouveau"}
    for gpu in gpus:
        if gpu is primary:
            continue
        gpu["role"] = "Discrete"
        if gpu.get("driver") == "nvidia" and len(proprietary_nvidia) == 1:
            gpu["routeKey"] = "nvidia"  # Legacy key retained for saved overrides.
        elif gpu.get("driver") in mesa_drivers and gpu.get("driPrime"):
            gpu["routeKey"] = f"dri:{gpu['driPrime']}"
    return gpus


def load_telemetry_history():
    if not TELEMETRY_EXTREMA_FILE.exists():
        return {}
    try:
        saved = json.loads(TELEMETRY_EXTREMA_FILE.read_text())
        return saved if isinstance(saved, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def update_telemetry_history(gpus):
    """Persist the highest observed power draw and attach it to each GPU.

    PCI addresses survive card-number reordering. Synthetic GPU entries fall
    back to a vendor/model identity when no PCI address is available.
    """
    history = load_telemetry_history()
    changed = False

    for gpu in gpus:
        key = gpu.get("pciAddress") or f"{gpu.get('vendor', 'gpu')}:{gpu.get('model', gpu.get('id', 'unknown'))}"
        record = history.get(key)
        if not isinstance(record, dict):
            record = {}
            history[key] = record
            changed = True

        value = gpu.get("powerWatts")
        if not isinstance(value, (int, float)):
            gpu["powerObservedMaxWatts"] = None
            continue

        # Migrate the min/max structure written by the short-lived initial
        # implementation without throwing away its learned peak.
        stored_max = record.get("powerObservedMaxWatts")
        legacy_bounds = record.get("powerWatts")
        if not isinstance(stored_max, (int, float)) and isinstance(legacy_bounds, dict):
            stored_max = legacy_bounds.get("max")
        if not isinstance(stored_max, (int, float)):
            stored_max = value

        observed_max = max(stored_max, value)
        if record.get("powerObservedMaxWatts") != observed_max or "powerWatts" in record or "tempC" in record:
            record["powerObservedMaxWatts"] = observed_max
            record.pop("powerWatts", None)
            record.pop("tempC", None)
            changed = True
        gpu["powerObservedMaxWatts"] = observed_max

    if changed:
        try:
            TELEMETRY_EXTREMA_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = TELEMETRY_EXTREMA_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(history, indent=2))
            os.replace(tmp, TELEMETRY_EXTREMA_FILE)
        except OSError:
            pass

    return gpus


def scan_gpu_telemetry():
    """Cheap per-GPU telemetry: vendor, temp, VRAM, busy%, power. Safe to
    poll frequently — only reads a handful of small sysfs files per card."""
    gpus = []
    drm_cards = sorted(Path("/sys/class/drm").glob("card[0-9]*"))
    actual_cards = [c for c in drm_cards if "-" not in c.name]

    for card in actual_cards:
        dev_path = card / "device"
        if not dev_path.exists():
            continue

        vendor_id = read_sysfs(dev_path / "vendor") or ""
        device_id = read_sysfs(dev_path / "device") or "Unknown"
        pci_addr = dev_path.resolve().name
        driver_link = dev_path / "driver"
        driver = driver_link.resolve().name if driver_link.exists() else "unknown"
        boot_vga = read_sysfs(dev_path / "boot_vga") == "1"

        vendor = "Generic"
        if "0x1002" in vendor_id.lower():
            vendor = "AMD"
        elif "0x10de" in vendor_id.lower():
            vendor = "NVIDIA"
        elif "0x8086" in vendor_id.lower():
            vendor = "Intel"

        model = f"Graphics ({device_id})"
        try:
            # A runtime-suspended GPU (common for the idle discrete GPU on a
            # hybrid laptop) takes a moment to wake when its PCI config space
            # is read — give lspci real headroom rather than a tight timeout
            # that silently falls back to the generic "Graphics (0x...)" label.
            res = subprocess.run(["lspci", "-s", pci_addr], capture_output=True, text=True, timeout=3.0)
            for line in res.stdout.splitlines():
                if "VGA" in line or "3D" in line or "Display" in line:
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        # Vendor is already shown as its own label in the UI —
                        # strip it here instead of duplicating it in the model string.
                        clean = re.sub(r'\[AMD/ATI\]|Advanced Micro Devices, Inc\.|NVIDIA Corporation|Intel Corporation', '', parts[2].strip()).strip()
                        model = clean
                        break
        except Exception:
            pass

        vram_used = int(read_sysfs(dev_path / "mem_info_vram_used") or 0)
        vram_total = int(read_sysfs(dev_path / "mem_info_vram_total") or 0)
        vram_used_mb = round(vram_used / (1024 * 1024), 1)
        vram_total_mb = round(vram_total / (1024 * 1024), 1)
        vram_percent = round((vram_used_mb / vram_total_mb) * 100, 1) if vram_total_mb > 0 else None

        busy = read_sysfs(dev_path / "gpu_busy_percent")
        busy_percent = int(busy) if busy and busy.isdigit() else None

        temp_c = None
        temp_critical_c = None
        power_w = None
        power_limit_w = None
        fan_mode = None
        supports_fan_control = False
        hwmon_dirs = list(dev_path.glob("hwmon/hwmon*"))
        if hwmon_dirs:
            hdir = hwmon_dirs[0]
            t = read_sysfs(hdir / "temp1_input")
            if t and t.isdigit():
                temp_c = round(int(t) / 1000, 1)
            critical = read_sysfs(hdir / "temp1_crit") or read_sysfs(hdir / "temp1_max")
            if critical and critical.isdigit():
                temp_critical_c = round(int(critical) / 1000, 1)
            p = read_sysfs(hdir / "power1_average") or read_sysfs(hdir / "power1_input")
            if p and p.isdigit():
                power_w = round(int(p) / 1000000.0, 1)
            cap = read_sysfs(hdir / "power1_cap")
            if cap and cap.isdigit():
                power_limit_w = round(int(cap) / 1000000.0, 1)
            # A hwmon directory existing doesn't mean fan control exists —
            # iGPUs/APUs publish temp/power sensors but no pwm1 node, since
            # the fan is wired to the system EC rather than the GPU.
            supports_fan_control = (hdir / "pwm1").exists() and (hdir / "pwm1_enable").exists()
            if supports_fan_control:
                pwm_enable = read_sysfs(hdir / "pwm1_enable")
                fan_mode = "manual" if pwm_enable == "1" else "auto"

        # DPM power governors are an amdgpu-only sysfs interface — NVIDIA and
        # Intel drivers don't expose power_dpm_force_performance_level.
        performance_level = read_sysfs(dev_path / "power_dpm_force_performance_level")
        supports_tuning = performance_level is not None
        connected_outputs = []
        for connector in Path("/sys/class/drm").glob(f"{card.name}-*"):
            if read_sysfs(connector / "status") == "connected":
                connected_outputs.append(connector.name[len(card.name) + 1:])

        gpus.append({
            "id": card.name,
            "vendor": vendor,
            "model": model,
            "driver": driver,
            "bootVga": boot_vga,
            "pciAddress": pci_addr,
            "driPrime": "pci-" + re.sub(r"[:.]", "_", pci_addr),
            "tempC": temp_c,
            "tempCriticalC": temp_critical_c,
            "vramUsedMb": vram_used_mb if vram_total_mb > 0 else None,
            "vramTotalMb": vram_total_mb if vram_total_mb > 0 else None,
            "vramPercent": vram_percent,
            "busyPercent": busy_percent,
            "powerWatts": power_w,
            "powerLimitWatts": power_limit_w,
            "supportsTuning": supports_tuning,
            "performanceLevel": performance_level,
            "supportsFanControl": supports_fan_control,
            "fanMode": fan_mode,
            "connectedOutputs": sorted(connected_outputs),
            "processes": [],
        })

    # The NVIDIA proprietary driver doesn't populate the amdgpu-style sysfs
    # telemetry nodes above (mem_info_vram_*, gpu_busy_percent, hwmon
    # temp/power) even though the card itself shows up fine in /sys/class/drm
    # — so any NVIDIA entry from the loop above has real id/vendor/model but
    # null telemetry. Fill those in from nvidia-smi, matched by order; if no
    # NVIDIA card was found via sysfs at all, append a synthetic one instead.
    nvidia_rows = []
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=gpu_name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw,power.limit,power.default_limit,temperature.gpu.tlimit",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1.0)
        if res.returncode == 0:
            for line in res.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 5:
                    nvidia_rows.append(parts)
    except Exception:
        pass

    nvidia_entries = [g for g in gpus if g["vendor"] == "NVIDIA"]
    for i, parts in enumerate(nvidia_rows):
        vram_used = float(parts[1])
        vram_total = float(parts[2])
        current_power_limit = float(parts[6]) if len(parts) > 6 and _is_number(parts[6]) else None
        default_power_limit = float(parts[7]) if len(parts) > 7 and _is_number(parts[7]) else None
        telemetry = {
            "tempC": float(parts[4]),
            "tempCriticalC": float(parts[8]) if len(parts) > 8 and _is_number(parts[8]) else None,
            "vramUsedMb": vram_used,
            "vramTotalMb": vram_total,
            "vramPercent": round((vram_used / vram_total) * 100, 1) if vram_total > 0 else None,
            "busyPercent": int(parts[3]) if parts[3].isdigit() else None,
            "powerWatts": float(parts[5]) if len(parts) > 5 and _is_number(parts[5]) else None,
            "powerLimitWatts": current_power_limit if current_power_limit is not None else default_power_limit,
        }
        if i < len(nvidia_entries):
            nvidia_entries[i].update(telemetry)
        else:
            model_name = re.sub(r'^NVIDIA\s+', '', parts[0]).strip()
            gpus.append({
                "id": f"nvidia{i}", "vendor": "NVIDIA", "model": model_name, "driver": "nvidia",
                "bootVga": False,
                "pciAddress": None, "driPrime": None,
                "supportsTuning": False, "performanceLevel": None,
                "supportsFanControl": False, "fanMode": None,
                "connectedOutputs": [],
                "processes": [],
                **telemetry,
            })

    drm_processes = scan_drm_processes()
    for gpu in gpus:
        gpu["processes"] = drm_processes.get(gpu.get("pciAddress"), [])

    nvidia_processes = scan_nvidia_processes()
    for index, gpu in enumerate([item for item in gpus if item["vendor"] == "NVIDIA"]):
        if index in nvidia_processes:
            gpu["processes"] = nvidia_processes[index]

    return update_telemetry_history(add_gpu_identity(gpus))


def get_render_default():
    if not RENDER_TOGGLE_FILE.exists():
        return "amd"
    try:
        for line in RENDER_TOGGLE_FILE.read_text().splitlines():
            if line.startswith(RENDER_TARGET_MARKER):
                return line[len(RENDER_TARGET_MARKER):].strip()
    except OSError:
        pass
    return "nvidia"  # Compatibility with files written before target markers.


def is_route_target(target):
    return target in ("amd", "nvidia") or bool(re.fullmatch(r"dri:pci-[0-9a-fA-F_]+", target))


def is_drm_card_id(card_id):
    """Accept only kernel DRM card names, never paths or shell syntax."""
    return isinstance(card_id, str) and bool(re.fullmatch(r"card[0-9]+", card_id))


def render_toggle_content(target, gpus=()):
    values = env_values_for_gpu(target, gpus)
    lines = [
        "-- Written by the GPU Selecta plugin. Delete this file to fall back to Omarchy's raw session defaults.",
        f"{RENDER_TARGET_MARKER}{target}",
    ]
    for name, value in values.items():
        lines.append(f'hl.env("{name}", "{value}")')
    return "\n".join(lines) + "\n"


def set_render_default(target):
    gpus = scan_gpu_telemetry()
    available = {gpu["routeKey"] for gpu in gpus if gpu.get("routeKey")}
    if not is_route_target(target) or target not in available:
        return {"status": "error", "message": f"Invalid render default: {target}"}

    # Always write an explicit toggle, including for "amd" — Omarchy's own
    # Hyprland default (default/hypr/nvidia.lua) unconditionally sets
    # LIBVA_DRIVER_NAME=nvidia (and __GLX_VENDOR_LIBRARY_NAME=nvidia)
    # session-wide whenever an NVIDIA GPU is present. That default loads
    # before this plugin's toggle on every `hyprctl reload`, so previously
    # deleting the toggle for "amd" left those NVIDIA-biased vars in effect
    # for every AMD-routed process instead of clearing them.
    values = env_values_for_gpu(target, gpus)
    try:
        RENDER_TOGGLE_DIR.mkdir(parents=True, exist_ok=True)
        RENDER_TOGGLE_FILE.write_text(render_toggle_content(target, gpus))
    except OSError as e:
        return {"status": "error", "message": str(e)}

    try:
        subprocess.run(["hyprctl", "reload"], check=True, capture_output=True, text=True, timeout=3.0)
    except Exception as e:
        return {
            "status": "error",
            "message": f"Wrote render default but 'hyprctl reload' failed: {e}",
        }

    # `hyprctl reload` only updates Hyprland's own process environment (what
    # hl.env() sets). Apps launched normally don't fork directly from
    # Hyprland — Hyprland wraps them in a systemd --user scope
    # (app-Hyprland-*.scope), which gets its environment from the systemd
    # --user manager's environment block instead. That block is populated
    # once at login by Omarchy's autostart (systemctl --user
    # import-environment + dbus-update-activation-environment) and is never
    # refreshed on reload, so without this step every scope-launched app
    # keeps using the stale pre-toggle vars even though the toggle file and
    # Hyprland's own env are already correct. Run the same re-import
    # Omarchy's autostart does, but as a child of Hyprland (via hl.exec_cmd)
    # so it inherits the environment hl.env() just set, not this script's.
    var_names = " ".join(values.keys())
    propagate_cmd = (
        f"systemctl --user import-environment {var_names}; "
        f"dbus-update-activation-environment --systemd {var_names}"
    )
    try:
        subprocess.run(
            ["hyprctl", "eval", f'hl.exec_cmd("{propagate_cmd}")'],
            check=True, capture_output=True, text=True, timeout=3.0,
        )
    except Exception as e:
        return {
            "status": "error",
            "message": f"Wrote render default but propagating env to systemd failed: {e}",
        }

    return {"status": "success", "renderDefault": target}


def get_driver(card_id):
    driver_link = Path(f"/sys/class/drm/{card_id}/device/driver")
    return driver_link.resolve().name if driver_link.exists() else None


def set_performance_level(card_id, level):
    if not is_drm_card_id(card_id):
        return {"status": "error", "message": f"Invalid DRM card ID: {card_id}"}

    allowed_levels = {"auto", "low", "high", "profile_peak"}
    if level not in allowed_levels:
        return {"status": "error", "message": f"Invalid power governor: {level}"}

    driver = get_driver(card_id)
    dev_path = Path(f"/sys/class/drm/{card_id}/device/power_dpm_force_performance_level")

    if dev_path.exists():
        try:
            dev_path.write_text(level)
            return {"status": "success", "level": level, "method": "direct"}
        except (PermissionError, OSError):
            cmd = f"printf %s {shlex.quote(level)} > {shlex.quote(str(dev_path))}"
            try:
                subprocess.run(["pkexec", "sh", "-c", cmd], check=True, capture_output=True, text=True)
                return {"status": "success", "level": level, "method": "pkexec"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

    if driver == "nvidia":
        try:
            if level == "high":
                subprocess.run(["nvidia-smi", "-pm", "1"], check=True)
                return {"status": "success", "level": level, "method": "nvidia-smi"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        return {
            "status": "error",
            "message": "Not supported: NVIDIA has no DPM governors over sysfs (only persistence mode).",
        }

    return {
        "status": "error",
        "message": f"Not supported: DPM governors are amdgpu-only (driver: {driver or 'unknown'})",
    }


def set_fan_pwm(card_id, pwm_val):
    if not is_drm_card_id(card_id):
        return {"status": "error", "message": f"Invalid DRM card ID: {card_id}"}

    hwmon_dirs = list(Path(f"/sys/class/drm/{card_id}/device/hwmon").glob("hwmon*"))
    if hwmon_dirs:
        hdir = hwmon_dirs[0]
        pwm_file = hdir / "pwm1"
        pwm_enable_file = hdir / "pwm1_enable"

        if pwm_file.exists() and pwm_enable_file.exists() and pwm_val == "auto":
            try:
                pwm_enable_file.write_text("2")
                return {"status": "success", "mode": "auto", "method": "direct"}
            except (PermissionError, OSError):
                cmd = f"printf %s 2 > {shlex.quote(str(pwm_enable_file))}"
                try:
                    subprocess.run(["pkexec", "sh", "-c", cmd], check=True, capture_output=True, text=True)
                    return {"status": "success", "mode": "auto", "method": "pkexec"}
                except Exception as e:
                    return {"status": "error", "message": str(e)}
        elif pwm_file.exists() and pwm_enable_file.exists():
            try:
                pwm_num = max(0, min(255, int(pwm_val)))
            except ValueError:
                return {"status": "error", "message": f"Invalid PWM value: {pwm_val}"}
            try:
                pwm_enable_file.write_text("1")
                pwm_file.write_text(str(pwm_num))
                return {"status": "success", "pwm": pwm_num, "method": "direct"}
            except (PermissionError, OSError):
                cmd = (
                    f"printf %s 1 > {shlex.quote(str(pwm_enable_file))} && "
                    f"printf %s {shlex.quote(str(pwm_num))} > {shlex.quote(str(pwm_file))}"
                )
                try:
                    subprocess.run(["pkexec", "sh", "-c", cmd], check=True, capture_output=True, text=True)
                    return {"status": "success", "pwm": pwm_num, "method": "pkexec"}
                except Exception as e:
                    return {"status": "error", "message": str(e)}

    driver = get_driver(card_id)
    if driver == "nvidia":
        return {
            "status": "error",
            "message": "Not supported: NVIDIA laptop GPUs have no writable fan PWM node (EC-controlled).",
        }

    return {
        "status": "error",
        "message": f"Not supported: no fan PWM node exposed (driver: {driver or 'unknown'})",
    }


def load_saved_overrides():
    if not APP_GPU_STATE_FILE.exists():
        return {}
    try:
        saved = json.loads(APP_GPU_STATE_FILE.read_text())
        return saved if isinstance(saved, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_app_gpu_state(state):
    APP_GPU_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = APP_GPU_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, APP_GPU_STATE_FILE)


def parse_desktop_entry(path):
    """Return {"name": ...} for a launchable app entry, or None to skip it
    (not an Application, hidden, or has no Exec)."""
    try:
        text = path.read_text()
    except OSError:
        return None

    in_main_section = False
    name = None
    entry_type = None
    no_display = False
    hidden = False
    has_exec = False

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_main_section = (s == "[Desktop Entry]")
            continue
        if not in_main_section or not s or s.startswith("#"):
            continue
        if s.startswith("Name=") and name is None:
            name = s[len("Name="):]
        elif s.startswith("Type="):
            entry_type = s[len("Type="):]
        elif s.startswith("NoDisplay="):
            no_display = s[len("NoDisplay="):].strip().lower() == "true"
        elif s.startswith("Hidden="):
            hidden = s[len("Hidden="):].strip().lower() == "true"
        elif s.startswith("Exec="):
            has_exec = bool(s[len("Exec="):].strip())

    if not name or entry_type != "Application" or no_display or hidden or not has_exec:
        return None
    return {"name": name}


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "app"


def discover_all_apps():
    """Full app catalog: every launchable .desktop entry on the system, one
    row each. Nothing is pre-selected — every app starts at "auto" (follow
    the global default) unless it's been explicitly pinned before. This is
    the expensive call — walks the filesystem — meant for on-demand use."""
    apps = {}
    seen_basenames = set()
    used_keys = set()
    # User dir first so a user override's Name= wins if both exist.
    for d in (USER_APPLICATIONS_DIR, SYSTEM_APPLICATIONS_DIR):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.desktop")):
            if f.name in seen_basenames:
                continue
            seen_basenames.add(f.name)
            parsed = parse_desktop_entry(f)
            if parsed is None:
                continue
            base_key = slugify(f.stem)
            key = base_key
            n = 2
            while key in used_keys:
                key = f"{base_key}-{n}"
                n += 1
            used_keys.add(key)
            apps[key] = {"label": parsed["name"], "gpu": "auto", "desktopFiles": [f.name]}

    overrides = load_saved_overrides()
    for key, saved in overrides.items():
        if not isinstance(saved, dict):
            continue
        if key in apps:
            if "gpu" in saved:
                apps[key]["gpu"] = saved["gpu"]
        elif "desktopFiles" in saved:
            # A custom entry for an app that's since been uninstalled (or
            # was never auto-discoverable) — keep it visible/editable.
            apps[key] = {
                "label": saved.get("label", key),
                "gpu": saved.get("gpu", "auto"),
                "desktopFiles": saved["desktopFiles"],
            }

    return apps


def get_pristine_desktop_content(basename):
    """Return the never-touched-by-us content for a .desktop file, snapshotting
    it into our backup dir the first time we ever see it."""
    backup_path = APP_GPU_BACKUP_DIR / basename
    if backup_path.exists():
        return backup_path.read_text()

    user_path = USER_APPLICATIONS_DIR / basename
    source = None
    if user_path.exists():
        content = user_path.read_text()
        if not content.startswith(APP_GPU_MARKER):
            source = content
    if source is None:
        system_path = SYSTEM_APPLICATIONS_DIR / basename
        if system_path.exists():
            source = system_path.read_text()

    if source is None:
        return None

    APP_GPU_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(source)
    return source


def env_values_for_gpu(gpu, gpus=()):
    """Env vars for a render target. `gpus` (from scan_gpu_telemetry) is used
    to resolve the correct VA-API decoder driver for the target GPU — see
    LIBVA_DRIVER_BY_KERNEL_DRIVER above for why this must be pinned
    explicitly rather than left to Omarchy's NVIDIA-biased session default."""
    values = {
        "DRI_PRIME": "",
        "__NV_PRIME_RENDER_OFFLOAD": "",
        "__GLX_VENDOR_LIBRARY_NAME": "",
        "__VK_LAYER_NV_optimus": "",
        "LIBVA_DRIVER_NAME": "",
    }
    if gpu == "nvidia":
        values.update({
            "__NV_PRIME_RENDER_OFFLOAD": "1",
            "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
            "__VK_LAYER_NV_optimus": "NVIDIA_only",
            "LIBVA_DRIVER_NAME": "nvidia",
        })
    else:
        if gpu.startswith("dri:"):
            values["DRI_PRIME"] = gpu[len("dri:"):]
        target_gpu = next((g for g in gpus if g.get("routeKey") == gpu), None)
        libva_driver = LIBVA_DRIVER_BY_KERNEL_DRIVER.get((target_gpu or {}).get("driver"))
        if libva_driver:
            values["LIBVA_DRIVER_NAME"] = libva_driver
    return values


def env_lines_for_gpu(gpu, gpus=()):
    lines = []
    for name, value in env_values_for_gpu(gpu, gpus).items():
        lines.append(f"export {name}={shlex.quote(value)}" if value else f"unset {name}")
    return lines


def write_wrapper_script(path, env_lines, binary, static_args):
    cmd = " ".join([shlex.quote(binary)] + [shlex.quote(a) for a in static_args] + ['"$@"'])
    content = "\n".join([
        "#!/bin/sh",
        "# Written by the GPU Switch plugin — do not edit by hand",
        *env_lines,
        f"exec {cmd}",
        "",
    ])
    path.write_text(content)
    path.chmod(0o755)


def remove_wrappers_for_basename(basename):
    slug = slugify(Path(basename).stem)
    for f in APP_GPU_WRAPPER_DIR.glob(f"{slug}-*.sh"):
        try:
            f.unlink()
        except OSError:
            pass


def rewrite_exec_lines(content, basename, gpu, gpus=()):
    """Point every Exec= line at a generated wrapper script instead of
    prefixing it with `env VAR=val ...` directly — see the module docstring
    for why. field codes (%U etc.) stay in Exec= itself, after the wrapper
    path, so spec-compliant launchers still substitute args into them."""
    env_lines = env_lines_for_gpu(gpu, gpus)
    slug = slugify(Path(basename).stem)
    APP_GPU_WRAPPER_DIR.mkdir(parents=True, exist_ok=True)

    out_lines = []
    exec_index = 0
    for line in content.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped.startswith("Exec="):
            value = stripped[len("Exec="):]
            newline = "\n" if line.endswith("\n") else ""
            try:
                tokens = shlex.split(value, posix=True)
            except ValueError:
                tokens = value.split()

            if not tokens:
                out_lines.append(line)
                continue

            binary = tokens[0]
            rest = tokens[1:]
            static_args = [t for t in rest if t not in FIELD_CODES]
            field_codes = [t for t in rest if t in FIELD_CODES]

            wrapper_path = APP_GPU_WRAPPER_DIR / f"{slug}-{exec_index}.sh"
            write_wrapper_script(wrapper_path, env_lines, binary, static_args)
            exec_index += 1

            new_value = " ".join([shlex.quote(str(wrapper_path))] + field_codes)
            out_lines.append(f"Exec={new_value}{newline}")
        else:
            out_lines.append(line)
    return "".join(out_lines)


def apply_desktop_gpu(basename, gpu, gpus=()):
    system_path = SYSTEM_APPLICATIONS_DIR / basename
    user_path = USER_APPLICATIONS_DIR / basename

    if gpu == "auto":
        if not (APP_GPU_BACKUP_DIR / basename).exists() and not user_path.exists():
            return {"status": "error", "message": f"{basename} not found", "file": basename}
        if system_path.exists():
            try:
                if user_path.exists():
                    current = user_path.read_text()
                    if current.startswith(APP_GPU_MARKER):
                        user_path.unlink()
            except OSError as e:
                return {"status": "error", "message": str(e), "file": basename}
        else:
            pristine = get_pristine_desktop_content(basename)
            if pristine is None:
                return {"status": "error", "message": f"{basename} not found", "file": basename}
            try:
                user_path.write_text(pristine)
            except OSError as e:
                return {"status": "error", "message": str(e), "file": basename}
        remove_wrappers_for_basename(basename)
        return {"status": "success", "file": basename, "gpu": "auto"}

    if not is_route_target(gpu):
        return {"status": "error", "message": f"Invalid GPU target: {gpu}", "file": basename}

    pristine = get_pristine_desktop_content(basename)
    if pristine is None:
        return {"status": "error", "message": f"{basename} not found", "file": basename}

    rewritten = APP_GPU_MARKER + rewrite_exec_lines(pristine, basename, gpu, gpus)

    try:
        USER_APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
        user_path.write_text(rewritten)
    except OSError as e:
        return {"status": "error", "message": str(e), "file": basename}

    return {"status": "success", "file": basename, "gpu": gpu}


def set_app_gpu(app_key, gpu, label=None, desktop_files=None):
    """Apply a GPU choice to an app. If the app isn't already known (e.g. a
    fresh key from the auto-discovered catalog that's never been saved
    before), label/desktop_files must be supplied so we know what to write."""
    if gpu != "auto" and not is_route_target(gpu):
        return {"status": "error", "message": f"Invalid GPU target: {gpu}"}

    overrides = load_saved_overrides()
    if app_key in overrides and "desktopFiles" in overrides[app_key]:
        entry = dict(overrides[app_key])
    elif desktop_files:
        entry = {"label": label or app_key, "desktopFiles": desktop_files}
    else:
        return {"status": "error", "message": f"Unknown app: {app_key}"}

    gpus = scan_gpu_telemetry() if gpu != "auto" else ()
    results = [apply_desktop_gpu(f, gpu, gpus) for f in entry.get("desktopFiles", [])]
    failures = [r for r in results if r["status"] == "error"]

    entry["gpu"] = gpu
    overrides[app_key] = entry
    save_app_gpu_state(overrides)

    if failures:
        return {
            "status": "error",
            "message": "; ".join(f"{r['file']}: {r['message']}" for r in failures),
            "appKey": app_key,
        }
    return {"status": "success", "appKey": app_key, "gpu": gpu, "label": entry.get("label", app_key)}


def main():
    if len(sys.argv) > 1:
        action = sys.argv[1]
        if action == "--set-render-default" and len(sys.argv) > 2:
            print(json.dumps(set_render_default(sys.argv[2])))
            return
        elif action == "--set-app-gpu" and len(sys.argv) > 3:
            label = sys.argv[4] if len(sys.argv) > 4 else None
            desktop_files = sys.argv[5].split(",") if len(sys.argv) > 5 and sys.argv[5] else None
            print(json.dumps(set_app_gpu(sys.argv[2], sys.argv[3], label, desktop_files)))
            return
        elif action == "--list-apps":
            print(json.dumps({"apps": discover_all_apps()}, indent=2))
            return
        elif action == "--set-power-profile" and len(sys.argv) > 3:
            print(json.dumps(set_performance_level(sys.argv[2], sys.argv[3])))
            return
        elif action == "--set-fan" and len(sys.argv) > 3:
            print(json.dumps(set_fan_pwm(sys.argv[2], sys.argv[3])))
            return

    gpus = scan_gpu_telemetry()
    route_keys = {g["routeKey"] for g in gpus if g.get("routeKey")}
    is_hybrid = "amd" in route_keys and len(route_keys) > 1

    render_default = get_render_default()
    if render_default not in route_keys:
        render_default = "amd"

    output = {
        "gpus": gpus,
        "themeColors": read_theme_bar_colors(),
        "isHybrid": is_hybrid,
        "renderDefault": render_default,
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
