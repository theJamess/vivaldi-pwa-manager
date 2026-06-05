# Vivaldi PWA Manager

A small GTK3 thing for cleaning up after Vivaldi's PWA feature.

Vivaldi can install a website as an app — neat. The `.desktop` file it writes is bare-bones: no window size, no isolated profile, no dark-mode override. Delete that file and you can't easily get it back, even though Vivaldi still remembers the PWA. And if you want an app-feel window that *also* has tabs and an address bar, there's no UI for that.

This is the fix-it utility. Lists every Vivaldi-launched `.desktop` in `~/.local/share/applications`, edits them with a sensible form, surfaces orphan PWAs, and adds a third launcher style — **sandboxed window** — full Vivaldi (tabs, address bar) with its own profile and WM class so the panel treats it as a separate app.

Built and tested on Linux Mint Cinnamon. Should be fine anywhere PyGObject + GTK 3 + a Chromium-based Vivaldi exist.

## What you get

- **Lists** every Vivaldi-launched `.desktop`, including ICE-style `WebApp-*` entries (still Vivaldi under the hood).
- **Orphan PWAs** — anything Vivaldi remembers but has no launcher for. One click to make one.
- **Three launcher kinds**, switchable in place:
  - **PWA** — `--app-id`, chromeless. The Vivaldi-default shape.
  - **Web App** — `--app=URL`, chromeless, no PWA record. Quick app-ify any URL.
  - **Sandboxed window** — full Vivaldi window, isolated `--user-data-dir` profile, own WM class. Multi-tab + own taskbar identity.
- **Structured form** for the flags that matter: window state / size / position, isolated profile (one tick), incognito, force-dark, language, proxy, `--password-store=basic` (silences Mint keyring prompts), `SingleMainWindow`, Categories / Keywords / MimeType, *etc*.
- **Icon picker** with three sources — file browser, fetch from URL (scrapes `<link rel=icon>` + apple-touch-icon + manifest icons + favicon), and a searchable browser over installed icon themes. Browse by **Category** (Apps, Actions, Mimetypes…) or by **Folder** (every dir under `/usr/share/icons` etc.). Pick a tint colour and the grid live-recolours symbolic + monocolour SVG icons; PNGs tint on click + save (with monochrome detection so black-on-transparent ink actually changes colour).
- **Flag reference** — built-in dialog with descriptions for ~25 useful Chromium flags. Includes the "how do I get the address bar back" answer (you can't — that's why *Sandboxed window* exists).
- **Round-trip safe** — unmanaged keys (`X-WebApp-*`, custom additions) survive saves via `RawConfigParser`. Vivaldi's `#!/usr/bin/env xdg-open` shebang is preserved when present.
- **Ctrl+Q** quits.

## Requirements

- Python 3.8+ with PyGObject (GTK 3 bindings)
- Vivaldi (any modern build)
- Optional: `python3-xlib` for the alt-tab icon override

```bash
# Mint / Ubuntu / Debian
sudo apt install python3-gi gir1.2-gtk-3.0 python3-xlib
# Fedora
sudo dnf install python3-gobject gtk3 python3-xlib
# Arch
sudo pacman -S python-gobject gtk3 python-xlib
```

## Install

```bash
git clone https://github.com/theJamess/vivaldi-pwa-manager.git
cd vivaldi-pwa-manager
./install_launcher.sh
```

Drops a menu entry pointing back at this dir. Move the repo, re-run the script. One Python file, no build step.

## Using it

- **Left pane** lists every launcher. Single-click to inspect, double-click to launch.
- **Right pane** is the editor. Save commits, Revert discards, Delete asks first.
- **Kind dropdown** rewrites the Exec in place when changed, preserving URL / app-id.
- **+** opens *New launcher*. Default is **Install via Vivaldi** because Vivaldi handles real PWAs better than anything we can fake (manifest, extensions, per-app settings). It opens the URL in Vivaldi — finish from there: **right-click the tab → Progressive Web Apps → Install page as app…**:

  ![Right-click tab → Progressive Web Apps → Install page as app…](docs/install-via-vivaldi.png)

  After install, hit Refresh in the manager and the new PWA shows up. The dialog also has direct *Sandboxed window* / *Web App* options. URL is optional for Sandboxed — leave it blank if startup pages / pinned tabs will handle what loads.
- **ⓘ** opens the flag reference dialog.
- **Duplicate** clones the selected launcher for "I want a second one signed in to a different account."
- **Forget orphan…** deletes Vivaldi's cached icon dir for an orphan. Doesn't fully uninstall (use *Open vivaldi://apps* for that).

After Save, `update-desktop-database` refreshes so Cinnamon's menu sees the change immediately.

### The three kinds

| Kind | Gives you | Use for |
|------|-----------|---------|
| **PWA** | Chromeless app window via `--app-id`, lives in Vivaldi's PWA registry | Apps installed via Vivaldi |
| **Web App** | Chromeless app window via `--app=URL`, no PWA record needed | Quick app-ifying any URL |
| **Sandboxed window** | Full Vivaldi window, separate profile + WM class | Multi-tab dashboards, two-of-the-same logins |

### Fixing alt-tab's "every PWA shows Vivaldi's icon"

Cinnamon's alt-tab uses `_NET_WM_ICON`, which Chromium sets to its own logo for `--app=URL` and sandboxed windows (true PWAs *should* pull from the manifest, but it's flaky). The `.desktop`'s `Icon=` only gets consulted by some surfaces.

Tick **Override alt-tab / taskbar icon via xseticon wrapper** in the *Window* expander. On Save the manager installs `~/.local/bin/vivaldi-pwa-icon-wrap` (a small python-xlib helper) and rewraps the Exec so the helper patches `_NET_WM_ICON` after the window appears. Untick to revert. X11 only (Cinnamon is X11). Needs `python3-xlib`. Falls through to a plain exec if the helper / deps are missing — never breaks your launcher.

### "Visible on all workspaces" — known broken on Cinnamon

The *Window* expander has a **Visible on all workspaces (sticky)** checkbox that asks the wrapper helper to set `_NET_WM_STATE_STICKY` + `_NET_WM_DESKTOP=0xFFFFFFFF` on the new window. Works on EWMH-compliant WMs (KDE/KWin, Xfwm, Mate's Marco, Openbox).

**Doesn't work on Cinnamon/Muffin 6.x.** Muffin sets the state-property bit but doesn't actually pin the window — verified across `wmctrl -b add,sticky`, `wmctrl -t -1`, `libwnck.Window.pin()`, and our own ClientMessages with both `SOURCE=normal-app` and `SOURCE=pager`. None of them trip Muffin's internal `Meta.Window.stick()`. The only way is right-click → **Always on Visible Workspace** after launch, or set a keybinding for `org.cinnamon.desktop.keybindings.wm toggle-on-all-workspaces` and hit it manually.

If/when Muffin grows back a working client-app sticky path, the checkbox will start working with no other changes.

### Isolated profile in one tick

*Profile & Privacy* expander → **Isolated profile** → Save. Sets `--user-data-dir=$HOME/.local/share/vivaldi-pwa-profiles/<wmclass-slug>` and `mkdir -p`'s the dir on first save (only inside our managed root — never arbitrary user-typed paths). Run two of the same app signed in to different accounts and they don't see each other.

## Recipes

### One window with 5–6 social tabs

1. **+ → Sandboxed window** → Name: `Socials`, URL blank, Save. Launch.
2. In that Vivaldi window: `vivaldi://settings/startup` → **Specific pages** → paste your URLs. Close, relaunch. Window opens with all of them.

Layer on top: pin tabs (right-click → *Pin Tab*), Workspaces for grouping, per-profile extensions (Dark Reader / content-blockers won't touch your main Vivaldi), distinct alt-tab icon via *Browse system icons* + tint.

### Two of the same web app with different logins

**+ → Sandboxed** → Save → **Duplicate** → rename + new WM Class. Each gets its own isolated profile dir; cookies don't cross-contaminate.

### Self-hosted lab service behind a self-signed cert

Put it behind a reverse proxy with a real cert (Let's Encrypt / SWAG / Caddy / Traefik) — PWAs *just work*, no flags or cert imports.

If you have to hit the raw IP: `--ignore-certificate-errors --test-type` in *Extra flags*. Catch — only takes effect when Vivaldi *isn't already running* (singleton IPC discards new-launch flags). Trusting the cert in `vivaldi://certificate-manager` is the only thing that survives the singleton.

## What it writes

- `~/.local/share/applications/*.desktop` — the launchers
- `~/.local/share/vivaldi-pwa-profiles/<slug>/` — isolated profile dirs (only when *Isolated profile* is on)
- `~/.local/share/vivaldi-pwa-icons/` — fetched / tinted icon variants
- `~/.local/bin/vivaldi-pwa-icon-wrap` — alt-tab icon helper (only when override is on)

Vivaldi's profile (`~/.config/vivaldi/`) is read-only as far as this tool is concerned.

## Known limitations

Tracked as GitHub issues so you can subscribe / comment / +1:

- **[#1](https://github.com/theJamess/vivaldi-pwa-manager/issues/1)** — *Visible on all workspaces (sticky)* doesn't work on Cinnamon/Muffin. EWMH path is dead-on-arrival; right-click → *Always on Visible Workspace* is the only mechanism. Works on KDE / Xfce / Mate / Openbox.
- **[#2](https://github.com/theJamess/vivaldi-pwa-manager/issues/2)** — True PWAs (`--app-id`) don't store their URL in the launcher. Converting PWA → Web App / Sandboxed needs you to type the URL by hand. Could be auto-recovered from Chromium's sqlite DB.
- **[#3](https://github.com/theJamess/vivaldi-pwa-manager/issues/3)** — Shell-wrapped `Exec=sh -c '…'` lines force the form into read-only mode (no winners from a tool guessing shell escaping).
- **[#4](https://github.com/theJamess/vivaldi-pwa-manager/issues/4)** — Cinnamon-tested only. Reports from KDE / GNOME / Xfce / etc. welcome.
- **[#5](https://github.com/theJamess/vivaldi-pwa-manager/issues/5)** — Wayland: tool itself works, the X11 window-poke features (icon override / sticky) no-op.
- **[#6](https://github.com/theJamess/vivaldi-pwa-manager/issues/6)** — PWAs at hosts with untrusted certs (UDM-Pro, raw IPMI, lab gear at IPs) crash on *Continue*. `--ignore-certificate-errors` dies on Vivaldi's singleton; the only durable fix is trusting the cert in `vivaldi://certificate-manager` (or the NSS DB).

Permanent design choice, not a bug: **no flag puts the address bar back in `--app` mode.** That's the whole point of `--app`. Use *Sandboxed window* if you want chrome.

## Contributing

PRs welcome. Goal: one Python file you can read in 20 minutes. New structured field bar = "people will flip this regularly." Otherwise → flag reference dialog and *Extra flags*.

## License

MIT — see [LICENSE](LICENSE). Do whatever you want with it.
