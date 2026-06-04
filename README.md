# Vivaldi PWA Manager

A small GTK3 GUI for managing the `.desktop` launchers that Vivaldi (and ICE-style web-app tools) drop into `~/.local/share/applications`. List them, edit them, launch them, delete them, and create new ones — including a *Sandboxed window* mode that gives you a full Vivaldi window (tabs, address bar, the lot) but with its own Chromium profile and WM class so it pins to the taskbar as a separate app.

Designed against Vivaldi on Linux Mint Cinnamon, but works anywhere PyGObject/GTK3 + a Chromium-based Vivaldi build are available.

## Why

Vivaldi's PWA support is great but its post-install management is sparse:

- The .desktop file Vivaldi writes is minimal — no easy way to set `--user-data-dir`, force dark mode, set a startup size, route through a proxy, etc.
- If you want a PWA-style "app feel" but with **multiple tabs** (e.g. for a self-hosted dashboard that links to subpages), nothing in Vivaldi's UI does that.
- "Orphan" PWAs — ones Vivaldi knows about internally but whose launcher you deleted — are invisible until you recreate them.

This tool surfaces all of it in one window.

## Features

- **Lists every Vivaldi-launched `.desktop`** in `~/.local/share/applications`, with icons.
- **Surfaces orphan PWAs** — records inside Vivaldi's profile (`~/.config/vivaldi/Default/Web Applications/Manifest Resources/`) that don't have a launcher yet. One click to create one.
- **Three launcher kinds**, switchable in-place:
  - **PWA** — true Chromium PWA via `--app-id=<hash>` (no chrome).
  - **Web App** — chromeless window via `--app=URL` (ICE-style).
  - **Sandboxed window** — full Vivaldi (tabs + address bar) but with its own `--user-data-dir` profile and WM class, so it pins to the taskbar like a separate app and has its own cookie jar / login.
- **Structured form** for the most useful Chromium flags:
  - Window: maximized / fullscreen / size / position, `SingleMainWindow`
  - Profile & privacy: isolated profile (auto `--user-data-dir`), incognito, disable-extensions, `--password-store=basic` (skip kwallet/gnome-keyring prompts on Mint)
  - Appearance & network: force dark mode, language, proxy server
  - Desktop integration: Categories, Keywords, MimeType, Comment, NoDisplay
- **Flag reference dialog** with descriptions for ~25 common Chromium flags, plus a "Insert into Extra flags" button.
- **Safe round-trip**: unmanaged keys (`X-WebApp-*`, `StartupNotify`, custom additions) are preserved via `RawConfigParser`. Vivaldi's `#!/usr/bin/env xdg-open` shebang style is preserved when present.

## Requirements

- Python 3.8+
- PyGObject (`python3-gi`) with GTK 3 bindings
- Vivaldi (any recent build that supports PWAs)

On Mint / Ubuntu / Debian:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0
```

On Fedora:

```bash
sudo dnf install python3-gobject gtk3
```

On Arch:

```bash
sudo pacman -S python-gobject gtk3
```

## Install

Clone the repo anywhere you like:

```bash
git clone https://github.com/<your-user>/vivaldi-pwa-manager.git
cd vivaldi-pwa-manager
./install_launcher.sh
```

`install_launcher.sh` drops a `.desktop` entry into `~/.local/share/applications` that points back at this directory, so the app shows up in your menu as **Vivaldi PWA Manager**. Move or delete the repo and you'll need to re-run the script.

To run it without installing a menu entry:

```bash
python3 vivaldi_pwa_manager.py
```

## Usage

- **Left pane** lists all detected Vivaldi launchers. Single-click to inspect, double-click (or *Launch*) to open. Orphan PWAs (no launcher yet) appear at the bottom.
- **Right pane** is the editor. Most fields explain themselves; the **Kind** dropdown reshapes the underlying `Exec` line when changed (preserving URL and app-id from the form).
- **+ New** in the header bar creates a fresh *Sandboxed window* launcher from just a name + URL.
- **Flag reference** (ⓘ in the header) opens the full flag list with descriptions.

After Save, `update-desktop-database` is invoked so Cinnamon's menu picks up changes immediately.

### About the three kinds

| Kind | What you get | Use it for |
|------|--------------|------------|
| **PWA** (`--app-id`) | Chromeless window, uses Vivaldi's PWA record | Apps you "installed" via Vivaldi's menu |
| **Web App** (`--app=URL`) | Chromeless window, no PWA record needed | Quick app-windows for any URL |
| **Sandboxed window** | Full Vivaldi window (tabs, address bar), isolated profile, own WM class | Multi-tab "app-feel" apps; two-of-the-same-thing logins (two Slacks, etc.) |

There is no Chromium flag to re-enable the menu/address bar/tabs in app mode — that's the whole point of `--app` and `--app-id`. *Sandboxed window* exists for exactly that reason.

### Isolated profile recipe

Tick **Isolated profile** in the *Profile & Privacy* expander and the manager sets:

```
--user-data-dir=$HOME/.local/share/vivaldi-pwa-profiles/<wmclass-slug>
```

Each isolated launcher gets its own Chromium profile dir (~50–150 MB), with independent cookies, extensions, bookmarks. Run two of the same web app signed in to different accounts.

## Files written

- `~/.local/share/applications/*.desktop` — the launchers themselves
- `~/.local/share/vivaldi-pwa-profiles/<slug>/` — isolated profile dirs (only when *Isolated profile* is ticked)

Nothing else. Vivaldi's profile (`~/.config/vivaldi/`) is read but never modified.

## Limitations

- For true PWAs (`--app-id`), the manager can't recover the original start URL — Vivaldi stores it inside Chromium's sqlite databases, which the tool doesn't parse. Converting a PWA to *Web App* or *Sandboxed window* requires you to type the URL.
- Shell-wrapped `Exec=sh -c '…'` lines (rare with Vivaldi, common with some ICE tools) are detected as opaque and left untouched; the structured form is read-only for those.
- Cinnamon-tested only. Other desktops (KDE / GNOME / XFCE) should work — `.desktop` is standard — but pinning/grouping behavior depends on the desktop's WM-class handling.

## Contributing

PRs welcome. Keep it a single-file GTK3 tool — the goal is a small, self-contained utility, not a framework.

If you find a Vivaldi/Chromium flag combo that would be useful as a structured field (not just a free-form extra), open an issue.

## License

MIT — see [LICENSE](LICENSE).
