# Changelog

All notable changes will go here. Format roughly follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] — 2026-06-04

First tagged release. Feature-complete enough to be useful daily; rough edges tracked as GitHub issues.

### What it does

- Lists every Vivaldi-launched `.desktop` in `~/.local/share/applications` (including ICE-style `WebApp-*` entries).
- Surfaces orphan PWAs — anything Vivaldi remembers in its profile that has no launcher yet. One click to materialize one.
- Edit launchers via a structured form; unmanaged `.desktop` keys (X-WebApp-\*, custom additions) survive saves via `RawConfigParser` round-trip.
- Three launcher kinds, switchable in place: **PWA** (`--app-id`), **Web App** (`--app=URL`), **Sandboxed window** (full Vivaldi + isolated profile + own WM class).
- *New launcher* dialog defaults to **Install via Vivaldi** — opens Vivaldi at a URL so the user can finish the install via the right-click → Progressive Web Apps → Install page as app… flow. Direct *Sandboxed window* / *Web App* options also available.
- Structured form for the Chromium flags people actually want: window state / size / position, isolated profile, incognito, force-dark, language, proxy, `--password-store=basic`, alt-tab icon override (via `python3-xlib` helper), *Visible on all workspaces* checkbox.
- Icon picker with three sources: file browser, fetch from URL (scrapes `<link rel=icon>` / apple-touch-icon / og:image / web manifest), and a searchable browser over installed icon themes (~5k icons on a Mint default). Browse by **Category** or by **Folder**.
- Live icon tinting: pick a color and the grid re-renders symbolic + monocolour SVG icons; raster PNGs tint on click + save (with monochrome detection so black-on-transparent ink actually changes colour). Viewport-prioritized, lazy.
- Custom app icon (Vivaldi-style rounded red square + bold "PWA" + gear badge).
- Flag reference dialog: ~25 useful Chromium flags with descriptions.
- Duplicate launchers; *Forget orphan* deletes Vivaldi's cached icon dir for orphan PWAs; *Open vivaldi://apps* shortcut for proper Vivaldi-side uninstalls.
- `Ctrl+Q` to quit.

### Known limitations (as filed issues)

- [#1](https://github.com/theJamess/vivaldi-pwa-manager/issues/1) Sticky checkbox doesn't take effect on Cinnamon/Muffin 6.x (works on KDE / Xfce / Mate / Openbox).
- [#2](https://github.com/theJamess/vivaldi-pwa-manager/issues/2) True PWA → Web App / Sandboxed conversion requires the user to type the URL (could be auto-recovered from Chromium sqlite).
- [#3](https://github.com/theJamess/vivaldi-pwa-manager/issues/3) Shell-wrapped `Exec=sh -c '…'` launchers go read-only.
- [#4](https://github.com/theJamess/vivaldi-pwa-manager/issues/4) Cinnamon-only test coverage so far — reports from other desktops welcome.
- [#5](https://github.com/theJamess/vivaldi-pwa-manager/issues/5) Wayland: editor works, X11 window-poke features (icon override / sticky) no-op.
- [#6](https://github.com/theJamess/vivaldi-pwa-manager/issues/6) PWAs at hosts with untrusted certs (UDM-Pro, raw IPMI, lab gear at IPs) crash on *Continue*; cert-trust in `vivaldi://certificate-manager` is the only durable fix.

[0.1.0]: https://github.com/theJamess/vivaldi-pwa-manager/releases/tag/v0.1.0
