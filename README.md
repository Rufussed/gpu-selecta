# GPU Switch for Omarchy

An [Omarchy](https://omarchy.org/) shell plugin for hybrid AMD/NVIDIA (or Intel/NVIDIA)
laptops: a global default-renderer toggle plus per-app GPU pinning, using PRIME
render offload.

## What this is (and isn't)

On a muxless hybrid laptop, there's no hardware switch between GPUs — the
discrete GPU has no display path of its own, so the integrated GPU always
drives the screen. **This plugin does not change that.** What it controls is
which GPU newly-launched *apps* render on:

- **Global toggle**: sets PRIME offload environment variables for the whole
  session, applied live via Omarchy's Hyprland toggle mechanism — no restart
  or logout needed, but it only affects apps launched *after* you flip it.
- **Per-app pinning**: rewrites a specific app's `.desktop` launcher entry
  (the standard XDG override mechanism — this is the same technique GNOME's
  own "Launch using Discrete Graphics Card" uses) so that app always renders
  on the GPU you choose, regardless of the global toggle. Good for apps you
  always want on the discrete GPU (Blender, Unreal Engine, games) or always
  want to keep off it (VS Code, LibreOffice, browsers) so the discrete GPU
  doesn't wake for apps that don't need it.

Per-app pinning is the more power-efficient option for apps you use
regularly: the discrete GPU only wakes while that specific app is open,
rather than for an entire session (or, worse, for literally everything if
the global toggle is left on).

Neither mechanism can affect a process that's already running — env vars
only apply at process launch.

## Install

```sh
omarchy plugin add https://github.com/Rufussed/gpu-switch.git --enable
```

If the widget doesn't appear, restart the shell once:

```sh
omarchy restart shell
```

### From a local checkout (development)

```sh
ln -s "$PWD" ~/.config/omarchy/plugins/rufussed.gpu-switch
omarchy-shell shell rescanPlugins
```

QuickShell's file watcher doesn't follow symlinks, so after edits run
`omarchy restart shell`.

## Remove

```sh
omarchy plugin remove rufussed.gpu-switch
```

This unregisters the widget and removes its checkout. It does **not**
automatically revert any per-app `.desktop` overrides or the global toggle
file — reset each app to `Auto` and turn the global toggle back to `AMD`
from the panel *before* removing the plugin, so nothing is left pinned to a
GPU with no way to change it back. (If you forget: delete
`~/.local/state/omarchy/toggles/hypr/gpu-switch-render-default.lua` for the
global toggle, and any `~/.local/share/applications/<app>.desktop` file that
starts with `# Written by the GPU Switch plugin` for per-app overrides —
originals are preserved in `~/.config/omarchy/gpu-switch/backups/` if you
want to restore them by hand.)

## Dependencies

None beyond what Omarchy already provides: `python3`, `hyprctl`. No external
packages, services, or `pkexec`/root access are required.

## Use

- **Click** the bar icon to open the panel.
- **Global Default**: AMD / NVIDIA pills at the top — sets the session-wide
  default for new app launches.
- **Per-App GPU**: each listed app has AMD / Auto / NVIDIA pills. `Auto`
  means "follow the global default"; AMD/NVIDIA pin it regardless of the
  global setting.
- **Add App**: pin any other installed app by its `.desktop` file name
  (list them with `ls /usr/share/applications` or
  `ls ~/.local/share/applications`).
- The ✕ button on an app resets it back to `Auto` (built-in entries) or
  removes it entirely (apps you added yourself).

Seeded defaults: Blender and Unreal Engine → NVIDIA, VS Code and
LibreOffice → AMD. Edit or remove these like any other entry.

## How it works

**Global toggle** writes a small file to
`~/.local/state/omarchy/toggles/hypr/gpu-switch-render-default.lua` using
Omarchy's own Hyprland toggle-file convention — `hl.env()` calls there are
re-applied on every `hyprctl reload`, which is what makes this take effect
live.

**Per-app pinning** writes an override to `~/.local/share/applications/<app>.desktop`,
prefixing every `Exec=` line with `env __NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia __VK_LAYER_NV_optimus=NVIDIA_only`
(NVIDIA) or `env -u __NV_PRIME_RENDER_OFFLOAD -u __GLX_VENDOR_LIBRARY_NAME -u __VK_LAYER_NV_optimus`
(force AMD). `TryExec=` is left untouched, since the desktop entry spec
requires it to stay a bare executable path. The original file is snapshotted
to `~/.config/omarchy/gpu-switch/backups/` the first time an app is touched,
so resetting an app to `Auto` restores it exactly (or removes the override
entirely, revealing the system default, if one exists).

This only affects apps launched through a `.desktop` entry — the app grid,
Omarchy's menu, launchers like Walker/fuzzel. It doesn't affect a binary run
directly from a terminal.

## Config

State lives in `~/.config/omarchy/gpu-switch/apps.json` if you'd rather edit
it directly:

```json
{
  "blender": { "label": "Blender", "gpu": "nvidia", "desktopFiles": ["blender.desktop"] }
}
```

## Security

Telemetry (which GPUs exist) is 100% unprivileged sysfs reads. Applying a
setting only ever writes to files already owned by your user account
(`~/.config`, `~/.local/share/applications`, `~/.local/state`) — no root or
`pkexec` is ever needed, unlike GPU tuning plugins that write to sysfs.

## Credits

GPU vendor/driver detection is adapted from
[OmaGPU](https://github.com/ucmz851/omagpu) by ucmz851 (MIT licensed). This
plugin is a separate, independent tool focused on GPU *launch routing*
rather than telemetry/tuning — see OmaGPU if you want fan curves, DPM power
governors, and hardware telemetry instead.

## License

MIT
