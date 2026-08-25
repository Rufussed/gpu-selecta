#!/usr/bin/env python3
"""
GPU Switch Engine
Global default-renderer toggle, live telemetry, and per-app GPU pinning for
hybrid AMD/NVIDIA (and Intel) laptops, via PRIME render offload.

This is NOT a hardware GPU mux — muxless Optimus-style laptops have no
display path from the discrete GPU, so the panel-owning GPU never changes.
What this does is control which GPU newly-launched apps render on:

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
from pathlib import Path

HOME = Path.home()

RENDER_TOGGLE_DIR = HOME / ".local" / "state" / "omarchy" / "toggles" / "hypr"
RENDER_TOGGLE_FILE = RENDER_TOGGLE_DIR / "gpu-switch-render-default.lua"
RENDER_TOGGLE_CONTENT = (
    '-- Written by the GPU Switch plugin. Delete this file (or toggle it off\n'
    '-- in the panel) to go back to the default GPU for new app launches.\n'
    'hl.env("__NV_PRIME_RENDER_OFFLOAD", "1")\n'
    'hl.env("__GLX_VENDOR_LIBRARY_NAME", "nvidia")\n'
    'hl.env("__VK_LAYER_NV_optimus", "NVIDIA_only")\n'
)

USER_APPLICATIONS_DIR = HOME / ".local" / "share" / "applications"
SYSTEM_APPLICATIONS_DIR = Path("/usr/share/applications")
APP_GPU_STATE_FILE = HOME / ".config" / "omarchy" / "gpu-switch" / "apps.json"
APP_GPU_BACKUP_DIR = HOME / ".config" / "omarchy" / "gpu-switch" / "backups"
APP_GPU_WRAPPER_DIR = HOME / ".config" / "omarchy" / "gpu-switch" / "wrappers"
APP_GPU_MARKER = "# Written by the GPU Switch plugin — per-app GPU override\n"

# Standard XDG desktop-entry field codes — kept in place (appended after the
# wrapper path) so launchers that do proper Exec= parsing still see them and
# substitute file/url args in.
FIELD_CODES = {"%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%i", "%c", "%k", "%v", "%m"}


def read_sysfs(path):
    try:
        p = Path(path)
        if p.is_file():
            return p.read_text().strip()
    except Exception:
        pass
    return None


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
        driver_link = dev_path / "driver"
        driver = driver_link.resolve().name if driver_link.exists() else "unknown"

        vendor = "Generic"
        if "0x1002" in vendor_id.lower():
            vendor = "AMD"
        elif "0x10de" in vendor_id.lower():
            vendor = "NVIDIA"
        elif "0x8086" in vendor_id.lower():
            vendor = "Intel"

        model = f"Graphics ({device_id})"
        try:
            pci_addr = dev_path.resolve().name
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
        power_w = None
        fan_mode = None
        supports_fan_control = False
        hwmon_dirs = list(dev_path.glob("hwmon/hwmon*"))
        if hwmon_dirs:
            hdir = hwmon_dirs[0]
            t = read_sysfs(hdir / "temp1_input")
            if t and t.isdigit():
                temp_c = round(int(t) / 1000, 1)
            p = read_sysfs(hdir / "power1_average") or read_sysfs(hdir / "power1_input")
            if p and p.isdigit():
                power_w = round(int(p) / 1000000.0, 1)
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

        gpus.append({
            "id": card.name,
            "vendor": vendor,
            "model": model,
            "driver": driver,
            "tempC": temp_c,
            "vramUsedMb": vram_used_mb if vram_total_mb > 0 else None,
            "vramTotalMb": vram_total_mb if vram_total_mb > 0 else None,
            "vramPercent": vram_percent,
            "busyPercent": busy_percent,
            "powerWatts": power_w,
            "supportsTuning": supports_tuning,
            "performanceLevel": performance_level,
            "supportsFanControl": supports_fan_control,
            "fanMode": fan_mode,
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
            ["nvidia-smi", "--query-gpu=gpu_name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw",
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
        telemetry = {
            "tempC": float(parts[4]),
            "vramUsedMb": vram_used,
            "vramTotalMb": vram_total,
            "vramPercent": round((vram_used / vram_total) * 100, 1) if vram_total > 0 else None,
            "busyPercent": int(parts[3]) if parts[3].isdigit() else None,
            "powerWatts": float(parts[5]) if len(parts) > 5 else None,
        }
        if i < len(nvidia_entries):
            nvidia_entries[i].update(telemetry)
        else:
            model_name = re.sub(r'^NVIDIA\s+', '', parts[0]).strip()
            gpus.append({
                "id": f"nvidia{i}", "vendor": "NVIDIA", "model": model_name, "driver": "nvidia",
                "supportsTuning": False, "performanceLevel": None,
                "supportsFanControl": False, "fanMode": None,
                **telemetry,
            })

    return gpus


def get_render_default():
    return "nvidia" if RENDER_TOGGLE_FILE.exists() else "amd"


def set_render_default(target):
    if target not in ("amd", "nvidia"):
        return {"status": "error", "message": f"Invalid render default: {target}"}

    try:
        if target == "nvidia":
            RENDER_TOGGLE_DIR.mkdir(parents=True, exist_ok=True)
            RENDER_TOGGLE_FILE.write_text(RENDER_TOGGLE_CONTENT)
        else:
            RENDER_TOGGLE_FILE.unlink(missing_ok=True)
    except OSError as e:
        return {"status": "error", "message": str(e)}

    try:
        subprocess.run(["hyprctl", "reload"], check=True, capture_output=True, text=True, timeout=3.0)
    except Exception as e:
        return {
            "status": "error",
            "message": f"Wrote render default but 'hyprctl reload' failed: {e}",
        }

    return {"status": "success", "renderDefault": target}


def get_driver(card_id):
    driver_link = Path(f"/sys/class/drm/{card_id}/device/driver")
    return driver_link.resolve().name if driver_link.exists() else None


def set_performance_level(card_id, level):
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
            cmd = f"echo '{level}' > {dev_path}"
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
                cmd = f"echo '2' > {pwm_enable_file}"
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
                cmd = f"echo '1' > {pwm_enable_file} && echo '{pwm_num}' > {pwm_file}"
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


def env_lines_for_gpu(gpu):
    if gpu == "nvidia":
        return [
            "export __NV_PRIME_RENDER_OFFLOAD=1",
            "export __GLX_VENDOR_LIBRARY_NAME=nvidia",
            "export __VK_LAYER_NV_optimus=NVIDIA_only",
        ]
    return [
        "unset __NV_PRIME_RENDER_OFFLOAD",
        "unset __GLX_VENDOR_LIBRARY_NAME",
        "unset __VK_LAYER_NV_optimus",
    ]


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


def rewrite_exec_lines(content, basename, gpu):
    """Point every Exec= line at a generated wrapper script instead of
    prefixing it with `env VAR=val ...` directly — see the module docstring
    for why. field codes (%U etc.) stay in Exec= itself, after the wrapper
    path, so spec-compliant launchers still substitute args into them."""
    env_lines = env_lines_for_gpu(gpu)
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


def apply_desktop_gpu(basename, gpu):
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

    if gpu not in ("amd", "nvidia"):
        return {"status": "error", "message": f"Invalid GPU target: {gpu}", "file": basename}

    pristine = get_pristine_desktop_content(basename)
    if pristine is None:
        return {"status": "error", "message": f"{basename} not found", "file": basename}

    rewritten = APP_GPU_MARKER + rewrite_exec_lines(pristine, basename, gpu)

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
    if gpu not in ("amd", "nvidia", "auto"):
        return {"status": "error", "message": f"Invalid GPU target: {gpu}"}

    overrides = load_saved_overrides()
    if app_key in overrides and "desktopFiles" in overrides[app_key]:
        entry = dict(overrides[app_key])
    elif desktop_files:
        entry = {"label": label or app_key, "desktopFiles": desktop_files}
    else:
        return {"status": "error", "message": f"Unknown app: {app_key}"}

    results = [apply_desktop_gpu(f, gpu) for f in entry.get("desktopFiles", [])]
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
    distinct_vendors = sorted(set(g["vendor"] for g in gpus))
    has_nvidia = any(g["vendor"] == "NVIDIA" for g in gpus)
    is_hybrid = has_nvidia and len(distinct_vendors) > 1

    output = {
        "gpus": gpus,
        "isHybrid": is_hybrid,
        "renderDefault": get_render_default(),
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
