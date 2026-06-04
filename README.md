# Vivaldi PWA Manager

A small GTK3 thing for cleaning up after Vivaldi's PWA feature.

Vivaldi can install a website as an app — neat. The `.desktop` file it writes for it is... minimal. No flags for window size. No isolated profile. No dark-mode override. If you ever delete that file you can't easily get it back, even though Vivaldi still remembers the PWA internally. And if you want a real *app-feel* window that's also got tabs and an address bar, well — Vivaldi's UI doesn't have an option for that either.

This is the fix-it utility for all of the above. Lists every Vivaldi-launched `.desktop` in `~/.local/share/applications`, lets you edit them with a sensible form instead of `nano`, surfaces the orphan PWAs Vivaldi forgot about, and adds a third launcher style I'm calling **sandboxed window** — full Vivaldi (tabs, address bar, the lot) but with its own profile and WM class, so the panel treats it as a separate app.

Built and tested on Linux Mint Cinnamon. Should be fine anywhere PyGObject + GTK 3 + a Chromium-based Vivaldi exist.

## What you get

- **List view** of every Vivaldi-launched `.desktop` in `~/.local/share/applications`, with icons. ICE-style `WebApp-*` entries from Mint's Web App Manager show up too, since they're still Vivaldi under the hood.
- **Orphan PWAs**: anything Vivaldi remembers in `~/.config/vivaldi/Default/Web Applications/Manifest Resources/` that doesn't have a launcher yet. One click to make one.
- **Three launcher kinds**, switchable on any existing entry:
  - **PWA** — true Chromium PWA via `--app-id`. No chrome. This is what Vivaldi normally writes.
  - **Web App** — `--app=URL`. No chrome. ICE-style. Quick way to app-ify any URL with no manifest involved.
  - **Sandboxed window** — full Vivaldi window with tabs + address bar + menu, but pinned to its own `--user-data-dir` profile and its own WM class. Looks like a separate app in the taskbar, behaves like a normal browser inside.
- **Structured form** for the Chromium flags people actually want:
  - **Window**: maximized / fullscreen / size / position, `SingleMainWindow`
  - **Profile & privacy**: isolated profile (one-click), incognito, disable-extensions, `--password-store=basic` (silences keyring prompts on Mint)
  - **Appearance & network**: force dark mode, language override, proxy server
  - **Desktop integration**: Categories, Keywords, MimeType, Comment, NoDisplay
- **Flag reference** — built-in dialog with descriptions for ~25 useful Chromium flags. One-click "insert into Extra flags." Includes the answer to "how do I get the address bar back" (you can't, it's the design — use sandboxed window instead).
- **Doesn't eat your hand-edits**. Unmanaged keys (`X-WebApp-*`, custom additions, weird vendor stuff) round-trip through `RawConfigParser` and survive saves. Vivaldi's `#!/usr/bin/env xdg-open` shebang is preserved when present.

## Requirements

- Python 3.8+
- PyGObject with GTK 3 bindings
- Vivaldi (any modern build)

Mint / Ubuntu / Debian:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0
```

Fedora:

```bash
sudo dnf install python3-gobject gtk3
```

Arch:

```bash
sudo pacman -S python-gobject gtk3
```

## Install

```bash
git clone https://github.com/theJamess/vivaldi-pwa-manager.git
cd vivaldi-pwa-manager
./install_launcher.sh
```

`install_launcher.sh` drops a `.desktop` entry into `~/.local/share/applications` pointing back at this directory. The app shows up in your menu as **Vivaldi PWA Manager**. Move the repo and the launcher breaks — re-run the script if you do.

Or just run it directly:

```bash
python3 vivaldi_pwa_manager.py
```

No build step, no virtualenv, nothing to compile. It's one Python file.

## Using it

- **Left pane**: every Vivaldi launcher it found, plus orphan PWAs at the bottom. Single-click to inspect, double-click (or *Launch*) to open.
- **Right pane**: editor. Change anything, hit *Save*.
- **Kind dropdown**: switching it rewrites the underlying `Exec` line in place, preserving URL/app-id from the form. So you can take an existing chromeless PWA, flip it to *Sandboxed window*, and have a tabbed version pinned alongside.
- **+** in the header bar: new sandboxed launcher from just a name + URL.
- **ⓘ** in the header bar: the flag reference dialog.

After Save, `update-desktop-database` is called so Cinnamon's menu sees the change immediately.

### The three kinds at a glance

| Kind | What it gives you | Use it for |
|------|-------------------|------------|
| **PWA** (`--app-id`) | Chromeless app window. Lives in Vivaldi's PWA registry. | Stuff you actually "installed as an app" via Vivaldi |
| **Web App** (`--app=URL`) | Chromeless app window. No PWA record needed. | Quick app-ifying any URL |
| **Sandboxed window** | Full Vivaldi window. Separate profile. Separate WM class. | Multi-tab dashboards, two-of-the-same logins (two Slacks, two Gmails, take your pick) |

### Isolated profile, in one tick

Check **Isolated profile** in the *Profile & Privacy* expander, hit Save. You get:

```
--user-data-dir=$HOME/.local/share/vivaldi-pwa-profiles/<wmclass-slug>
```

Each isolated launcher carves out its own Chromium profile (~50–150 MB), with independent cookies, extensions, bookmarks, the works. Run two of the same web app signed in to different accounts and they'll never see each other.

The profile directory is created on Save, but *only* if it sits inside `~/.local/share/vivaldi-pwa-profiles/`. The tool won't reach into arbitrary user-typed paths and `mkdir -p` them.

## What it writes

- `~/.local/share/applications/*.desktop` — the launchers
- `~/.local/share/vivaldi-pwa-profiles/<slug>/` — isolated profile dirs, only when *Isolated profile* is on

Nothing else. Vivaldi's profile (`~/.config/vivaldi/`) is read-only as far as this tool is concerned.

## Known limitations

- **True PWAs don't store their URL in the launcher** — Vivaldi keeps it in a Chromium sqlite DB the tool doesn't poke. If you convert a PWA to *Web App* or *Sandboxed window*, you'll need to type the URL.
- **Shell-wrapped Exec lines** (`Exec=sh -c '…'`) are left alone — the form goes read-only for those because nobody wins from this tool second-guessing your shell escaping.
- **Cinnamon-tested only.** Other desktops should work — `.desktop` is a standard — but pinning and WM-class grouping behavior depends on the desktop, not this tool.
- **No, there isn't a flag to put the address bar back into app mode.** I checked. The whole point of `--app` is no chrome. Use *Sandboxed window* if you want chrome.

## Contributing

PRs welcome. Goal is to stay one Python file you can read in 20 minutes. If you're adding a new structured field, the bar is "people will actually flip this regularly" — otherwise it belongs in the flag reference dialog and the *Extra flags* box.

If you find a Vivaldi/Chromium flag that's genuinely useful and not already in the reference dialog, open an issue with what it does and why.

## License

MIT — see [LICENSE](LICENSE). Do whatever you want with it.
