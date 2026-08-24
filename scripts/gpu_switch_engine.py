#!/usr/bin/env python3
"""
GPU Switch Engine
Global default-renderer toggle and per-app GPU pinning for hybrid AMD/NVIDIA
(and Intel) laptops, via PRIME render offload.

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
  exactly how GNOME's own "Launch using Discrete Graphics Card" works),
  prefixing every Exec= line with `env VAR=val ...` (NVIDIA) or
  `env -u VAR ...` (force AMD, overriding the global toggle). TryExec= is
  never touched, since the desktop spec requires it to stay a bare
  executable path for existence checks.
"""

import sys
import os
import json
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
APP_GPU_MARKER = "# Written by the GPU Switch plugin — per-app GPU override\n"

NVIDIA_EXEC_PREFIX = "env __NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia __VK_LAYER_NV_optimus=NVIDIA_only "
AMD_EXEC_PREFIX = "env -u __NV_PRIME_RENDER_OFFLOAD -u __GLX_VENDOR_LIBRARY_NAME -u __VK_LAYER_NV_optimus "

DEFAULT_APP_GPU_CONFIG = {
    "blender": {"label": "Blender", "gpu": "nvidia", "desktopFiles": ["blender.desktop"]},
    "unrealengine": {"label": "Unreal Engine", "gpu": "nvidia", "desktopFiles": ["UnrealEngine.desktop"]},
    "vscode": {"label": "VS Code", "gpu": "amd", "desktopFiles": ["code.desktop"]},
    "libreoffice": {
        "label": "LibreOffice",
        "gpu": "amd",
        "desktopFiles": [
            "libreoffice-startcenter.desktop", "libreoffice-writer.desktop",
            "libreoffice-calc.desktop", "libreoffice-impress.desktop",
            "libreoffice-draw.desktop", "libreoffice-base.desktop", "libreoffice-math.desktop",
        ],
    },
}


def read_sysfs(path):
    try:
        p = Path(path)
        if p.is_file():
            return p.read_text().strip()
    except Exception:
        pass
    return None


def scan_gpu_vendors():
    """Lightweight vendor scan — just enough to know if this is a hybrid
    (2+ distinct vendor) system, without pulling in full telemetry."""
    vendors = []
    drm_cards = sorted(Path("/sys/class/drm").glob("card[0-9]*"))
    actual_cards = [c for c in drm_cards if "-" not in c.name]
    for card in actual_cards:
        dev_path = card / "device"
        vendor_id = read_sysfs(dev_path / "vendor") or ""
        driver_link = dev_path / "driver"
        driver = driver_link.resolve().name if driver_link.exists() else "unknown"
        name = "Generic"
        if "0x1002" in vendor_id.lower():
            name = "AMD"
        elif "0x10de" in vendor_id.lower():
            name = "NVIDIA"
        elif "0x8086" in vendor_id.lower():
            name = "Intel"
        vendors.append({"id": card.name, "vendor": name, "driver": driver})
    return vendors


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
        import subprocess
        subprocess.run(["hyprctl", "reload"], check=True, capture_output=True, text=True, timeout=3.0)
    except Exception as e:
        return {
            "status": "error",
            "message": f"Wrote render default but 'hyprctl reload' failed: {e}",
        }

    return {"status": "success", "renderDefault": target}


def load_app_gpu_state():
    state = {key: dict(entry) for key, entry in DEFAULT_APP_GPU_CONFIG.items()}
    if APP_GPU_STATE_FILE.exists():
        try:
            saved = json.loads(APP_GPU_STATE_FILE.read_text())
            if isinstance(saved, dict):
                for key, entry in saved.items():
                    if isinstance(entry, dict) and "gpu" in entry:
                        merged = dict(state.get(key, {}))
                        merged.update(entry)
                        state[key] = merged
        except (json.JSONDecodeError, OSError):
            pass
    return state


def save_app_gpu_state(state):
    APP_GPU_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = APP_GPU_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, APP_GPU_STATE_FILE)


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


def rewrite_exec_lines(content, prefix):
    out_lines = []
    for line in content.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped.startswith("Exec="):
            value = stripped[len("Exec="):]
            newline = "\n" if line.endswith("\n") else ""
            out_lines.append(f"Exec={prefix}{value}{newline}")
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
            # Our override just shadowed the system entry — remove it to
            # reveal the system one again.
            try:
                if user_path.exists():
                    current = user_path.read_text()
                    if current.startswith(APP_GPU_MARKER):
                        user_path.unlink()
            except OSError as e:
                return {"status": "error", "message": str(e), "file": basename}
        else:
            # No system entry — this was always a user-only file, so restore
            # its pristine (pre-us) content instead of deleting it outright.
            pristine = get_pristine_desktop_content(basename)
            if pristine is None:
                return {"status": "error", "message": f"{basename} not found", "file": basename}
            try:
                user_path.write_text(pristine)
            except OSError as e:
                return {"status": "error", "message": str(e), "file": basename}
        return {"status": "success", "file": basename, "gpu": "auto"}

    if gpu not in ("amd", "nvidia"):
        return {"status": "error", "message": f"Invalid GPU target: {gpu}", "file": basename}

    pristine = get_pristine_desktop_content(basename)
    if pristine is None:
        return {"status": "error", "message": f"{basename} not found", "file": basename}

    prefix = NVIDIA_EXEC_PREFIX if gpu == "nvidia" else AMD_EXEC_PREFIX
    rewritten = APP_GPU_MARKER + rewrite_exec_lines(pristine, prefix)

    try:
        USER_APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)
        user_path.write_text(rewritten)
    except OSError as e:
        return {"status": "error", "message": str(e), "file": basename}

    return {"status": "success", "file": basename, "gpu": gpu}


def set_app_gpu(app_key, gpu):
    if gpu not in ("amd", "nvidia", "auto"):
        return {"status": "error", "message": f"Invalid GPU target: {gpu}"}

    state = load_app_gpu_state()
    if app_key not in state:
        return {"status": "error", "message": f"Unknown app: {app_key}"}

    results = [apply_desktop_gpu(f, gpu) for f in state[app_key].get("desktopFiles", [])]
    failures = [r for r in results if r["status"] == "error"]

    state[app_key]["gpu"] = gpu
    save_app_gpu_state(state)

    if failures:
        return {
            "status": "error",
            "message": "; ".join(f"{r['file']}: {r['message']}" for r in failures),
            "appKey": app_key,
        }
    return {"status": "success", "appKey": app_key, "gpu": gpu, "label": state[app_key].get("label", app_key)}


def add_app(app_key, label, desktop_file, gpu):
    if not app_key or not desktop_file:
        return {"status": "error", "message": "App key and desktop file are required"}
    if gpu not in ("amd", "nvidia", "auto"):
        return {"status": "error", "message": f"Invalid GPU target: {gpu}"}

    state = load_app_gpu_state()
    state[app_key] = {"label": label or app_key, "gpu": "auto", "desktopFiles": [desktop_file]}
    save_app_gpu_state(state)

    if gpu != "auto":
        return set_app_gpu(app_key, gpu)
    return {"status": "success", "appKey": app_key, "gpu": "auto", "label": state[app_key]["label"]}


def remove_app(app_key):
    state = load_app_gpu_state()
    if app_key not in state:
        return {"status": "error", "message": f"Unknown app: {app_key}"}

    for f in state[app_key].get("desktopFiles", []):
        apply_desktop_gpu(f, "auto")

    if app_key in DEFAULT_APP_GPU_CONFIG:
        # Built-in entries just reset to auto rather than disappearing.
        state[app_key]["gpu"] = "auto"
    else:
        del state[app_key]
    save_app_gpu_state(state)
    return {"status": "success", "appKey": app_key}


def main():
    if len(sys.argv) > 1:
        action = sys.argv[1]
        if action == "--set-render-default" and len(sys.argv) > 2:
            print(json.dumps(set_render_default(sys.argv[2])))
            return
        elif action == "--set-app-gpu" and len(sys.argv) > 3:
            print(json.dumps(set_app_gpu(sys.argv[2], sys.argv[3])))
            return
        elif action == "--add-app" and len(sys.argv) > 5:
            print(json.dumps(add_app(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])))
            return
        elif action == "--remove-app" and len(sys.argv) > 2:
            print(json.dumps(remove_app(sys.argv[2])))
            return

    vendors = scan_gpu_vendors()
    distinct_vendors = sorted(set(v["vendor"] for v in vendors))
    has_nvidia = any(v["vendor"] == "NVIDIA" for v in vendors)
    is_hybrid = has_nvidia and len(distinct_vendors) > 1

    output = {
        "gpus": vendors,
        "isHybrid": is_hybrid,
        "renderDefault": get_render_default(),
        "appGpuAssignments": load_app_gpu_state(),
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
