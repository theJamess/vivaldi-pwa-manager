#!/usr/bin/env python3
"""Vivaldi PWA Manager — small GTK3 GUI for listing, editing, launching, and
removing Vivaldi-launched .desktop entries in ~/.local/share/applications,
plus surfacing Vivaldi-internal PWA records that don't yet have a launcher.

Save semantics:
- Unmanaged .desktop keys (X-WebApp-*, Comment, etc.) are preserved via
  RawConfigParser round-trip.
- Managed Chromium flags (window state, isolation, dark mode, …) are owned by
  the structured form: on load they're stripped from the Exec field; on save
  they're re-emitted from the form. The Exec field itself stays editable and
  holds the "core" command (binary + --profile-directory + --app-id/--app).
"""

import os
import re
import shlex
import shutil
import subprocess
from configparser import RawConfigParser
from io import StringIO
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, GdkPixbuf, GLib  # noqa: E402

APPS_DIR = Path.home() / ".local/share/applications"
VIVALDI_PROFILE = Path.home() / ".config/vivaldi/Default"
MANIFEST_DIR = VIVALDI_PROFILE / "Web Applications" / "Manifest Resources"
ISOLATED_PROFILES_ROOT = Path.home() / ".local/share/vivaldi-pwa-profiles"

VIVALDI_BINARIES = ("vivaldi", "vivaldi-stable", "/opt/vivaldi/vivaldi")


def find_vivaldi_binary() -> str:
    """Pick the best Vivaldi binary available on this system, preferring
    PATH-resolved names over absolute paths so the produced .desktop entries
    keep working across upgrades and distros."""
    for name in ("vivaldi-stable", "vivaldi", "vivaldi-snapshot"):
        if shutil.which(name):
            return name
    for path in ("/opt/vivaldi/vivaldi", "/usr/bin/vivaldi-stable", "/usr/bin/vivaldi"):
        if os.path.exists(path):
            return path
    return "vivaldi-stable"  # last-ditch fallback; user can edit


DEFAULT_BINARY = find_vivaldi_binary()

KIND_LABELS = [
    ("pwa", "PWA  (--app-id, no chrome)"),
    ("webapp", "Web App  (--app=URL, no chrome)"),
    ("sandboxed", "Sandboxed window  (full chrome, isolated profile)"),
    ("vivaldi", "Other Vivaldi launcher"),
]
APP_ID_RE = re.compile(r"--app-id=([A-Za-z0-9]+)")
APP_URL_RE = re.compile(r"""--app=(?:"([^"]+)"|'([^']+)'|(\S+))""")

# Managed Chromium flags — these get stripped from Exec on load and re-emitted
# from structured form fields on save.
MANAGED_BOOL_FLAGS = {
    "--start-maximized", "--start-fullscreen",
    "--incognito", "--disable-extensions", "--force-dark-mode",
}
MANAGED_KV_FLAG_NAMES = {
    "--window-size", "--window-position", "--user-data-dir",
    "--lang", "--proxy-server", "--password-store",
    "--class", "--name",
}

# Reference table shown in the "Flag reference" dialog.
# Tuple: (flag, description, has_value)
REFERENCE_FLAGS = [
    ("--start-maximized", "Open window maximized.", False),
    ("--start-fullscreen", "Open window fullscreen.", False),
    ("--window-size=W,H", "Initial window size in pixels (e.g. 1280,800).", True),
    ("--window-position=X,Y", "Initial window position in pixels.", True),
    ("--user-data-dir=PATH", "Separate Chromium profile (per-PWA cookies/login). "
        "Key trick for multi-account: two Slacks, two Gmails, etc.", True),
    ("--profile-directory=NAME", "Pick which sub-profile inside the user-data-dir.", True),
    ("--app-id=HASH", "Launch the Chromium PWA with this app-id (no chrome).", True),
    ("--app=URL", "Launch URL in app-mode window (no chrome). ICE-style.", True),
    ("--class=NAME", "Set X11 WM_CLASS (taskbar pinning / grouping).", True),
    ("--name=NAME", "Set X11 instance name (usually matches --class).", True),
    ("--incognito", "Always open in private browsing mode.", False),
    ("--disable-extensions", "Disable all extensions for this PWA.", False),
    ("--force-dark-mode", "Force dark UI mode (pair with --enable-features="
        "WebContentsForceDark for page content).", False),
    ("--enable-features=WebContentsForceDark", "Force dark on rendered page content. "
        "Companion to --force-dark-mode.", False),
    ("--lang=xx-YY", "Override UI language (en-GB, ja, de, …).", True),
    ("--proxy-server=URL", "Route this PWA through a proxy (http://host:port or "
        "socks5://host:port). Per-PWA tunnel routing.", True),
    ("--password-store=basic", "Skip kwallet / gnome-keyring prompts on Mint Cinnamon.", False),
    ("--no-default-browser-check", "Suppress the 'set as default browser' prompt.", False),
    ("--no-first-run", "Skip the Vivaldi first-run flow.", False),
    ("--disable-gpu", "Disable hardware acceleration (last resort for crashes/glitches).", False),
    ("--use-gl=desktop", "Force GL backend: 'desktop', 'angle', or 'swiftshader'.", True),
    ("--ozone-platform=x11", "Force X11 backend. Cinnamon is X11 by default.", True),
    ("--remote-debugging-port=N", "Expose Chrome DevTools on a local port.", True),
    ("--auto-open-devtools-for-tabs", "Open DevTools on launch (debugging only).", False),
    ("--ignore-certificate-errors", "Skip TLS validation. Use only for self-hosted "
        "lab apps you control.", False),
    ("--new-window", "Force a new window rather than focusing existing.", False),
]

REFERENCE_FOOTER = (
    "There is no Chromium flag that re-enables menu / address bar / tabs in app mode — "
    "that's the entire purpose of --app and --app-id. To get a normal browser window, "
    "drop --app=/--app-id= and launch the URL as a regular tab instead."
)


# ---------- .desktop parse / write ----------

def _read_with_shebang(path: Path):
    text = path.read_text(errors="replace")
    had_shebang = text.startswith("#!")
    lines = text.splitlines()
    start = 0
    for i, ln in enumerate(lines):
        if ln.strip().startswith("["):
            start = i
            break
    return text, "\n".join(lines[start:]), had_shebang


def parse_desktop(path: Path):
    cp = RawConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        text, body, had_shebang = _read_with_shebang(path)
        cp.read_string(body)
    except Exception:
        return None
    if "Desktop Entry" not in cp:
        return None
    sec = cp["Desktop Entry"]
    return {
        "path": str(path),
        "name": sec.get("Name", ""),
        "exec": sec.get("Exec", ""),
        "icon": sec.get("Icon", ""),
        "wmclass": sec.get("StartupWMClass", ""),
        "comment": sec.get("Comment", ""),
        "categories": sec.get("Categories", ""),
        "keywords": sec.get("Keywords", ""),
        "mimetype": sec.get("MimeType", ""),
        "single_main_window": sec.get("SingleMainWindow", "").lower() == "true",
        "no_display": sec.get("NoDisplay", "").lower() == "true",
        "type": sec.get("Type", "Application"),
        "terminal": sec.get("Terminal", "false"),
        "_cp": cp,
        "_had_shebang": had_shebang,
    }


def write_desktop(path: Path, cp: RawConfigParser, had_shebang: bool):
    buf = StringIO()
    cp.write(buf, space_around_delimiters=False)
    body = buf.getvalue()
    if had_shebang:
        body = "#!/usr/bin/env xdg-open\n" + body
    path.write_text(body)
    if had_shebang:
        os.chmod(path, 0o755)


VIVALDI_BIN_RE = re.compile(r"^vivaldi(-[a-z]+)?$")


def is_vivaldi_exec(exec_line: str) -> bool:
    """True iff one of the Exec tokens IS a vivaldi binary (not just a path
    that contains the word 'vivaldi')."""
    if not exec_line:
        return False
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        tokens = exec_line.split()
    for tok in tokens:
        base = os.path.basename(tok)
        if VIVALDI_BIN_RE.match(base):
            return True
    return False


# ---------- Exec-line structured model ----------

def parse_exec(exec_line: str) -> dict:
    """Decompose a Chromium-style Exec line into structured pieces.

    Managed flags go into named slots and are removed from `core` so the form
    can present them as widgets. Unrecognised tokens stay in `core` so editing
    the Exec field still works.
    """
    model = {
        "core": exec_line,
        "opaque": False,
        "user_data_dir": "",
        "window_state": "default",
        "window_size": "",
        "window_position": "",
        "wm_class": "",
        "wm_name": "",
        "incognito": False,
        "disable_extensions": False,
        "force_dark_mode": False,
        "password_store_basic": False,
        "lang": "",
        "proxy_server": "",
    }
    if not exec_line:
        return model
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        model["opaque"] = True
        return model
    if not tokens:
        return model
    if tokens[0] in ("sh", "/bin/sh", "bash", "/bin/bash") and "-c" in tokens:
        model["opaque"] = True
        return model

    keep = []
    for t in tokens:
        if t == "--start-maximized":
            model["window_state"] = "maximized"
        elif t == "--start-fullscreen":
            model["window_state"] = "fullscreen"
        elif t == "--incognito":
            model["incognito"] = True
        elif t == "--disable-extensions":
            model["disable_extensions"] = True
        elif t == "--force-dark-mode":
            model["force_dark_mode"] = True
        elif t == "--password-store=basic":
            model["password_store_basic"] = True
        elif t.startswith("--window-size="):
            model["window_size"] = t.split("=", 1)[1]
        elif t.startswith("--window-position="):
            model["window_position"] = t.split("=", 1)[1]
        elif t.startswith("--user-data-dir="):
            model["user_data_dir"] = t.split("=", 1)[1]
        elif t.startswith("--lang="):
            model["lang"] = t.split("=", 1)[1]
        elif t.startswith("--proxy-server="):
            model["proxy_server"] = t.split("=", 1)[1]
        elif t.startswith("--class="):
            model["wm_class"] = t.split("=", 1)[1]
        elif t.startswith("--name="):
            model["wm_name"] = t.split("=", 1)[1]
        else:
            keep.append(t)
    model["core"] = " ".join(_quote_token(x) for x in keep)
    return model


def _quote_token(t: str) -> str:
    """Quote a token for inclusion in an Exec line, preserving --app="URL" style."""
    if t.startswith("--app=") and not t.startswith("--app-id="):
        return f'--app="{t[len("--app="):]}"'
    if " " in t or '"' in t or "'" in t:
        return shlex.quote(t)
    return t


def build_exec(core: str, model: dict, extras: str, wm_class: str) -> str:
    """Reassemble Exec from the (possibly-edited) core + structured model + extras."""
    if model.get("opaque"):
        # Don't touch shell-wrapped commands; user owns Exec entirely.
        return core
    parts = [core.strip()] if core.strip() else []
    flags = []
    if wm_class:
        flags.append(f"--class={wm_class}")
        flags.append(f"--name={wm_class}")
    elif model.get("wm_class"):
        flags.append(f'--class={model["wm_class"]}')
        if model.get("wm_name"):
            flags.append(f'--name={model["wm_name"]}')
    if model["user_data_dir"]:
        flags.append(f'--user-data-dir={model["user_data_dir"]}')
    if model["window_state"] == "maximized":
        flags.append("--start-maximized")
    elif model["window_state"] == "fullscreen":
        flags.append("--start-fullscreen")
    if model["window_size"]:
        flags.append(f'--window-size={model["window_size"]}')
    if model["window_position"]:
        flags.append(f'--window-position={model["window_position"]}')
    if model["incognito"]:
        flags.append("--incognito")
    if model["disable_extensions"]:
        flags.append("--disable-extensions")
    if model["force_dark_mode"]:
        flags.append("--force-dark-mode")
    if model["password_store_basic"]:
        flags.append("--password-store=basic")
    if model["lang"]:
        flags.append(f'--lang={model["lang"]}')
    if model["proxy_server"]:
        flags.append(f'--proxy-server={model["proxy_server"]}')
    parts.extend(flags)
    if extras and extras.strip():
        parts.append(extras.strip())
    return " ".join(parts)


# ---------- Discovery ----------

def extract_app_id(exec_line: str):
    m = APP_ID_RE.search(exec_line or "")
    return m.group(1) if m else ""


def extract_app_url(exec_line: str):
    m = APP_URL_RE.search(exec_line or "")
    if not m:
        return ""
    return m.group(1) or m.group(2) or m.group(3) or ""


POSITIONAL_URL_RE = re.compile(r"\bhttps?://\S+")


def extract_positional_url(exec_line: str):
    """First http(s):// token that isn't inside a flag value."""
    try:
        tokens = shlex.split(exec_line or "")
    except ValueError:
        tokens = (exec_line or "").split()
    for t in tokens:
        if t.startswith("--"):
            continue
        if POSITIONAL_URL_RE.match(t):
            return t
    return ""


def detect_kind(exec_line: str, app_id: str, app_url: str) -> str:
    """pwa | webapp | sandboxed | vivaldi"""
    if app_id:
        return "pwa"
    if app_url:
        return "webapp"
    if extract_positional_url(exec_line):
        return "sandboxed"
    return "vivaldi"


def build_core_for_kind(kind: str, binary: str, app_id: str, url: str) -> str:
    """Build the 'core' Exec (binary + identity flags + URL). WMClass and
    --user-data-dir are added later by build_exec() from the form state.
    """
    binary = (binary or "vivaldi-stable").strip()
    if kind == "pwa":
        tail = f" --profile-directory=Default --app-id={app_id}" if app_id else ""
        return binary + tail
    if kind == "webapp":
        tail = f' --app="{url}"' if url else ""
        return binary + tail
    if kind == "sandboxed":
        parts = [binary, "--no-first-run", "--no-default-browser-check"]
        if url:
            parts.append(url)
        return " ".join(parts)
    return binary


def discover_launchers():
    items = []
    if not APPS_DIR.is_dir():
        return items
    for f in sorted(APPS_DIR.glob("*.desktop")):
        d = parse_desktop(f)
        if not d:
            continue
        if not is_vivaldi_exec(d.get("exec", "")):
            continue
        d["app_id"] = extract_app_id(d["exec"])
        app_url = extract_app_url(d["exec"])
        d["url"] = app_url or extract_positional_url(d["exec"])
        d["kind"] = detect_kind(d["exec"], d["app_id"], app_url)
        items.append(d)
    return items


def discover_orphan_pwas(existing_app_ids):
    orphans = []
    if not MANIFEST_DIR.is_dir():
        return orphans
    for d in sorted(MANIFEST_DIR.iterdir()):
        if not d.is_dir():
            continue
        if d.name in existing_app_ids:
            continue
        orphans.append({"app_id": d.name, "icon_dir": str(d / "Icons")})
    return orphans


def find_best_icon_for_appid(app_id: str):
    icons_dir = MANIFEST_DIR / app_id / "Icons"
    if not icons_dir.is_dir():
        return ""
    best, best_size = None, -1
    for p in icons_dir.glob("*.png"):
        try:
            sz = int(p.stem)
        except ValueError:
            continue
        if sz > best_size:
            best_size, best = sz, p
    return str(best) if best else ""


def load_icon_pixbuf(icon_field: str, app_id: str, size: int = 48):
    candidates = []
    if icon_field:
        if os.path.isabs(icon_field) and os.path.exists(icon_field):
            candidates.append(icon_field)
        else:
            try:
                info = Gtk.IconTheme.get_default().lookup_icon(icon_field, size, 0)
                if info:
                    candidates.append(info.get_filename())
            except Exception:
                pass
    if app_id:
        p = find_best_icon_for_appid(app_id)
        if p:
            candidates.append(p)
    for c in candidates:
        if not c:
            continue
        try:
            return GdkPixbuf.Pixbuf.new_from_file_at_size(c, size, size)
        except Exception:
            continue
    try:
        return Gtk.IconTheme.get_default().load_icon("application-x-executable", size, 0)
    except Exception:
        return None


def isolated_profile_path_for(wm_class: str, app_id: str) -> str:
    slug = wm_class or app_id or "pwa"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", slug).strip("-").lower() or "pwa"
    return str(ISOLATED_PROFILES_ROOT / slug)


def ensure_isolated_profile_dir(path: str) -> None:
    """Create the user-data-dir if it sits inside our managed root. We avoid
    auto-creating arbitrary user-typed paths to stay predictable."""
    if not path:
        return
    try:
        p = Path(path).resolve()
        root = ISOLATED_PROFILES_ROOT.resolve()
        p.relative_to(root)
    except (ValueError, OSError):
        return
    p.mkdir(parents=True, exist_ok=True)


# ---------- UI ----------

class PWAManager(Gtk.Window):
    COL_PIXBUF, COL_NAME, COL_SUBTITLE, COL_PATH, COL_KIND = 0, 1, 2, 3, 4

    def __init__(self):
        super().__init__(title="Vivaldi PWA Manager")
        self.set_default_size(1080, 720)
        self.set_icon_name("vivaldi")
        self.items = []
        self.orphans = []
        self.current = None
        self._build_ui()
        self.refresh()

    # ---- layout ----
    def _build_ui(self):
        hb = Gtk.HeaderBar(show_close_button=True, title="Vivaldi PWA Manager")
        self.set_titlebar(hb)
        for icon, tip, cb in (
            ("view-refresh-symbolic", "Re-scan launchers", lambda *_: self.refresh()),
            ("folder-symbolic", "Open ~/.local/share/applications",
             lambda *_: subprocess.Popen(["xdg-open", str(APPS_DIR)])),
            ("dialog-information-symbolic", "Flag reference",
             lambda *_: self._show_reference()),
        ):
            b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            b.set_tooltip_text(tip)
            b.connect("clicked", cb)
            hb.pack_end(b)
        new_btn = Gtk.Button.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON)
        new_btn.set_tooltip_text("New launcher (sandboxed window)")
        new_btn.connect("clicked", lambda *_: self._new_sandboxed())
        hb.pack_start(new_btn)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(330)
        self.add(paned)

        # --- left: list ---
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.store = Gtk.ListStore(GdkPixbuf.Pixbuf, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store, headers_visible=False)
        col = Gtk.TreeViewColumn("App")
        cell_pix = Gtk.CellRendererPixbuf(); cell_pix.set_padding(4, 2)
        col.pack_start(cell_pix, False); col.add_attribute(cell_pix, "pixbuf", self.COL_PIXBUF)
        cell_text = Gtk.CellRendererText(); cell_text.props.ypad = 2
        col.pack_start(cell_text, True)
        col.set_cell_data_func(cell_text, self._render_two_lines)
        self.tree.append_column(col)
        self.tree.get_selection().connect("changed", self._on_select)
        self.tree.connect("row-activated", lambda *_: self._launch())
        sw = Gtk.ScrolledWindow(); sw.add(self.tree)
        left.pack_start(sw, True, True, 0)
        paned.add1(left)

        # --- right: scrolled detail pane ---
        right_sw = Gtk.ScrolledWindow()
        right_sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.set_margin_top(10); right.set_margin_bottom(10)
        right.set_margin_start(12); right.set_margin_end(12)
        right_sw.add(right)
        paned.add2(right_sw)

        # header
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.icon_img = Gtk.Image(); self.icon_img.set_size_request(64, 64)
        head.pack_start(self.icon_img, False, False, 0)
        head_in = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.name_entry = Gtk.Entry(placeholder_text="Name")
        head_in.pack_start(self.name_entry, False, False, 0)
        self.subtitle_lbl = Gtk.Label(xalign=0)
        self.subtitle_lbl.set_line_wrap(True)
        self.subtitle_lbl.get_style_context().add_class("dim-label")
        head_in.pack_start(self.subtitle_lbl, False, False, 0)
        head.pack_start(head_in, True, True, 0)
        right.pack_start(head, False, False, 0)

        # core grid
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        right.pack_start(grid, False, False, 0)

        def add_row(row, label, widget, helper=None):
            lbl = Gtk.Label(label=label, xalign=1)
            grid.attach(lbl, 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
            if helper is not None:
                grid.attach(helper, 2, row, 1, 1)
            widget.set_hexpand(True)

        self.icon_entry = Gtk.Entry(placeholder_text="Icon name or absolute path")
        icon_btn = Gtk.Button.new_from_icon_name("document-open-symbolic", Gtk.IconSize.BUTTON)
        icon_btn.set_tooltip_text("Browse for icon file")
        icon_btn.connect("clicked", self._browse_icon)
        add_row(1, "Icon", self.icon_entry, icon_btn)

        self.kind_combo = Gtk.ComboBoxText()
        for k, label in KIND_LABELS:
            self.kind_combo.append(k, label)
        self.kind_combo.set_tooltip_text(
            "PWA: chromeless --app-id window.\n"
            "Web App: chromeless --app=URL window.\n"
            "Sandboxed window: full Vivaldi (tabs + address bar) but isolated "
            "profile + own WM class. Best for app-feel + multi-tab."
        )
        self.kind_combo.connect("changed", self._on_kind_changed)
        add_row(0, "Kind", self.kind_combo)

        self.url_entry = Gtk.Entry(placeholder_text="https://… (Web App or Sandboxed)")
        add_row(2, "URL", self.url_entry)

        self.appid_entry = Gtk.Entry(placeholder_text="Chromium app-id")
        self.appid_entry.set_editable(False)
        add_row(3, "App ID", self.appid_entry)

        self.wmclass_entry = Gtk.Entry(placeholder_text="StartupWMClass / --class")
        self.wmclass_entry.set_tooltip_text(
            "Sets both StartupWMClass and --class/--name. Match this against "
            "the WM class your panel pinning uses."
        )
        add_row(4, "WM Class", self.wmclass_entry)

        # ---- Window expander ----
        win_exp = Gtk.Expander(label="Window")
        win_exp.set_expanded(True)
        right.pack_start(win_exp, False, False, 0)
        win_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        win_box.set_margin_top(6); win_box.set_margin_start(12)
        win_exp.add(win_box)

        state_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.rb_default = Gtk.RadioButton.new_with_label_from_widget(None, "Default")
        self.rb_max = Gtk.RadioButton.new_with_label_from_widget(self.rb_default, "Maximized")
        self.rb_full = Gtk.RadioButton.new_with_label_from_widget(self.rb_default, "Fullscreen")
        state_row.pack_start(Gtk.Label(label="State:", xalign=0), False, False, 0)
        for rb in (self.rb_default, self.rb_max, self.rb_full):
            state_row.pack_start(rb, False, False, 0)
        win_box.pack_start(state_row, False, False, 0)

        size_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        size_row.pack_start(Gtk.Label(label="Size (W,H):", xalign=0), False, False, 0)
        self.win_size_entry = Gtk.Entry(placeholder_text="e.g. 1280,800")
        size_row.pack_start(self.win_size_entry, True, True, 0)
        size_row.pack_start(Gtk.Label(label="  Position (X,Y):"), False, False, 0)
        self.win_pos_entry = Gtk.Entry(placeholder_text="e.g. 100,100")
        size_row.pack_start(self.win_pos_entry, True, True, 0)
        win_box.pack_start(size_row, False, False, 0)

        self.cb_single_main = Gtk.CheckButton(label="Single main window (focus existing instance)")
        win_box.pack_start(self.cb_single_main, False, False, 0)

        # ---- Profile & Privacy ----
        priv_exp = Gtk.Expander(label="Profile & Privacy")
        right.pack_start(priv_exp, False, False, 0)
        priv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        priv_box.set_margin_top(6); priv_box.set_margin_start(12)
        priv_exp.add(priv_box)

        iso_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.cb_isolated = Gtk.CheckButton(label="Isolated profile (separate cookies/login)")
        self.cb_isolated.set_tooltip_text(
            "Sets --user-data-dir to ~/.local/share/vivaldi-pwa-profiles/<wmclass>. "
            "Lets you run two of the same PWA signed in to different accounts."
        )
        iso_row.pack_start(self.cb_isolated, False, False, 0)
        priv_box.pack_start(iso_row, False, False, 0)
        udd_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        udd_row.pack_start(Gtk.Label(label="user-data-dir:", xalign=0), False, False, 0)
        self.udd_entry = Gtk.Entry(placeholder_text="(auto when Isolated profile is checked)")
        udd_row.pack_start(self.udd_entry, True, True, 0)
        priv_box.pack_start(udd_row, False, False, 0)
        self.cb_isolated.connect("toggled", self._on_isolated_toggled)

        self.cb_incognito = Gtk.CheckButton(label="Always launch incognito (--incognito)")
        priv_box.pack_start(self.cb_incognito, False, False, 0)
        self.cb_no_ext = Gtk.CheckButton(label="Disable extensions (--disable-extensions)")
        priv_box.pack_start(self.cb_no_ext, False, False, 0)
        self.cb_pwstore = Gtk.CheckButton(label="Skip keyring (--password-store=basic) — Mint-friendly")
        priv_box.pack_start(self.cb_pwstore, False, False, 0)

        # ---- Appearance & Network ----
        app_exp = Gtk.Expander(label="Appearance & Network")
        right.pack_start(app_exp, False, False, 0)
        app_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        app_box.set_margin_top(6); app_box.set_margin_start(12)
        app_exp.add(app_box)

        self.cb_dark = Gtk.CheckButton(label="Force dark mode (--force-dark-mode)")
        self.cb_dark.set_tooltip_text(
            "For page content also, add --enable-features=WebContentsForceDark "
            "via Extra flags."
        )
        app_box.pack_start(self.cb_dark, False, False, 0)
        lang_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lang_row.pack_start(Gtk.Label(label="Language:", xalign=0), False, False, 0)
        self.lang_entry = Gtk.Entry(placeholder_text="e.g. en-GB, ja, de")
        lang_row.pack_start(self.lang_entry, True, True, 0)
        app_box.pack_start(lang_row, False, False, 0)
        proxy_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        proxy_row.pack_start(Gtk.Label(label="Proxy:", xalign=0), False, False, 0)
        self.proxy_entry = Gtk.Entry(placeholder_text="http://host:port or socks5://host:port")
        proxy_row.pack_start(self.proxy_entry, True, True, 0)
        app_box.pack_start(proxy_row, False, False, 0)

        # ---- Desktop integration ----
        di_exp = Gtk.Expander(label="Desktop integration")
        right.pack_start(di_exp, False, False, 0)
        di_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        di_box.set_margin_top(6); di_box.set_margin_start(12)
        di_exp.add(di_box)
        for label, attr, ph in (
            ("Categories:", "categories_entry", "Network;WebBrowser; (semicolon-separated)"),
            ("Keywords:", "keywords_entry", "self-hosted;monitoring; (semicolon-separated)"),
            ("MimeType:", "mimetype_entry", "x-scheme-handler/rumble; (advanced)"),
            ("Comment:", "comment_entry", "Tooltip shown in the menu"),
        ):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.pack_start(Gtk.Label(label=label, xalign=0), False, False, 0)
            ent = Gtk.Entry(placeholder_text=ph)
            setattr(self, attr, ent)
            row.pack_start(ent, True, True, 0)
            di_box.pack_start(row, False, False, 0)
        self.cb_no_display = Gtk.CheckButton(label="Hide from menu (NoDisplay=true)")
        di_box.pack_start(self.cb_no_display, False, False, 0)

        # ---- Extra flags + Exec + path ----
        extras_lbl = Gtk.Label(label="Extra flags (appended verbatim):", xalign=0)
        extras_lbl.set_margin_top(6)
        right.pack_start(extras_lbl, False, False, 0)
        self.extras_entry = Gtk.Entry(
            placeholder_text="--no-default-browser-check --enable-features=WebContentsForceDark"
        )
        right.pack_start(self.extras_entry, False, False, 0)

        exec_lbl = Gtk.Label(label="Exec (core command):", xalign=0)
        exec_lbl.set_margin_top(6)
        right.pack_start(exec_lbl, False, False, 0)
        self.exec_entry = Gtk.Entry()
        self.exec_entry.set_tooltip_text(
            "Binary + --profile-directory + --app-id/--app + anything not "
            "recognised. Managed flags are added on save from the form above."
        )
        right.pack_start(self.exec_entry, False, False, 0)

        self.path_lbl = Gtk.Label(xalign=0, selectable=True)
        self.path_lbl.get_style_context().add_class("dim-label")
        self.path_lbl.set_line_wrap(True)
        right.pack_start(self.path_lbl, False, False, 0)

        # buttons
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_row.set_margin_top(10)
        self.launch_btn = Gtk.Button(label="Launch")
        self.launch_btn.connect("clicked", lambda *_: self._launch())
        self.save_btn = Gtk.Button(label="Save")
        self.save_btn.get_style_context().add_class("suggested-action")
        self.save_btn.connect("clicked", lambda *_: self._save())
        self.revert_btn = Gtk.Button(label="Revert")
        self.revert_btn.connect("clicked", lambda *_: self._populate(self.current))
        self.delete_btn = Gtk.Button(label="Delete…")
        self.delete_btn.get_style_context().add_class("destructive-action")
        self.delete_btn.connect("clicked", lambda *_: self._delete())
        self.create_btn = Gtk.Button(label="Create Launcher")
        self.create_btn.connect("clicked", lambda *_: self._create_from_orphan())
        btn_row.pack_start(self.launch_btn, False, False, 0)
        btn_row.pack_start(self.save_btn, False, False, 0)
        btn_row.pack_start(self.revert_btn, False, False, 0)
        btn_row.pack_end(self.delete_btn, False, False, 0)
        btn_row.pack_end(self.create_btn, False, False, 0)
        right.pack_start(btn_row, False, False, 0)

        self.status = Gtk.Statusbar()
        self.status_ctx = self.status.get_context_id("main")
        right.pack_end(self.status, False, False, 0)

        self.icon_entry.connect("changed", lambda *_: self._update_icon_preview())
        self._set_detail_sensitive(False)

    def _render_two_lines(self, col, cell, model, it, _):
        name = model.get_value(it, self.COL_NAME)
        sub = model.get_value(it, self.COL_SUBTITLE)
        cell.set_property(
            "markup",
            f"<b>{GLib.markup_escape_text(name)}</b>\n"
            f"<small>{GLib.markup_escape_text(sub)}</small>",
        )

    # ---- data ----
    def refresh(self):
        self.items = discover_launchers()
        existing = {d["app_id"] for d in self.items if d.get("app_id")}
        self.orphans = discover_orphan_pwas(existing)
        self.store.clear()
        for d in self.items:
            self.store.append([
                load_icon_pixbuf(d.get("icon", ""), d.get("app_id", ""), 40),
                d["name"] or "(unnamed)",
                self._subtitle_for(d),
                d["path"],
                d["kind"],
            ])
        for o in self.orphans:
            self.store.append([
                load_icon_pixbuf("", o["app_id"], 40),
                "(orphan PWA)",
                f"app-id: {o['app_id']}  ·  no launcher",
                "orphan:" + o["app_id"],
                "orphan",
            ])
        self._set_status(f"{len(self.items)} launcher(s), {len(self.orphans)} orphan PWA(s)")
        if len(self.store):
            self.tree.get_selection().select_iter(self.store.get_iter_first())

    def _subtitle_for(self, d):
        return self._subtitle_for_kind(d.get("kind", "vivaldi"),
                                       d.get("app_id", ""),
                                       d.get("url", ""))

    def _set_status(self, msg):
        self.status.pop(self.status_ctx)
        self.status.push(self.status_ctx, msg)

    # ---- selection / populate ----
    def _on_select(self, selection):
        model, it = selection.get_selected()
        if it is None:
            self.current = None
            self._set_detail_sensitive(False)
            return
        path = model.get_value(it, self.COL_PATH)
        kind = model.get_value(it, self.COL_KIND)
        if kind == "orphan":
            self.current = {"_orphan": True, "app_id": path.split(":", 1)[1]}
        else:
            self.current = next((d for d in self.items if d["path"] == path), None)
        self._populate(self.current)

    def _populate(self, d):
        # Clear / freeze signals to avoid feedback loops
        self._clear_form()
        if d is None:
            self._set_detail_sensitive(False)
            return

        if d.get("_orphan"):
            self._set_detail_sensitive(True, orphan=True)
            self.icon_entry.set_text(find_best_icon_for_appid(d["app_id"]))
            self.appid_entry.set_text(d["app_id"])
            self.exec_entry.set_text(
                f"{DEFAULT_BINARY} --profile-directory=Default --app-id={d['app_id']}"
            )
            self.kind_combo.handler_block_by_func(self._on_kind_changed)
            self.kind_combo.set_active_id("pwa")
            self.kind_combo.handler_unblock_by_func(self._on_kind_changed)
            self.path_lbl.set_text("(no launcher yet — fill Name, then Create Launcher)")
            self.subtitle_lbl.set_text(f"Vivaldi PWA · app-id: {d['app_id']}")
            self._update_icon_preview()
            return

        self._set_detail_sensitive(True, orphan=False)
        m = parse_exec(d.get("exec", ""))
        self.name_entry.set_text(d.get("name", ""))
        self.icon_entry.set_text(d.get("icon", ""))
        self.url_entry.set_text(d.get("url", ""))
        self.appid_entry.set_text(d.get("app_id", ""))
        self.wmclass_entry.set_text(d.get("wmclass") or m.get("wm_class", ""))
        self.exec_entry.set_text(m["core"])
        self.path_lbl.set_text(d.get("path", ""))
        self.subtitle_lbl.set_text(self._subtitle_for(d))
        # Kind combo (block handler to avoid Exec reshape on load)
        self.kind_combo.handler_block_by_func(self._on_kind_changed)
        self.kind_combo.set_active_id(d.get("kind", "vivaldi"))
        self.kind_combo.handler_unblock_by_func(self._on_kind_changed)

        # window
        st = m["window_state"]
        (self.rb_max if st == "maximized" else
         self.rb_full if st == "fullscreen" else
         self.rb_default).set_active(True)
        self.win_size_entry.set_text(m["window_size"])
        self.win_pos_entry.set_text(m["window_position"])
        self.cb_single_main.set_active(d.get("single_main_window", False))

        # privacy
        self.udd_entry.set_text(m["user_data_dir"])
        iso_path = isolated_profile_path_for(d.get("wmclass", ""), d.get("app_id", ""))
        self.cb_isolated.set_active(
            bool(m["user_data_dir"]) and m["user_data_dir"] == iso_path
        )
        self.cb_incognito.set_active(m["incognito"])
        self.cb_no_ext.set_active(m["disable_extensions"])
        self.cb_pwstore.set_active(m["password_store_basic"])

        # appearance/network
        self.cb_dark.set_active(m["force_dark_mode"])
        self.lang_entry.set_text(m["lang"])
        self.proxy_entry.set_text(m["proxy_server"])

        # desktop integration
        self.categories_entry.set_text(d.get("categories", ""))
        self.keywords_entry.set_text(d.get("keywords", ""))
        self.mimetype_entry.set_text(d.get("mimetype", ""))
        self.comment_entry.set_text(d.get("comment", ""))
        self.cb_no_display.set_active(d.get("no_display", False))

        # extras: empty on load (new flags only)
        self.extras_entry.set_text("")
        self._update_icon_preview()

    def _clear_form(self):
        for w in (self.name_entry, self.icon_entry, self.url_entry, self.appid_entry,
                  self.wmclass_entry, self.exec_entry, self.win_size_entry, self.win_pos_entry,
                  self.udd_entry, self.lang_entry, self.proxy_entry, self.categories_entry,
                  self.keywords_entry, self.mimetype_entry, self.comment_entry, self.extras_entry):
            w.set_text("")
        for cb in (self.cb_single_main, self.cb_isolated, self.cb_incognito, self.cb_no_ext,
                   self.cb_pwstore, self.cb_dark, self.cb_no_display):
            cb.set_active(False)
        self.rb_default.set_active(True)
        self.kind_combo.handler_block_by_func(self._on_kind_changed)
        self.kind_combo.set_active(-1)
        self.kind_combo.handler_unblock_by_func(self._on_kind_changed)
        self.path_lbl.set_text("")
        self.subtitle_lbl.set_text("")

    def _set_detail_sensitive(self, on, orphan=False):
        widgets = [
            self.name_entry, self.icon_entry, self.url_entry, self.wmclass_entry,
            self.exec_entry, self.win_size_entry, self.win_pos_entry, self.udd_entry,
            self.lang_entry, self.proxy_entry, self.categories_entry, self.keywords_entry,
            self.mimetype_entry, self.comment_entry, self.extras_entry,
            self.rb_default, self.rb_max, self.rb_full,
            self.cb_single_main, self.cb_isolated, self.cb_incognito, self.cb_no_ext,
            self.cb_pwstore, self.cb_dark, self.cb_no_display,
            self.kind_combo,
        ]
        for w in widgets:
            w.set_sensitive(on)
        self.launch_btn.set_sensitive(on and not orphan)
        self.save_btn.set_sensitive(on and not orphan)
        self.revert_btn.set_sensitive(on and not orphan)
        self.delete_btn.set_sensitive(on and not orphan)
        self.create_btn.set_sensitive(on and orphan)

    def _update_icon_preview(self):
        pix = load_icon_pixbuf(self.icon_entry.get_text(), self.appid_entry.get_text(), 64)
        if pix:
            self.icon_img.set_from_pixbuf(pix)
        else:
            self.icon_img.set_from_icon_name("application-x-executable", Gtk.IconSize.DIALOG)

    def _on_isolated_toggled(self, cb):
        if cb.get_active():
            path = isolated_profile_path_for(
                self.wmclass_entry.get_text(),
                self.appid_entry.get_text(),
            )
            self.udd_entry.set_text(path)
        else:
            # only clear if it matches the auto-generated path
            current = self.udd_entry.get_text()
            auto = isolated_profile_path_for(
                self.wmclass_entry.get_text(),
                self.appid_entry.get_text(),
            )
            if current == auto:
                self.udd_entry.set_text("")

    # ---- kind switching ----
    def _on_kind_changed(self, _combo):
        """Reshape Exec for the newly-chosen kind, preserving URL / app-id."""
        if not self.current or self.current.get("_orphan"):
            return
        new_kind = self.kind_combo.get_active_id() or "vivaldi"
        if new_kind == "vivaldi":
            return
        # Figure out the binary from the existing core
        core_tokens = shlex.split(self.exec_entry.get_text() or "")
        binary = core_tokens[0] if core_tokens else DEFAULT_BINARY
        url = self.url_entry.get_text().strip()
        app_id = self.appid_entry.get_text().strip()
        if new_kind in ("webapp", "sandboxed") and not url and self.current.get("url"):
            url = self.current["url"]
            self.url_entry.set_text(url)
        new_core = build_core_for_kind(new_kind, binary, app_id, url)
        self.exec_entry.set_text(new_core)
        # Sandboxed feels broken without an isolated profile + WM class
        if new_kind == "sandboxed":
            if not self.wmclass_entry.get_text().strip() and self.current.get("name"):
                slug = re.sub(r"[^A-Za-z0-9]+", "",
                              self.current["name"]) or "VivaldiApp"
                self.wmclass_entry.set_text(slug)
            if not self.cb_isolated.get_active():
                self.cb_isolated.set_active(True)
        self.subtitle_lbl.set_text(self._subtitle_for_kind(new_kind, app_id, url))

    def _subtitle_for_kind(self, kind, app_id, url):
        if kind == "pwa":
            return f"PWA · app-id: {app_id}"
        if kind == "webapp":
            return f"Web App · {url}"
        if kind == "sandboxed":
            return f"Sandboxed window · {url or '(no URL set)'}"
        return "Vivaldi launcher"

    def _new_sandboxed(self):
        dlg = Gtk.Dialog(title="New sandboxed launcher",
                         transient_for=self, modal=True)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Create", Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(6); box.set_margin_top(8)
        box.set_margin_start(10); box.set_margin_end(10); box.set_margin_bottom(8)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        name_e = Gtk.Entry(placeholder_text="e.g. Portainer")
        url_e = Gtk.Entry(placeholder_text="https://…")
        icon_e = Gtk.Entry(placeholder_text="Icon name or path (optional)")
        for r, (lbl, w) in enumerate((("Name", name_e), ("URL", url_e), ("Icon", icon_e))):
            grid.attach(Gtk.Label(label=lbl, xalign=1), 0, r, 1, 1)
            grid.attach(w, 1, r, 1, 1)
            w.set_hexpand(True)
        box.pack_start(grid, False, False, 0)
        note = Gtk.Label(
            xalign=0,
            label="Creates a launcher with its own Chromium profile and WM "
                  "class, so it appears as a separate app in the panel but "
                  "keeps full Vivaldi chrome (tabs + address bar).",
        )
        note.set_line_wrap(True)
        note.get_style_context().add_class("dim-label")
        box.pack_start(note, False, False, 0)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            name = name_e.get_text().strip()
            url = url_e.get_text().strip()
            icon = icon_e.get_text().strip()
            dlg.destroy()
            if not name or not url:
                self._error("Name and URL are required.")
                return
            self._create_sandboxed(name, url, icon)
        else:
            dlg.destroy()

    def _create_sandboxed(self, name, url, icon):
        slug = re.sub(r"[^A-Za-z0-9]+", "", name) or "VivaldiApp"
        wm_class = slug
        udd = isolated_profile_path_for(wm_class, "")
        core = build_core_for_kind("sandboxed", DEFAULT_BINARY, "", url)
        # Compose full Exec via build_exec so it picks up WMClass + user-data-dir
        model = {
            "opaque": False, "user_data_dir": udd,
            "window_state": "default", "window_size": "", "window_position": "",
            "wm_class": "", "wm_name": "",
            "incognito": False, "disable_extensions": False,
            "force_dark_mode": False, "password_store_basic": False,
            "lang": "", "proxy_server": "",
        }
        exec_line = build_exec(core, model, "", wm_class)
        ensure_isolated_profile_dir(udd)
        slug_lower = slug.lower()
        out = APPS_DIR / f"vivaldi-sandboxed-{slug_lower}.desktop"
        if out.exists():
            self._error(f"{out.name} already exists.")
            return
        cp = RawConfigParser(interpolation=None, strict=False)
        cp.optionxform = str
        sec = {
            "Version": "1.0",
            "Type": "Application",
            "Terminal": "false",
            "Name": name,
            "Exec": exec_line,
            "StartupWMClass": wm_class,
            "StartupNotify": "true",
            "Categories": "Network;",
        }
        if icon:
            sec["Icon"] = icon
        cp["Desktop Entry"] = sec
        try:
            write_desktop(out, cp, had_shebang=False)
            self._update_mime_cache()
            self._set_status(f"Created {out.name}")
        except Exception as e:
            self._error(f"Create failed: {e}")
            return
        self.refresh()
        self._reselect(str(out))

    # ---- actions ----
    def _browse_icon(self, _btn):
        dlg = Gtk.FileChooserDialog(
            title="Choose icon image", parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        f = Gtk.FileFilter(); f.set_name("Images")
        for p in ("*.png", "*.svg", "*.jpg", "*.ico", "*.webp"):
            f.add_pattern(p)
        dlg.add_filter(f)
        if self.current and self.current.get("app_id"):
            start = MANIFEST_DIR / self.current["app_id"] / "Icons"
            if start.is_dir():
                dlg.set_current_folder(str(start))
        if dlg.run() == Gtk.ResponseType.OK:
            self.icon_entry.set_text(dlg.get_filename())
        dlg.destroy()

    def _launch(self):
        if not self.current or self.current.get("_orphan"):
            return
        # XDG .desktop Exec is shell-like (quoted tokens) but NOT shell-evaluated
        # (no globbing/expansion). Tokenize with shlex and exec directly.
        clean = re.sub(r"%[fFuUdDnNickvm]", "", self.current.get("exec", "")).strip()
        try:
            argv = shlex.split(clean)
            if not argv:
                self._error("Exec is empty.")
                return
            subprocess.Popen(argv, start_new_session=True)
            self._set_status(f"Launched: {self.current['name']}")
        except ValueError as e:
            self._error(f"Exec parse error: {e}")
        except FileNotFoundError:
            self._error(f"Binary not found: {argv[0]}")
        except Exception as e:
            self._error(f"Launch failed: {e}")

    def _collect_form_model(self):
        return {
            "core": self.exec_entry.get_text(),
            "opaque": False,
            "user_data_dir": self.udd_entry.get_text().strip(),
            "window_state": ("maximized" if self.rb_max.get_active() else
                             "fullscreen" if self.rb_full.get_active() else
                             "default"),
            "window_size": self.win_size_entry.get_text().strip(),
            "window_position": self.win_pos_entry.get_text().strip(),
            "wm_class": "",  # consumed via wmclass_entry below
            "wm_name": "",
            "incognito": self.cb_incognito.get_active(),
            "disable_extensions": self.cb_no_ext.get_active(),
            "force_dark_mode": self.cb_dark.get_active(),
            "password_store_basic": self.cb_pwstore.get_active(),
            "lang": self.lang_entry.get_text().strip(),
            "proxy_server": self.proxy_entry.get_text().strip(),
        }

    def _rewrite_url_in_core(self, core: str) -> str:
        new_url = self.url_entry.get_text().strip()
        if not new_url:
            return core
        if APP_URL_RE.search(core):
            return APP_URL_RE.sub(f'--app="{new_url}"', core, count=1)
        if POSITIONAL_URL_RE.search(core):
            return POSITIONAL_URL_RE.sub(new_url, core, count=1)
        return core  # don't inject into --app-id launchers

    def _save(self):
        if not self.current or self.current.get("_orphan"):
            return
        name = self.name_entry.get_text().strip()
        if not name:
            self._error("Name is required.")
            return

        model = self._collect_form_model()
        # If editing an --app= entry, propagate URL changes into core.
        core = self._rewrite_url_in_core(model["core"])
        if not core.strip():
            self._error("Exec (core command) is required.")
            return
        new_exec = build_exec(core, model, self.extras_entry.get_text(),
                              self.wmclass_entry.get_text().strip())
        ensure_isolated_profile_dir(model["user_data_dir"])

        cp: RawConfigParser = self.current["_cp"]
        sec = cp["Desktop Entry"]
        sec["Name"] = name
        sec["Exec"] = new_exec
        if self.icon_entry.get_text().strip():
            sec["Icon"] = self.icon_entry.get_text().strip()
        elif "Icon" in sec:
            del sec["Icon"]
        if self.wmclass_entry.get_text().strip():
            sec["StartupWMClass"] = self.wmclass_entry.get_text().strip()
        elif "StartupWMClass" in sec:
            del sec["StartupWMClass"]
        self._set_or_clear(sec, "Comment", self.comment_entry.get_text().strip())
        self._set_or_clear(sec, "Categories", self.categories_entry.get_text().strip())
        self._set_or_clear(sec, "Keywords", self.keywords_entry.get_text().strip())
        self._set_or_clear(sec, "MimeType", self.mimetype_entry.get_text().strip())
        if self.cb_single_main.get_active():
            sec["SingleMainWindow"] = "true"
        elif "SingleMainWindow" in sec:
            del sec["SingleMainWindow"]
        if self.cb_no_display.get_active():
            sec["NoDisplay"] = "true"
        elif "NoDisplay" in sec:
            del sec["NoDisplay"]
        sec.setdefault("Type", "Application")
        sec.setdefault("Terminal", "false")
        sec.setdefault("Version", "1.0")

        path = Path(self.current["path"])
        try:
            write_desktop(path, cp, self.current.get("_had_shebang", False))
            self._update_mime_cache()
            self._set_status(f"Saved {path.name}")
        except Exception as e:
            self._error(f"Save failed: {e}")
            return
        self.refresh()
        self._reselect(str(path))

    def _set_or_clear(self, sec, key, value):
        if value:
            sec[key] = value
        elif key in sec:
            del sec[key]

    def _delete(self):
        if not self.current or self.current.get("_orphan"):
            return
        path = self.current["path"]
        dlg = Gtk.MessageDialog(
            transient_for=self, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Delete this launcher?",
        )
        dlg.format_secondary_text(
            f"{path}\n\nOnly the .desktop file is removed. Vivaldi's PWA record "
            "(icons / settings) stays in your profile and will reappear as an "
            "orphan."
        )
        if dlg.run() == Gtk.ResponseType.OK:
            try:
                os.remove(path)
                self._update_mime_cache()
                self._set_status(f"Deleted {os.path.basename(path)}")
            except Exception as e:
                self._error(f"Delete failed: {e}")
            self.refresh()
        dlg.destroy()

    def _create_from_orphan(self):
        if not self.current or not self.current.get("_orphan"):
            return
        app_id = self.current["app_id"]
        name = self.name_entry.get_text().strip() or f"PWA-{app_id[:8]}"
        out = APPS_DIR / f"vivaldi-{app_id}-Default.desktop"
        if out.exists():
            self._error(f"{out.name} already exists.")
            return
        cp = RawConfigParser(interpolation=None, strict=False)
        cp.optionxform = str
        cp["Desktop Entry"] = {
            "Version": "1.0",
            "Type": "Application",
            "Terminal": "false",
            "Name": name,
            "Exec": self.exec_entry.get_text().strip()
            or f"{DEFAULT_BINARY} --profile-directory=Default --app-id={app_id}",
            "Icon": self.icon_entry.get_text().strip() or find_best_icon_for_appid(app_id),
        }
        wm = self.wmclass_entry.get_text().strip()
        if wm:
            cp["Desktop Entry"]["StartupWMClass"] = wm
        try:
            write_desktop(out, cp, had_shebang=True)
            self._update_mime_cache()
            self._set_status(f"Created {out.name}")
        except Exception as e:
            self._error(f"Create failed: {e}")
            return
        self.refresh()
        self._reselect(str(out))

    def _reselect(self, path):
        for i, row in enumerate(self.store):
            if row[self.COL_PATH] == path:
                self.tree.get_selection().select_iter(self.store.get_iter(i))
                break

    def _update_mime_cache(self):
        try:
            subprocess.Popen(["update-desktop-database", str(APPS_DIR)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass

    def _error(self, msg):
        dlg = Gtk.MessageDialog(
            transient_for=self, message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK, text="Error",
        )
        dlg.format_secondary_text(msg); dlg.run(); dlg.destroy()

    # ---- reference dialog ----
    def _show_reference(self):
        dlg = Gtk.Dialog(title="Vivaldi / Chromium flag reference",
                         transient_for=self, modal=True)
        dlg.set_default_size(720, 520)
        dlg.add_button("Insert into Extra flags", Gtk.ResponseType.APPLY)
        dlg.add_button("Close", Gtk.ResponseType.CLOSE)

        box = dlg.get_content_area()
        box.set_spacing(6); box.set_margin_top(8)
        box.set_margin_start(8); box.set_margin_end(8); box.set_margin_bottom(8)

        store = Gtk.ListStore(str, str)
        for flag, desc, _ in REFERENCE_FLAGS:
            store.append([flag, desc])
        tv = Gtk.TreeView(model=store)
        tv.set_headers_visible(True)
        col1 = Gtk.TreeViewColumn("Flag", Gtk.CellRendererText(), text=0)
        col1.set_min_width(260)
        col1.set_resizable(True)
        tv.append_column(col1)
        renderer = Gtk.CellRendererText(); renderer.props.wrap_mode = 2
        renderer.props.wrap_width = 380
        col2 = Gtk.TreeViewColumn("Description", renderer, text=1)
        tv.append_column(col2)
        sw = Gtk.ScrolledWindow(); sw.add(tv); sw.set_vexpand(True)
        box.pack_start(sw, True, True, 0)

        footer = Gtk.Label(label=REFERENCE_FOOTER, xalign=0)
        footer.set_line_wrap(True)
        footer.get_style_context().add_class("dim-label")
        box.pack_start(footer, False, False, 0)

        dlg.show_all()
        while True:
            resp = dlg.run()
            if resp == Gtk.ResponseType.APPLY:
                sel = tv.get_selection().get_selected()
                if sel and sel[1]:
                    flag = sel[0].get_value(sel[1], 0)
                    existing = self.extras_entry.get_text().strip()
                    self.extras_entry.set_text((existing + " " + flag).strip())
            else:
                break
        dlg.destroy()


def main():
    win = PWAManager()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
