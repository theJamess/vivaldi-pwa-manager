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

import json
import os
import re
import shlex
import shutil
import ssl
import subprocess
import tempfile
import urllib.parse
import urllib.request
from configparser import RawConfigParser
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, Gtk, GdkPixbuf, GLib  # noqa: E402

APPS_DIR = Path.home() / ".local/share/applications"
VIVALDI_PROFILE = Path.home() / ".config/vivaldi/Default"
MANIFEST_DIR = VIVALDI_PROFILE / "Web Applications" / "Manifest Resources"
ISOLATED_PROFILES_ROOT = Path.home() / ".local/share/vivaldi-pwa-profiles"

WRAPPER_NAME = "vivaldi-pwa-icon-wrap"
WRAPPER_INSTALL_DIR = Path.home() / ".local/bin"
WRAPPER_SOURCE = Path(__file__).resolve().parent / WRAPPER_NAME

SCRIPT_DIR = Path(__file__).resolve().parent
INSTALL_SCREENSHOT = SCRIPT_DIR / "docs" / "install-via-vivaldi.png"

FETCHED_ICONS_DIR = Path.home() / ".local/share/vivaldi-pwa-icons"
ICON_THEME_ROOTS = (
    "/usr/share/icons", "/usr/local/share/icons",
    str(Path.home() / ".local/share/icons"), str(Path.home() / ".icons"),
)
HTTP_TIMEOUT = 10
HTTP_USER_AGENT = "Mozilla/5.0 vivaldi-pwa-manager/1.0"
MAX_ICON_BYTES = 4 * 1024 * 1024  # 4 MB per icon; refuse anything wilder

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
        # X-WebApp-URL is set by Mint's Web App Manager and survives our
        # round-trip. Used as a URL fallback when the user's tinkering blows
        # the URL out of the Exec line.
        "x_webapp_url": sec.get("X-WebApp-URL", ""),
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


_EXEC_RESERVED = set(' \t\n"\'\\<>~|&;$*?#()`')


def quote_exec_arg(s: str) -> str:
    """Quote a value for safe inclusion in a .desktop Exec line (XDG rules:
    double quotes, with backslash-escaped \\ and \")."""
    if not s or not any(c in _EXEC_RESERVED for c in s):
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_core_for_kind(kind: str, binary: str, app_id: str, url: str) -> str:
    """Build the 'core' Exec (binary + identity flags + URL). WMClass and
    --user-data-dir are added later by build_exec() from the form state.
    """
    binary = (binary or "vivaldi-stable").strip()
    if kind == "pwa":
        tail = f" --profile-directory=Default --app-id={app_id}" if app_id else ""
        return binary + tail
    if kind == "webapp":
        return binary + (f" --app={quote_exec_arg(url)}" if url else "")
    if kind == "sandboxed":
        parts = [binary, "--no-first-run", "--no-default-browser-check"]
        if url:
            parts.append(quote_exec_arg(url))
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
        d["url"] = app_url or extract_positional_url(d["exec"]) or d.get("x_webapp_url", "")
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


# ---------- xseticon wrapper handling ----------

def installed_wrapper_path() -> Path:
    return WRAPPER_INSTALL_DIR / WRAPPER_NAME


def install_icon_wrapper() -> Path:
    """Copy the wrapper script to ~/.local/bin and chmod +x. Idempotent."""
    dst = installed_wrapper_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not WRAPPER_SOURCE.exists():
        raise FileNotFoundError(
            f"Wrapper script not found next to the manager: {WRAPPER_SOURCE}"
        )
    # Always re-copy in case the source script in the repo was updated.
    shutil.copyfile(WRAPPER_SOURCE, dst)
    os.chmod(dst, 0o755)
    return dst


def icon_helpers_available() -> bool:
    """The Python wrapper needs python-xlib (Xlib module) importable."""
    try:
        import importlib
        importlib.import_module("Xlib")
        return True
    except Exception:
        return False


def resolve_icon_to_file(icon_field: str, app_id: str = "") -> str:
    """Resolve an Icon= value (absolute path or theme name) to a concrete file
    suitable for xseticon. Falls back to the largest PNG in the PWA's
    Manifest Resources directory if available."""
    if not icon_field:
        return find_best_icon_for_appid(app_id) if app_id else ""
    if os.path.isabs(icon_field) and os.path.isfile(icon_field):
        return icon_field
    try:
        info = Gtk.IconTheme.get_default().lookup_icon(icon_field, 256, 0)
        if info:
            fn = info.get_filename()
            if fn and os.path.isfile(fn):
                return fn
    except Exception:
        pass
    if app_id:
        return find_best_icon_for_appid(app_id)
    return ""


def peel_icon_wrapper(exec_line: str):
    """If exec_line invokes vivaldi-pwa-icon-wrap, return (icon, wmclass, inner).
    Otherwise (None, None, exec_line)."""
    if not exec_line:
        return None, None, exec_line
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        return None, None, exec_line
    if not tokens:
        return None, None, exec_line
    if os.path.basename(tokens[0]) != WRAPPER_NAME:
        return None, None, exec_line
    if len(tokens) < 5 or tokens[3] != "--":
        return None, None, exec_line
    icon, wmclass = tokens[1], tokens[2]
    inner = " ".join(_quote_token(t) for t in tokens[4:])
    return icon, wmclass, inner


def wrap_with_icon_helper(exec_line: str, icon_path: str, wm_class: str) -> str:
    wrapper = str(installed_wrapper_path())
    return (
        f"{shlex.quote(wrapper)} "
        f"{shlex.quote(icon_path)} "
        f"{shlex.quote(wm_class)} -- {exec_line}"
    )


# ---------- Icon fetching from a URL ----------


def _http_get(url, allow_insecure=False, max_bytes=MAX_ICON_BYTES):
    """Fetch URL with our user-agent + TLS-tolerance flag. Returns
    (content_type, bytes). Raises RuntimeError on error or oversize."""
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT})
    ctx = ssl.create_default_context()
    if allow_insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as r:
            ct = r.headers.get("Content-Type", "")
            data = r.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise RuntimeError(f"Response exceeded {max_bytes} bytes")
            return ct, data
    except urllib.error.URLError as e:
        raise RuntimeError(str(e.reason if hasattr(e, "reason") else e)) from e
    except Exception as e:
        raise RuntimeError(str(e)) from e


class _IconLinkExtractor(HTMLParser):
    """Scrape <link rel=icon/apple-touch-icon/manifest …> from a page head."""
    ICON_RELS = {
        "icon", "shortcut icon", "apple-touch-icon",
        "apple-touch-icon-precomposed", "mask-icon", "fluid-icon",
    }

    def __init__(self):
        super().__init__()
        self.icons = []
        self.manifest_url = None
        self.og_image = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "link":
            rel = a.get("rel", "").lower().strip()
            href = a.get("href", "")
            if not href:
                return
            if rel == "manifest":
                self.manifest_url = href
            elif rel in self.ICON_RELS or any(r in self.ICON_RELS for r in rel.split()):
                self.icons.append({
                    "href": href,
                    "sizes": a.get("sizes", ""),
                    "type": a.get("type", ""),
                })
        elif tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            if prop == "og:image":
                self.og_image = a.get("content", "")


def discover_icon_candidates(url, allow_insecure=False):
    """Given a page URL, return a list of icon candidate dicts with keys:
    href (absolute URL), sizes, type, source ('page' / 'manifest' / 'meta' /
    'fallback'). Manifest icons are followed when a <link rel=manifest> is
    present. Always appends /favicon.ico as a last-ditch fallback."""
    candidates = []
    try:
        ct, body = _http_get(url, allow_insecure)
    except Exception as e:
        raise RuntimeError(f"Could not fetch {url}: {e}") from e

    text = ""
    if "html" in ct.lower() or "text" in ct.lower() or not ct:
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            text = ""
    if text:
        ex = _IconLinkExtractor()
        try:
            ex.feed(text)
        except Exception:
            pass
        for i in ex.icons:
            candidates.append({
                "href": urllib.parse.urljoin(url, i["href"]),
                "sizes": i["sizes"],
                "type": i["type"],
                "source": "page",
            })
        if ex.manifest_url:
            mu = urllib.parse.urljoin(url, ex.manifest_url)
            try:
                _, mbody = _http_get(mu, allow_insecure)
                manifest = json.loads(mbody.decode("utf-8", errors="replace"))
            except Exception:
                manifest = None
            if isinstance(manifest, dict):
                for ic in manifest.get("icons", []) or []:
                    src = ic.get("src", "")
                    if not src:
                        continue
                    candidates.append({
                        "href": urllib.parse.urljoin(mu, src),
                        "sizes": ic.get("sizes", ""),
                        "type": ic.get("type", ""),
                        "source": "manifest",
                    })
        if ex.og_image:
            candidates.append({
                "href": urllib.parse.urljoin(url, ex.og_image),
                "sizes": "",
                "type": "",
                "source": "og:image",
            })

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme and parsed.netloc:
        favicon = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
        candidates.append({
            "href": favicon,
            "sizes": "",
            "type": "image/x-icon",
            "source": "fallback",
        })

    seen, deduped = set(), []
    for c in candidates:
        if c["href"] not in seen:
            seen.add(c["href"])
            deduped.append(c)
    return deduped


def _size_score(sizes_str):
    """Rough sort key: bigger declared sizes win."""
    if not sizes_str:
        return 0
    best = 0
    for token in sizes_str.lower().split():
        if "x" in token:
            try:
                w, h = token.split("x", 1)
                best = max(best, int(w) * int(h))
            except ValueError:
                pass
    return best


def rank_candidates(cands):
    """Sort candidates roughly best-first: manifest > page-link > og:image >
    fallback, then by declared size descending."""
    src_rank = {"manifest": 3, "page": 2, "og:image": 1, "fallback": 0}
    return sorted(
        cands,
        key=lambda c: (src_rank.get(c["source"], 0), _size_score(c["sizes"])),
        reverse=True,
    )


# ---------- Icon tinting (for symbolic + monocolor SVG icons) ----------

_SVG_PROTECTED_FILLS = {
    "none", "transparent", "white", "black",
    "#fff", "#ffffff", "#000", "#000000",
    "currentcolor",
}


def recolor_svg_text(svg_text: str, target_hex: str) -> str:
    """Best-effort: replace fill colours with target_hex, leaving structural
    ones (none / transparent / pure white / pure black) untouched. Works on
    `fill="…"` attributes and `style="…fill:…"` declarations. Doesn't try to
    parse CSS classes — multi-coloured SVGs come through partially recoloured
    (which is fine for the preview-and-decide UX)."""
    def repl_attr(m):
        val = m.group(1).strip()
        if val.lower() in _SVG_PROTECTED_FILLS:
            return m.group(0)
        return f'fill="{target_hex}"'

    def repl_style(m):
        val = m.group(1).strip()
        if val.lower() in _SVG_PROTECTED_FILLS:
            return m.group(0)
        return f"fill:{target_hex}"

    svg_text = re.sub(r'fill="([^"]+)"', repl_attr, svg_text)
    svg_text = re.sub(r"fill:\s*([^;\"\s}]+)", repl_style, svg_text)
    return svg_text


def render_recolored_svg(svg_path: str, target_hex: str, size: int):
    """Load svg_path, replace fills with target_hex, return a Pixbuf at size."""
    try:
        text = Path(svg_path).read_text()
    except Exception:
        return None
    colored = recolor_svg_text(text, target_hex)
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".svg", delete=False,
            dir=str(FETCHED_ICONS_DIR) if FETCHED_ICONS_DIR.exists() else None,
        ) as f:
            f.write(colored.encode("utf-8"))
            tmp = f.name
        try:
            return GdkPixbuf.Pixbuf.new_from_file_at_size(tmp, size, size)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except Exception:
        return None


def rgba_to_hex(rgba) -> str:
    return (
        f"#{int(rgba.red * 255):02x}"
        f"{int(rgba.green * 255):02x}"
        f"{int(rgba.blue * 255):02x}"
    )


def save_tinted_icon(icon_name: str, icon_info, rgba) -> str:
    """Save a coloured variant of an icon to FETCHED_ICONS_DIR.

    For symbolic icons we render with GTK's load_symbolic() and save the
    pixbuf as PNG. For non-symbolic SVGs we apply best-effort fill-
    replacement and save the modified SVG. Returns absolute path of the
    saved file, or empty string if the icon can't be tinted."""
    if icon_info is None:
        return ""
    FETCHED_ICONS_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", icon_name).strip("-").lower() or "icon"
    color_hex = rgba_to_hex(rgba)
    color_token = color_hex.lstrip("#")

    if icon_info.is_symbolic():
        try:
            pix, _ = icon_info.load_symbolic(rgba)
        except Exception:
            return ""
        dest = FETCHED_ICONS_DIR / f"{slug}-{color_token}.png"
        n = 1
        while dest.exists():
            n += 1
            dest = FETCHED_ICONS_DIR / f"{slug}-{color_token}-{n}.png"
        try:
            pix.savev(str(dest), "png", [], [])
            return str(dest)
        except Exception:
            return ""

    src_path = icon_info.get_filename() or ""
    if src_path.lower().endswith(".svg"):
        try:
            text = Path(src_path).read_text()
        except Exception:
            return ""
        colored = recolor_svg_text(text, color_hex)
        dest = FETCHED_ICONS_DIR / f"{slug}-{color_token}.svg"
        n = 1
        while dest.exists():
            n += 1
            dest = FETCHED_ICONS_DIR / f"{slug}-{color_token}-{n}.svg"
        try:
            dest.write_text(colored)
            return str(dest)
        except Exception:
            return ""
    return ""


def enumerate_icon_folders(max_depth=5):
    """Return a list of dicts {path, label, count, sample} for every folder
    under the known icon-theme roots that contains image files. Cheap
    pre-render: sample is the absolute path of one example image."""
    out = []
    seen = set()
    for root in ICON_THEME_ROOTS:
        if not os.path.isdir(root):
            continue
        root_norm = os.path.realpath(root)
        for dirpath, dirnames, filenames in os.walk(root):
            # Bound walk depth
            depth = dirpath[len(root):].count(os.sep)
            if depth > max_depth:
                del dirnames[:]
                continue
            icon_files = [
                f for f in filenames
                if f.lower().endswith((".png", ".svg", ".xpm"))
            ]
            if not icon_files:
                continue
            real = os.path.realpath(dirpath)
            if real in seen:
                continue
            seen.add(real)
            rel = os.path.relpath(dirpath, root)
            label = f"{os.path.basename(root_norm)}/{rel}" if rel != "." \
                else os.path.basename(root_norm)
            out.append({
                "path": dirpath,
                "label": label,
                "count": len(icon_files),
                "sample": os.path.join(dirpath, sorted(icon_files)[0]),
            })
    out.sort(key=lambda d: d["label"].lower())
    return out


def list_icon_files_in_folder(folder_path):
    """Return absolute paths of icon files directly in folder_path."""
    try:
        return sorted(
            os.path.join(folder_path, f)
            for f in os.listdir(folder_path)
            if f.lower().endswith((".png", ".svg", ".xpm"))
        )
    except OSError:
        return []


def save_fetched_icon(body, src_href, slug):
    """Write bytes to FETCHED_ICONS_DIR/<slug>.<ext>, picking a non-clashing
    name. Returns the absolute destination path."""
    FETCHED_ICONS_DIR.mkdir(parents=True, exist_ok=True)
    ext = ".png"
    path = urllib.parse.urlparse(src_href).path
    for cand in (".svg", ".ico", ".webp", ".jpg", ".jpeg", ".png"):
        if path.lower().endswith(cand):
            ext = cand
            break
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", slug).strip("-").lower() or "icon"
    dest = FETCHED_ICONS_DIR / f"{slug}{ext}"
    n = 1
    while dest.exists():
        n += 1
        dest = FETCHED_ICONS_DIR / f"{slug}-{n}{ext}"
    dest.write_bytes(body)
    return str(dest)


# ---------- UI ----------

class PWAManager(Gtk.Window):
    COL_PIXBUF, COL_NAME, COL_SUBTITLE, COL_PATH, COL_KIND = 0, 1, 2, 3, 4

    def __init__(self):
        super().__init__(title="Vivaldi PWA Manager")
        self.set_default_size(1240, 780)
        icon_path = SCRIPT_DIR / "docs" / "icon.svg"
        if icon_path.is_file():
            try:
                self.set_icon_from_file(str(icon_path))
            except Exception:
                self.set_icon_name("vivaldi")
        else:
            self.set_icon_name("vivaldi")
        self.items = []
        self.orphans = []
        self.current = None
        self._build_ui()
        self._install_accels()
        self.refresh()

    def _install_accels(self):
        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)
        accel.connect(
            Gdk.KEY_q, Gdk.ModifierType.CONTROL_MASK,
            Gtk.AccelFlags.VISIBLE, lambda *_: self.close() or True,
        )

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
        new_btn.connect("clicked", lambda *_: self._new_launcher())
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
        icon_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        icon_btn = Gtk.Button.new_from_icon_name("document-open-symbolic", Gtk.IconSize.BUTTON)
        icon_btn.set_tooltip_text("Browse for icon file")
        icon_btn.connect("clicked", self._browse_icon)
        fetch_btn = Gtk.Button.new_from_icon_name("emblem-downloads-symbolic", Gtk.IconSize.BUTTON)
        fetch_btn.set_tooltip_text("Fetch icons from a URL")
        fetch_btn.connect("clicked", lambda *_: self._fetch_icons_dialog())
        theme_btn = Gtk.Button.new_from_icon_name("view-grid-symbolic", Gtk.IconSize.BUTTON)
        theme_btn.set_tooltip_text("Browse installed system icons (theme names)")
        theme_btn.connect("clicked", lambda *_: self._icon_theme_browser_dialog())
        icon_btns.pack_start(icon_btn, False, False, 0)
        icon_btns.pack_start(fetch_btn, False, False, 0)
        icon_btns.pack_start(theme_btn, False, False, 0)
        add_row(1, "Icon", self.icon_entry, icon_btns)

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

        self.cb_icon_override = Gtk.CheckButton(
            label="Override alt-tab / taskbar icon via xseticon wrapper")
        self.cb_icon_override.set_tooltip_text(
            "Wraps the launcher in a helper that patches _NET_WM_ICON on the "
            "new window so alt-tab shows Icon instead of Chromium's default. "
            "Needs python3-xlib installed. X11 only."
        )
        win_box.pack_start(self.cb_icon_override, False, False, 0)

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
        self.duplicate_btn = Gtk.Button(label="Duplicate")
        self.duplicate_btn.set_tooltip_text(
            "Clone this launcher to a new .desktop file so you can tweak it "
            "(name, WMClass, isolated profile…) without touching the original."
        )
        self.duplicate_btn.connect("clicked", lambda *_: self._duplicate())
        self.delete_btn = Gtk.Button(label="Delete…")
        self.delete_btn.get_style_context().add_class("destructive-action")
        self.delete_btn.connect("clicked", lambda *_: self._delete())
        self.create_btn = Gtk.Button(label="Create Launcher")
        self.create_btn.connect("clicked", lambda *_: self._create_from_orphan())
        self.forget_btn = Gtk.Button(label="Forget orphan…")
        self.forget_btn.set_tooltip_text(
            "Delete Vivaldi's cached icons for this orphan PWA. Does NOT "
            "fully uninstall the PWA from Vivaldi — use vivaldi://apps for "
            "that."
        )
        self.forget_btn.connect("clicked", lambda *_: self._forget_orphan())
        self.vivaldi_apps_btn = Gtk.Button(label="Open vivaldi://apps")
        self.vivaldi_apps_btn.set_tooltip_text(
            "Opens Vivaldi's installed-apps page so you can fully uninstall "
            "a PWA from Vivaldi's registry."
        )
        self.vivaldi_apps_btn.connect("clicked",
                                      lambda *_: self._open_vivaldi_apps())
        btn_row.pack_start(self.launch_btn, False, False, 0)
        btn_row.pack_start(self.save_btn, False, False, 0)
        btn_row.pack_start(self.revert_btn, False, False, 0)
        btn_row.pack_start(self.duplicate_btn, False, False, 0)
        btn_row.pack_end(self.delete_btn, False, False, 0)
        btn_row.pack_end(self.create_btn, False, False, 0)
        btn_row.pack_end(self.forget_btn, False, False, 0)
        btn_row.pack_end(self.vivaldi_apps_btn, False, False, 0)
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
        raw_exec = d.get("exec", "")
        wrap_icon, wrap_class, inner_exec = peel_icon_wrapper(raw_exec)
        m = parse_exec(inner_exec)
        self.name_entry.set_text(d.get("name", ""))
        self.icon_entry.set_text(d.get("icon", ""))
        self.url_entry.set_text(d.get("url", ""))
        self.appid_entry.set_text(d.get("app_id", ""))
        self.wmclass_entry.set_text(d.get("wmclass") or wrap_class or m.get("wm_class", ""))
        self.exec_entry.set_text(m["core"])
        self.cb_icon_override.set_active(wrap_icon is not None)
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
        for cb in (self.cb_single_main, self.cb_icon_override, self.cb_isolated,
                   self.cb_incognito, self.cb_no_ext, self.cb_pwstore,
                   self.cb_dark, self.cb_no_display):
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
            self.cb_pwstore, self.cb_dark, self.cb_no_display, self.cb_icon_override,
            self.kind_combo,
        ]
        for w in widgets:
            w.set_sensitive(on)
        self.launch_btn.set_sensitive(on and not orphan)
        self.save_btn.set_sensitive(on and not orphan)
        self.revert_btn.set_sensitive(on and not orphan)
        self.delete_btn.set_sensitive(on and not orphan)
        self.duplicate_btn.set_sensitive(on and not orphan)
        self.create_btn.set_sensitive(on and orphan)
        self.forget_btn.set_sensitive(on and orphan)
        self.vivaldi_apps_btn.set_sensitive(True)  # always available

    def _update_icon_preview(self):
        pix = load_icon_pixbuf(self.icon_entry.get_text(), self.appid_entry.get_text(), 64)
        if pix:
            self.icon_img.set_from_pixbuf(pix)
        else:
            self.icon_img.set_from_icon_name("application-x-executable", Gtk.IconSize.DIALOG)

    def _on_isolated_toggled(self, cb):
        auto = isolated_profile_path_for(
            self.wmclass_entry.get_text(),
            self.appid_entry.get_text(),
        )
        if cb.get_active():
            # Only fill in the auto-path if udd is currently empty. Never
            # overwrite a path the user (or another tool) put there.
            if not self.udd_entry.get_text().strip():
                self.udd_entry.set_text(auto)
        else:
            # Only clear if it matches the auto-generated path
            if self.udd_entry.get_text() == auto:
                self.udd_entry.set_text("")

    # ---- kind switching ----
    def _on_kind_changed(self, _combo):
        """Reshape Exec for the newly-chosen kind, preserving URL / app-id.
        If required data is missing, show an error and revert the dropdown
        instead of silently writing a broken Exec."""
        if not self.current or self.current.get("_orphan"):
            return
        new_kind = self.kind_combo.get_active_id() or "vivaldi"
        if new_kind == "vivaldi":
            return
        prev_kind = self.current.get("kind", "vivaldi")
        url = self.url_entry.get_text().strip() or self.current.get("url", "")
        app_id = self.appid_entry.get_text().strip()

        # Prerequisite checks
        problem = None
        if new_kind == "pwa" and not app_id:
            problem = ("Switching to PWA needs a Chromium app-id, and this "
                       "launcher doesn't have one. PWAs come from Vivaldi "
                       "installing a site as an app — pick an orphan PWA "
                       "from the list, or change this launcher to a Web App "
                       "or Sandboxed window instead.")
        elif new_kind == "webapp" and not url:
            problem = ("Switching to Web App needs a URL — that's the entire "
                       "point of --app=URL. Fill in the URL field above "
                       "first, then change the Kind dropdown.")
        # Sandboxed without a URL is fine — it'll open Vivaldi at whatever
        # the profile's startup pages / pinned tabs set up.
        if problem:
            self._error(problem)
            # Revert the dropdown without re-triggering this handler
            self.kind_combo.handler_block_by_func(self._on_kind_changed)
            self.kind_combo.set_active_id(prev_kind)
            self.kind_combo.handler_unblock_by_func(self._on_kind_changed)
            return

        # Make sure the form's URL field reflects what we'll actually use.
        if url and self.url_entry.get_text().strip() != url:
            self.url_entry.set_text(url)

        core_tokens = shlex.split(self.exec_entry.get_text() or "")
        binary = core_tokens[0] if core_tokens else DEFAULT_BINARY
        new_core = build_core_for_kind(new_kind, binary, app_id, url)
        self.exec_entry.set_text(new_core)

        if new_kind == "sandboxed":
            if not self.wmclass_entry.get_text().strip() and self.current.get("name"):
                slug = re.sub(r"[^A-Za-z0-9]+", "",
                              self.current["name"]) or "VivaldiApp"
                self.wmclass_entry.set_text(slug)
            # Only auto-isolate if there's no profile dir yet — don't silently
            # overwrite an existing --user-data-dir and lose the user's state.
            if (not self.cb_isolated.get_active()
                    and not self.udd_entry.get_text().strip()):
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

    def _new_launcher(self):
        dlg = Gtk.Dialog(title="New launcher", transient_for=self, modal=True)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok_btn = dlg.add_button("Create", Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(6); box.set_margin_top(8)
        box.set_margin_start(10); box.set_margin_end(10); box.set_margin_bottom(8)
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        kind_combo = Gtk.ComboBoxText()
        kind_combo.append("vivaldi-install", "Install via Vivaldi  (recommended for PWAs)")
        kind_combo.append("sandboxed", "Sandboxed window  (full chrome, isolated profile)")
        kind_combo.append("webapp", "Web App  (chromeless --app=URL)")
        kind_combo.set_active_id("vivaldi-install")
        name_e = Gtk.Entry(placeholder_text="e.g. Portainer")
        url_e = Gtk.Entry(
            placeholder_text="https://…  (optional for Sandboxed window)")
        icon_e = Gtk.Entry(placeholder_text="Icon name or path (optional)")
        labels = {}
        rows = (("Kind", kind_combo), ("Name", name_e), ("URL", url_e), ("Icon", icon_e))
        for r, (text, w) in enumerate(rows):
            lbl = Gtk.Label(label=text, xalign=1)
            grid.attach(lbl, 0, r, 1, 1)
            grid.attach(w, 1, r, 1, 1)
            w.set_hexpand(True)
            labels[text] = lbl
        box.pack_start(grid, False, False, 0)

        note = Gtk.Label(xalign=0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("dim-label")
        box.pack_start(note, False, False, 0)

        # Screenshot showing the right-click → Progressive Web Apps menu path.
        # Only visible when "Install via Vivaldi" is the selected kind.
        screenshot_img = Gtk.Image()
        if INSTALL_SCREENSHOT.is_file():
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_size(
                    str(INSTALL_SCREENSHOT), 560, 320
                )
                screenshot_img.set_from_pixbuf(pix)
                screenshot_img.set_margin_top(6)
            except Exception:
                pass
        box.pack_start(screenshot_img, False, False, 0)

        def update_for_kind(*_):
            k = kind_combo.get_active_id()
            is_install = k == "vivaldi-install"
            if is_install:
                note.set_text(
                    "Opens Vivaldi (with the URL if you provide one, "
                    "otherwise just Vivaldi — pick a bookmark, paste a URL, "
                    "whatever). Then right-click the tab → Progressive Web "
                    "Apps → Install page as app… Vivaldi handles the "
                    "manifest, icons, extensions, and per-app settings. "
                    "After installing, click Refresh in this manager and "
                    "the new PWA shows up."
                )
                ok_btn.set_label("Open in Vivaldi")
            elif k == "sandboxed":
                note.set_text(
                    "Sandboxed: full Vivaldi window (tabs + address bar) with "
                    "its own Chromium profile and WM class. URL optional — "
                    "leave blank to open the profile cold and let pinned "
                    "tabs / startup pages decide what loads."
                )
                ok_btn.set_label("Create")
            else:
                note.set_text(
                    "Web App: chromeless --app=URL window. No tabs, no "
                    "address bar. Quickest way to app-ify a URL without "
                    "touching Vivaldi."
                )
                ok_btn.set_label("Create")
            for w in (name_e, icon_e):
                w.set_sensitive(not is_install)
            labels["Name"].set_sensitive(not is_install)
            labels["Icon"].set_sensitive(not is_install)
            screenshot_img.set_visible(is_install and screenshot_img.get_pixbuf() is not None)

        kind_combo.connect("changed", update_for_kind)

        dlg.show_all()
        # Apply initial state AFTER show_all so set_visible(False) takes effect
        update_for_kind()
        if dlg.run() == Gtk.ResponseType.OK:
            kind = kind_combo.get_active_id() or "sandboxed"
            name = name_e.get_text().strip()
            url = url_e.get_text().strip()
            icon = icon_e.get_text().strip()
            dlg.destroy()
            if kind == "vivaldi-install":
                if url and not url.startswith(("http://", "https://")):
                    url = "https://" + url
                cmd = [DEFAULT_BINARY] + ([url] if url else [])
                try:
                    subprocess.Popen(cmd, start_new_session=True)
                except Exception as e:
                    self._error(f"Couldn't open Vivaldi: {e}")
                    return
                info = Gtk.MessageDialog(
                    transient_for=self, message_type=Gtk.MessageType.INFO,
                    buttons=Gtk.ButtonsType.OK,
                    text="Install this page as a PWA in Vivaldi",
                )
                info.format_secondary_text(
                    "Right-click the tab → Progressive Web Apps → "
                    "Install page as app…\n\n"
                    "Once installed, click Refresh in this manager and the "
                    "new PWA will show up (as a launcher or an orphan you "
                    "can promote)."
                )
                info.run(); info.destroy()
                return
            if not name:
                self._error("Name is required.")
                return
            if kind == "webapp" and not url:
                self._error(
                    "Web App needs a URL — that's the --app=URL it gets "
                    "passed. Use Sandboxed window if you'd rather open the "
                    "profile without a starting page.")
                return
            self._create_new_launcher(kind, name, url, icon)
        else:
            dlg.destroy()

    def _create_new_launcher(self, kind, name, url, icon):
        slug = re.sub(r"[^A-Za-z0-9]+", "", name) or "VivaldiApp"
        slug_lower = slug.lower()
        out = APPS_DIR / f"vivaldi-{kind}-{slug_lower}.desktop"
        if out.exists():
            self._error(f"{out.name} already exists.")
            return

        wm_class = slug
        udd = isolated_profile_path_for(wm_class, "") if kind == "sandboxed" else ""
        core = build_core_for_kind(kind, DEFAULT_BINARY, "", url)
        model = {
            "opaque": False, "user_data_dir": udd,
            "window_state": "default", "window_size": "", "window_position": "",
            "wm_class": "", "wm_name": "",
            "incognito": False, "disable_extensions": False,
            "force_dark_mode": False, "password_store_basic": False,
            "lang": "", "proxy_server": "",
        }
        exec_line = build_exec(core, model, "", wm_class)
        if udd:
            ensure_isolated_profile_dir(udd)

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

    def _duplicate(self):
        if not self.current or self.current.get("_orphan"):
            return
        src = Path(self.current["path"])
        # Find a non-clashing destination
        stem = src.stem
        base_stem = re.sub(r"-copy(-\d+)?$", "", stem)
        n = 1
        while True:
            suffix = "-copy" if n == 1 else f"-copy-{n}"
            dst = src.with_name(f"{base_stem}{suffix}.desktop")
            if not dst.exists():
                break
            n += 1
        # Copy file, then bump the Name= so the user can spot it in menus
        try:
            shutil.copyfile(src, dst)
            os.chmod(dst, os.stat(src).st_mode)
            d = parse_desktop(dst)
            if d and "_cp" in d:
                cp = d["_cp"]
                sec = cp["Desktop Entry"]
                orig_name = sec.get("Name", base_stem)
                sec["Name"] = f"{orig_name} (copy)"
                write_desktop(dst, cp, d.get("_had_shebang", False))
            self._update_mime_cache()
            self._set_status(f"Duplicated → {dst.name}")
        except Exception as e:
            self._error(f"Duplicate failed: {e}")
            return
        self.refresh()
        self._reselect(str(dst))

    def _forget_orphan(self):
        if not self.current or not self.current.get("_orphan"):
            return
        app_id = self.current["app_id"]
        target = MANIFEST_DIR / app_id
        # Safety: only allow rmtree under MANIFEST_DIR
        try:
            target.resolve().relative_to(MANIFEST_DIR.resolve())
        except (ValueError, OSError):
            self._error("Refusing to delete a path outside Vivaldi's manifest dir.")
            return
        dlg = Gtk.MessageDialog(
            transient_for=self, message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Forget this orphan PWA?",
        )
        dlg.format_secondary_text(
            f"Deletes {target}\n\n"
            "This only removes Vivaldi's cached icons. Vivaldi may still have "
            "the PWA registered (it'll come back as an orphan if so). For a "
            "proper uninstall, use vivaldi://apps."
        )
        if dlg.run() == Gtk.ResponseType.OK:
            try:
                shutil.rmtree(target)
                self._set_status(f"Forgot {app_id}")
            except Exception as e:
                self._error(f"Could not delete: {e}")
            self.refresh()
        dlg.destroy()

    def _open_vivaldi_apps(self):
        try:
            subprocess.Popen([DEFAULT_BINARY, "vivaldi://apps"],
                             start_new_session=True)
            self._set_status("Opened vivaldi://apps")
        except Exception as e:
            self._error(f"Couldn't launch Vivaldi: {e}")

    # ---- icon fetch from URL ----
    def _fetch_icons_dialog(self):
        dlg = Gtk.Dialog(title="Fetch icons", transient_for=self, modal=True)
        dlg.set_default_size(700, 520)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        use_btn = dlg.add_button("Use selected", Gtk.ResponseType.OK)
        use_btn.set_sensitive(False)

        box = dlg.get_content_area()
        box.set_spacing(8); box.set_margin_top(10)
        box.set_margin_start(10); box.set_margin_end(10); box.set_margin_bottom(10)

        url_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        url_row.pack_start(Gtk.Label(label="Source URL:", xalign=0), False, False, 0)
        url_entry = Gtk.Entry()
        url_entry.set_text(self.url_entry.get_text().strip())
        url_entry.set_placeholder_text(
            "Page URL with the icons (e.g. portainer.io, github.com/foo/bar)"
        )
        url_row.pack_start(url_entry, True, True, 0)
        fetch_action = Gtk.Button(label="Fetch")
        url_row.pack_start(fetch_action, False, False, 0)
        box.pack_start(url_row, False, False, 0)

        insecure_cb = Gtk.CheckButton(label="Ignore TLS errors (use for self-signed certs)")
        box.pack_start(insecure_cb, False, False, 0)

        status = Gtk.Label(xalign=0)
        status.get_style_context().add_class("dim-label")
        box.pack_start(status, False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        flow.set_max_children_per_line(4)
        flow.set_min_children_per_line(2)
        flow.set_homogeneous(True)
        flow.set_valign(Gtk.Align.START)
        sw.add(flow)
        box.pack_start(sw, True, True, 0)

        state = {"href": None, "body": None}

        def clear_flow():
            for child in flow.get_children():
                flow.remove(child)
            state["href"] = None
            state["body"] = None
            use_btn.set_sensitive(False)

        def do_fetch(*_):
            clear_flow()
            u = url_entry.get_text().strip()
            if not u:
                status.set_text("Enter a URL.")
                return
            if not u.startswith(("http://", "https://")):
                u = "https://" + u
                url_entry.set_text(u)
            status.set_text(f"Fetching {u} …")
            # Force a UI redraw before the blocking HTTP work
            while Gtk.events_pending():
                Gtk.main_iteration()
            try:
                cands = discover_icon_candidates(u, insecure_cb.get_active())
            except Exception as e:
                status.set_text(f"Fetch failed: {e}")
                return
            if not cands:
                status.set_text("No icon candidates found.")
                return
            cands = rank_candidates(cands)[:16]  # cap to keep things sane
            status.set_text(f"Trying {len(cands)} candidate(s)…")
            while Gtk.events_pending():
                Gtk.main_iteration()

            shown = 0
            for c in cands:
                try:
                    _, body = _http_get(c["href"], insecure_cb.get_active())
                except Exception:
                    continue
                # Verify it's a renderable image; skip HTML 404 pages, etc.
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".img", delete=False, dir=str(FETCHED_ICONS_DIR)
                    if FETCHED_ICONS_DIR.exists() else None,
                )
                tmp.write(body); tmp.close()
                try:
                    pix = GdkPixbuf.Pixbuf.new_from_file_at_size(tmp.name, 96, 96)
                finally:
                    try:
                        os.unlink(tmp.name)
                    except OSError:
                        pass
                if not pix:
                    continue

                tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                tile.set_margin_top(4); tile.set_margin_bottom(4)
                tile.pack_start(Gtk.Image.new_from_pixbuf(pix), False, False, 0)
                meta = f"{c['sizes'] or '?'}  ·  {c['source']}"
                lbl = Gtk.Label(label=meta, xalign=0.5)
                lbl.get_style_context().add_class("dim-label")
                tile.pack_start(lbl, False, False, 0)
                tile._href = c["href"]
                tile._body = body
                flow.add(tile)
                shown += 1
                while Gtk.events_pending():
                    Gtk.main_iteration()
            flow.show_all()
            status.set_text(
                f"Found {shown} usable icon(s)."
                if shown else "Found candidates but none rendered as images."
            )

        def on_selection(fbox):
            children = fbox.get_selected_children()
            if not children:
                state["href"] = None
                state["body"] = None
                use_btn.set_sensitive(False)
                return
            tile = children[0].get_child()
            state["href"] = getattr(tile, "_href", None)
            state["body"] = getattr(tile, "_body", None)
            use_btn.set_sensitive(state["body"] is not None)

        fetch_action.connect("clicked", do_fetch)
        url_entry.connect("activate", do_fetch)
        flow.connect("selected-children-changed", on_selection)

        # Auto-fetch on open if we have a URL prefilled
        if url_entry.get_text().strip():
            GLib.idle_add(do_fetch)

        dlg.show_all()
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK and state["body"]:
            slug = self.name_entry.get_text().strip() or "icon"
            try:
                dest = save_fetched_icon(state["body"], state["href"], slug)
                self.icon_entry.set_text(dest)
                self._set_status(f"Saved icon → {dest}")
            except Exception as e:
                self._error(f"Could not save icon: {e}")
        dlg.destroy()

    # ---- system icon-theme browser ----
    def _icon_theme_browser_dialog(self):
        dlg = Gtk.Dialog(title="Browse system icons", transient_for=self, modal=True)
        dlg.set_default_size(820, 760)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        use_btn = dlg.add_button("Use selected", Gtk.ResponseType.OK)
        use_btn.set_sensitive(False)
        use_btn.set_can_default(True)
        dlg.set_default_response(Gtk.ResponseType.OK)

        box = dlg.get_content_area()
        box.set_spacing(8); box.set_margin_top(10)
        box.set_margin_start(10); box.set_margin_end(10); box.set_margin_bottom(10)

        # --- Top row: back + search + size ---
        search_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        back_btn = Gtk.Button.new_from_icon_name(
            "go-previous-symbolic", Gtk.IconSize.BUTTON)
        back_btn.set_tooltip_text("Back to the top-level list")
        back_btn.set_no_show_all(True)
        search_row.pack_start(back_btn, False, False, 0)
        search_row.pack_start(Gtk.Label(label="Search:", xalign=0), False, False, 0)
        search_entry = Gtk.Entry(
            placeholder_text="e.g. browser, mail, chat, vivaldi, web-, applications-")
        search_row.pack_start(search_entry, True, True, 0)
        size_combo = Gtk.ComboBoxText()
        for sz, label in (("32", "32px"), ("48", "48px"),
                          ("64", "64px"), ("96", "96px")):
            size_combo.append(sz, label)
        size_combo.set_active_id("48")
        size_combo.set_tooltip_text("Thumbnail size")
        search_row.pack_start(size_combo, False, False, 0)
        box.pack_start(search_row, False, False, 0)

        # --- Browse-by toggle (Categories | Folders) ---
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        mode_row.pack_start(Gtk.Label(label="Browse by:", xalign=0),
                            False, False, 0)
        rb_cat = Gtk.RadioButton.new_with_label_from_widget(None, "Categories")
        rb_folder = Gtk.RadioButton.new_with_label_from_widget(rb_cat, "Folders")
        for rb in (rb_cat, rb_folder):
            mode_row.pack_start(rb, False, False, 0)
        box.pack_start(mode_row, False, False, 0)

        status = Gtk.Label(xalign=0)
        status.get_style_context().add_class("dim-label")
        box.pack_start(status, False, False, 0)

        # --- Grid ---
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        flow.set_max_children_per_line(8)
        flow.set_min_children_per_line(3)
        flow.set_homogeneous(True)
        flow.set_valign(Gtk.Align.START)
        sw.add(flow)
        box.pack_start(sw, True, True, 0)

        # --- Preview / color row ---
        preview_frame = Gtk.Frame()
        preview_frame.set_margin_top(4)
        preview_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        preview_box.set_margin_top(8); preview_box.set_margin_bottom(8)
        preview_box.set_margin_start(10); preview_box.set_margin_end(10)
        preview_frame.add(preview_box)

        preview_img = Gtk.Image()
        preview_img.set_size_request(96, 96)
        preview_box.pack_start(preview_img, False, False, 0)

        preview_meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        preview_name = Gtk.Label(xalign=0, selectable=True)
        preview_meta.pack_start(preview_name, False, False, 0)

        color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        color_lbl = Gtk.Label(label="Tint:", xalign=0)
        color_row.pack_start(color_lbl, False, False, 0)
        color_btn = Gtk.ColorButton()
        color_btn.set_use_alpha(False)
        default_rgba = Gdk.RGBA(); default_rgba.parse("#3584e4")
        color_btn.set_rgba(default_rgba)
        color_row.pack_start(color_btn, False, False, 0)
        color_clear = Gtk.Button(label="Clear")
        color_clear.set_tooltip_text("Use the icon's native colors")
        color_row.pack_start(color_clear, False, False, 0)
        preview_meta.pack_start(color_row, False, False, 0)

        preview_note = Gtk.Label(xalign=0)
        preview_note.set_line_wrap(True)
        preview_note.get_style_context().add_class("dim-label")
        preview_meta.pack_start(preview_note, False, False, 0)
        preview_box.pack_start(preview_meta, True, True, 0)
        box.pack_start(preview_frame, False, False, 0)

        # --- Data ---
        theme = Gtk.IconTheme.get_default()
        all_names = sorted(theme.list_icons(None) or [])
        contexts = sorted(c for c in (theme.list_contexts() or []) if c)
        ctx_names = {c: sorted(theme.list_icons(c) or []) for c in contexts}
        folders_cache = {"data": None}

        def folders():
            if folders_cache["data"] is None:
                folders_cache["data"] = enumerate_icon_folders()
            return folders_cache["data"]

        # --- State ---
        # selected_kind: None | "theme" (selected is icon name) | "file" (path)
        state = {
            "selected_kind": None, "selected_value": None,
            "browse_mode": "categories",  # "categories" | "folders"
            "category": None, "folder": None,
            "color": None,            # Gdk.RGBA, or None to mean "no tint"
            "selected_info": None,    # Gtk.IconInfo for theme icons
            "debounce_id": 0,
            # Lazy-tinting state: tiles awaiting tinting + a generation counter
            # that invalidates in-flight idle callbacks when the colour changes.
            "pending_tint": [],
            "tint_generation": 0,
        }

        # --- Tile factories ---
        def _tinted_theme_pix(info, sz, color):
            """Try to render a theme icon tinted with `color`. Returns Pixbuf
            or None to mean 'fall back to native render'."""
            if color is None or not info:
                return None
            if info.is_symbolic():
                try:
                    pix, _ = info.load_symbolic(color)
                    if pix and (pix.get_width() != sz or pix.get_height() != sz):
                        pix = pix.scale_simple(
                            sz, sz, GdkPixbuf.InterpType.BILINEAR)
                    return pix
                except Exception:
                    return None
            fn = info.get_filename() or ""
            if fn.lower().endswith(".svg"):
                return render_recolored_svg(fn, rgba_to_hex(color), sz)
            return None

        def _tinted_file_pix(path, sz, color):
            if color is None or not path:
                return None
            if path.lower().endswith(".svg"):
                return render_recolored_svg(path, rgba_to_hex(color), sz)
            return None

        def make_theme_icon_tile(name, sz, color=None):
            info = theme.lookup_icon(name, sz, 0)
            if not info:
                return None
            pix = _tinted_theme_pix(info, sz, color)
            if pix is None:
                try:
                    pix = info.load_icon()
                    if pix.get_width() != sz or pix.get_height() != sz:
                        pix = pix.scale_simple(
                            sz, sz, GdkPixbuf.InterpType.BILINEAR)
                except Exception:
                    return None
            tile = _make_tile(Gtk.Image.new_from_pixbuf(pix), name)
            tile._kind = "theme"; tile._value = name
            return tile

        def make_file_icon_tile(path, sz, color=None):
            pix = _tinted_file_pix(path, sz, color)
            if pix is None:
                try:
                    pix = GdkPixbuf.Pixbuf.new_from_file_at_size(path, sz, sz)
                except Exception:
                    return None
            tile = _make_tile(Gtk.Image.new_from_pixbuf(pix),
                              os.path.basename(path))
            tile._kind = "file"; tile._value = path
            tile.set_tooltip_text(path)
            return tile

        def _make_tile(image_widget, caption):
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            tile.set_margin_top(4); tile.set_margin_bottom(4)
            tile.pack_start(image_widget, False, False, 0)
            lbl = Gtk.Label(label=caption, xalign=0.5)
            lbl.set_max_width_chars(18)
            lbl.set_ellipsize(3)
            lbl.get_style_context().add_class("dim-label")
            tile.pack_start(lbl, False, False, 0)
            tile.set_tooltip_text(caption)
            return tile

        def make_category_tile(ctx):
            example = None
            for candidate in ("folder", "applications-other",
                              "preferences-system", "image-x-generic"):
                if candidate in ctx_names.get(ctx, []):
                    example = candidate
                    break
            if example is None and ctx_names.get(ctx):
                example = ctx_names[ctx][len(ctx_names[ctx]) // 2]
            pix = None
            if example:
                info = theme.lookup_icon(example, 48, 0)
                if info:
                    try:
                        pix = info.load_icon()
                        if pix.get_width() != 48 or pix.get_height() != 48:
                            pix = pix.scale_simple(
                                48, 48, GdkPixbuf.InterpType.BILINEAR)
                    except Exception:
                        pix = None
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            tile.set_margin_top(8); tile.set_margin_bottom(8)
            if pix is not None:
                tile.pack_start(Gtk.Image.new_from_pixbuf(pix), False, False, 0)
            name_lbl = Gtk.Label()
            name_lbl.set_markup(f"<b>{GLib.markup_escape_text(ctx)}</b>")
            tile.pack_start(name_lbl, False, False, 0)
            count_lbl = Gtk.Label(
                label=f"{len(ctx_names.get(ctx, []))} icons", xalign=0.5)
            count_lbl.get_style_context().add_class("dim-label")
            tile.pack_start(count_lbl, False, False, 0)
            tile._kind = "category"; tile._value = ctx
            return tile

        def make_folder_tile(folder_dict):
            pix = None
            sample = folder_dict["sample"]
            if sample:
                try:
                    pix = GdkPixbuf.Pixbuf.new_from_file_at_size(sample, 48, 48)
                except Exception:
                    pix = None
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            tile.set_margin_top(8); tile.set_margin_bottom(8)
            if pix is not None:
                tile.pack_start(Gtk.Image.new_from_pixbuf(pix), False, False, 0)
            name_lbl = Gtk.Label()
            name_lbl.set_markup(
                f"<b>{GLib.markup_escape_text(folder_dict['label'])}</b>")
            name_lbl.set_max_width_chars(28)
            name_lbl.set_ellipsize(3)
            tile.pack_start(name_lbl, False, False, 0)
            count_lbl = Gtk.Label(
                label=f"{folder_dict['count']} files", xalign=0.5)
            count_lbl.get_style_context().add_class("dim-label")
            tile.pack_start(count_lbl, False, False, 0)
            tile.set_tooltip_text(folder_dict["path"])
            tile._kind = "folder"; tile._value = folder_dict["path"]
            return tile

        # --- Populate ---
        def populate():
            state["debounce_id"] = 0
            for child in flow.get_children():
                flow.remove(child)
            state["selected_kind"] = None
            state["selected_value"] = None
            state["selected_info"] = None
            use_btn.set_sensitive(False)
            update_preview()
            q = search_entry.get_text().strip().lower()
            at_top = state["category"] is None and state["folder"] is None
            back_btn.set_visible(not at_top)
            mode_row.set_visible(at_top)

            if at_top and not q:
                # Top-level: show categories OR folders
                if state["browse_mode"] == "categories":
                    for ctx in contexts:
                        flow.add(make_category_tile(ctx))
                    status.set_text(
                        f"Pick a category, or type to search across all "
                        f"{len(all_names)} icons.")
                else:
                    for f in folders():
                        flow.add(make_folder_tile(f))
                    status.set_text(
                        f"{len(folders())} folder(s) found under "
                        f"/usr/share/icons + ~/.local/share/icons.")
                flow.show_all()
                return False

            try:
                sz = int(size_combo.get_active_id() or "48")
            except ValueError:
                sz = 48

            # Bump the tint generation: any in-flight idle tinters checking
            # for this number will see it changed and abort.
            state["tint_generation"] += 1
            state["pending_tint"] = []
            tint_note = (f"  ·  tinted {rgba_to_hex(state['color'])}"
                         if state["color"] is not None else "")
            if state["folder"]:
                files = list_icon_files_in_folder(state["folder"])
                matches = [
                    p for p in files
                    if not q or q in os.path.basename(p).lower()
                ]
                cap = 240
                for path in matches[:cap]:
                    # Build the tile with NATIVE colours — fast — then queue
                    # for background tinting if a colour is active.
                    tile = make_file_icon_tile(path, sz)
                    if tile is not None:
                        flow.add(tile)
                        if state["color"] is not None:
                            state["pending_tint"].append(
                                (tile, path, "file", sz))
                cap_note = f" (showing first {cap})" if len(matches) > cap else ""
                if not matches:
                    status.set_text(
                        f"No files match '{q}' in {state['folder']}." if q
                        else f"No image files in {state['folder']}.")
                else:
                    status.set_text(
                        f"{len(matches)} file(s) in {state['folder']}"
                        f"{cap_note}{tint_note}.")
            else:
                cat = state["category"]
                source = ctx_names.get(cat, all_names) if cat else all_names
                matches = [n for n in source if q in n.lower()] if q else source
                cap = 240
                for name in matches[:cap]:
                    tile = make_theme_icon_tile(name, sz)
                    if tile is not None:
                        flow.add(tile)
                        if state["color"] is not None:
                            state["pending_tint"].append(
                                (tile, name, "theme", sz))
                cap_note = f" (showing first {cap})" if len(matches) > cap else ""
                scope = f" in {cat}" if cat else ""
                if not matches:
                    status.set_text(f"No icons match '{q}'{scope}.")
                elif q:
                    status.set_text(
                        f"{len(matches)} match(es) for '{q}'{scope}"
                        f"{cap_note}{tint_note}.")
                else:
                    status.set_text(
                        f"All icons in {cat}: {len(matches)}{cap_note}{tint_note}.")
            flow.show_all()
            if state["pending_tint"]:
                GLib.idle_add(tint_next_batch)
            return False

        # ---- Lazy tinter: prioritise visible tiles, work in batches ----

        def is_tile_visible(tile):
            alloc = tile.get_allocation()
            if alloc.height <= 0:
                return False  # not yet allocated
            # Tile's allocation is in FlowBox coords; FlowBox is inside the
            # scrolled window's viewport.
            adj = sw.get_vadjustment()
            top = adj.get_value()
            bot = top + adj.get_page_size()
            tile_top = alloc.y
            tile_bot = alloc.y + alloc.height
            return tile_bot >= top and tile_top <= bot

        def _swap_tile_image(tile, pix):
            for child in tile.get_children():
                if isinstance(child, Gtk.Image):
                    child.set_from_pixbuf(pix)
                    return

        def _render_tinted(value, kind, sz):
            if kind == "theme":
                info = theme.lookup_icon(value, sz, 0)
                return _tinted_theme_pix(info, sz, state["color"]) \
                    if info else None
            return _tinted_file_pix(value, sz, state["color"])

        def tint_next_batch():
            if state["color"] is None or not state["pending_tint"]:
                return False
            gen = state["tint_generation"]
            visible, offscreen = [], []
            for entry in state["pending_tint"]:
                if is_tile_visible(entry[0]):
                    visible.append(entry)
                else:
                    offscreen.append(entry)
            batch = visible[:8] if visible else offscreen[:4]
            for entry in batch:
                if state["tint_generation"] != gen \
                        or state["color"] is None:
                    return False
                tile, value, kind, sz = entry
                pix = _render_tinted(value, kind, sz)
                if pix is not None:
                    _swap_tile_image(tile, pix)
                try:
                    state["pending_tint"].remove(entry)
                except ValueError:
                    pass
            return bool(state["pending_tint"])

        # --- Preview / color ---
        def selected_source_svg_path():
            """If selected icon is a non-symbolic SVG file we can recolor,
            return its absolute path. Otherwise return ''."""
            if state["selected_kind"] == "theme" and state["selected_info"]:
                info = state["selected_info"]
                if not info.is_symbolic():
                    fn = info.get_filename() or ""
                    if fn.lower().endswith(".svg"):
                        return fn
            elif state["selected_kind"] == "file":
                p = state["selected_value"] or ""
                if p.lower().endswith(".svg"):
                    return p
            return ""

        def selected_is_symbolic():
            info = state["selected_info"]
            return bool(info and info.is_symbolic())

        def update_preview():
            kind = state["selected_kind"]
            if not kind:
                preview_img.clear()
                preview_name.set_text("")
                preview_note.set_text(
                    "Click any icon above to preview. "
                    "For symbolic / monocolor SVG icons you'll be able to pick "
                    "a tint colour.")
                for w in (color_btn, color_clear, color_lbl):
                    w.set_sensitive(False)
                return
            symbolic = selected_is_symbolic()
            recolorable_svg = bool(selected_source_svg_path())
            tintable = symbolic or recolorable_svg
            for w in (color_btn, color_lbl):
                w.set_sensitive(tintable)
            color_clear.set_sensitive(tintable and state["color"] is not None)

            pix = None
            color = state["color"] if tintable else None
            if symbolic and color is not None:
                try:
                    pix, _ = state["selected_info"].load_symbolic(color)
                except Exception:
                    pix = None
            elif recolorable_svg and color is not None:
                pix = render_recolored_svg(
                    selected_source_svg_path(), rgba_to_hex(color), 96)
            if pix is None:
                # Native render
                if kind == "theme":
                    info = state["selected_info"]
                    if info:
                        try:
                            pix = info.load_icon()
                            if pix.get_width() != 96 or pix.get_height() != 96:
                                pix = pix.scale_simple(
                                    96, 96, GdkPixbuf.InterpType.BILINEAR)
                        except Exception:
                            pix = None
                else:
                    try:
                        pix = GdkPixbuf.Pixbuf.new_from_file_at_size(
                            state["selected_value"], 96, 96)
                    except Exception:
                        pix = None
            if pix:
                preview_img.set_from_pixbuf(pix)
            else:
                preview_img.set_from_icon_name(
                    "image-missing", Gtk.IconSize.DIALOG)
            preview_name.set_text(str(state["selected_value"]))
            if symbolic:
                preview_note.set_text(
                    "Symbolic icon — tint colour fully supported.")
            elif recolorable_svg:
                preview_note.set_text(
                    "SVG — best-effort tint (multi-colour icons may keep some "
                    "of their original fills).")
            else:
                preview_note.set_text(
                    "Raster icon — tint not supported.")

        # --- Handlers ---
        def schedule_search(*_):
            if state["debounce_id"]:
                GLib.source_remove(state["debounce_id"])
            state["debounce_id"] = GLib.timeout_add(180, populate)

        def on_selection(fbox):
            children = fbox.get_selected_children()
            if not children:
                state["selected_kind"] = None
                state["selected_value"] = None
                state["selected_info"] = None
                use_btn.set_sensitive(False)
                update_preview()
                return
            tile = children[0].get_child()
            kind = getattr(tile, "_kind", None)
            if kind not in ("theme", "file"):
                state["selected_kind"] = None
                state["selected_value"] = None
                state["selected_info"] = None
                use_btn.set_sensitive(False)
                update_preview()
                return
            state["selected_kind"] = kind
            state["selected_value"] = tile._value
            if kind == "theme":
                state["selected_info"] = theme.lookup_icon(tile._value, 256, 0)
            else:
                state["selected_info"] = None
            use_btn.set_sensitive(True)
            update_preview()

        def on_activate(_fbox, child):
            tile = child.get_child()
            kind = getattr(tile, "_kind", None)
            if kind == "category":
                state["category"] = tile._value
                search_entry.set_text("")
                populate()
            elif kind == "folder":
                state["folder"] = tile._value
                search_entry.set_text("")
                populate()
            # For icon tiles: do NOT auto-close. Selecting an icon is what
            # populates the preview / enables the colour picker; user explicitly
            # clicks "Use selected" (or hits Enter, which fires it as the
            # default response) to commit. Otherwise picking a colour would
            # be impossible.

        def on_back(_btn):
            state["category"] = None
            state["folder"] = None
            search_entry.set_text("")
            populate()

        def on_mode_changed(_btn):
            if rb_cat.get_active():
                state["browse_mode"] = "categories"
            else:
                state["browse_mode"] = "folders"
            populate()

        def on_color_set(_btn):
            state["color"] = color_btn.get_rgba()
            update_preview()
            # Re-queue every visible tile for lazy tinting in the new colour,
            # without rebuilding the grid from scratch.
            _requeue_for_tint()

        def on_color_clear(_btn):
            state["color"] = None
            update_preview()
            # Color cleared — repaint every tile back to its native colours.
            _requeue_for_native()

        def _requeue_for_tint():
            state["tint_generation"] += 1
            state["pending_tint"] = []
            try:
                sz = int(size_combo.get_active_id() or "48")
            except ValueError:
                sz = 48
            for child in flow.get_children():
                tile = child.get_child()
                kind = getattr(tile, "_kind", None)
                value = getattr(tile, "_value", None)
                if kind in ("theme", "file") and value:
                    state["pending_tint"].append((tile, value, kind, sz))
            if state["pending_tint"]:
                GLib.idle_add(tint_next_batch)

        def _requeue_for_native():
            state["tint_generation"] += 1
            state["pending_tint"] = []
            try:
                sz = int(size_combo.get_active_id() or "48")
            except ValueError:
                sz = 48
            # Repaint each tile with its native pix (fast — no SVG fiddling)
            for child in flow.get_children():
                tile = child.get_child()
                kind = getattr(tile, "_kind", None)
                value = getattr(tile, "_value", None)
                if kind == "theme" and value:
                    info = theme.lookup_icon(value, sz, 0)
                    if not info:
                        continue
                    try:
                        pix = info.load_icon()
                        if pix.get_width() != sz or pix.get_height() != sz:
                            pix = pix.scale_simple(
                                sz, sz, GdkPixbuf.InterpType.BILINEAR)
                        _swap_tile_image(tile, pix)
                    except Exception:
                        pass
                elif kind == "file" and value:
                    try:
                        pix = GdkPixbuf.Pixbuf.new_from_file_at_size(
                            value, sz, sz)
                        _swap_tile_image(tile, pix)
                    except Exception:
                        pass

        search_entry.connect("changed", schedule_search)
        size_combo.connect("changed", schedule_search)
        flow.connect("selected-children-changed", on_selection)
        flow.connect("child-activated", on_activate)
        back_btn.connect("clicked", on_back)
        rb_cat.connect("toggled", on_mode_changed)
        rb_folder.connect("toggled", on_mode_changed)
        color_btn.connect("color-set", on_color_set)
        color_clear.connect("clicked", on_color_clear)
        # On scroll, wake the lazy tinter so newly-visible tiles get done first
        sw.get_vadjustment().connect(
            "value-changed",
            lambda *_: GLib.idle_add(tint_next_batch) if state["pending_tint"]
            else None,
        )

        current = self.icon_entry.get_text().strip()
        if current and not os.path.isabs(current):
            search_entry.set_text(current)
        else:
            populate()

        dlg.show_all()
        update_preview()
        # populate() ran before show_all; re-apply visibility now that widgets
        # are realized (handles the "hide mode_row on drill-in" case too)
        mode_row.set_visible(state["category"] is None and state["folder"] is None)
        back_btn.set_visible(state["category"] is not None or state["folder"] is not None)
        resp = dlg.run()
        if resp == Gtk.ResponseType.OK and state["selected_kind"]:
            kind = state["selected_kind"]
            value = state["selected_value"]
            color = state["color"]
            tinted_path = ""
            if color is not None:
                if kind == "theme":
                    info = state["selected_info"] or theme.lookup_icon(value, 256, 0)
                    tinted_path = save_tinted_icon(value, info, color)
                else:
                    # File-mode: build a pseudo IconInfo-like for save_tinted_icon
                    # by writing a temporary symlink? simpler: handle file SVG recolor
                    # inline.
                    if value.lower().endswith(".svg"):
                        try:
                            src = Path(value).read_text()
                            colored = recolor_svg_text(src, rgba_to_hex(color))
                            slug = re.sub(r"[^A-Za-z0-9_-]+", "-",
                                          Path(value).stem).strip("-").lower() \
                                          or "icon"
                            hex_token = rgba_to_hex(color).lstrip("#")
                            FETCHED_ICONS_DIR.mkdir(parents=True, exist_ok=True)
                            dest = FETCHED_ICONS_DIR / f"{slug}-{hex_token}.svg"
                            n = 1
                            while dest.exists():
                                n += 1
                                dest = FETCHED_ICONS_DIR / \
                                    f"{slug}-{hex_token}-{n}.svg"
                            dest.write_text(colored)
                            tinted_path = str(dest)
                        except Exception:
                            tinted_path = ""
            if tinted_path:
                self.icon_entry.set_text(tinted_path)
                self._set_status(f"Icon saved at {tinted_path}")
            elif kind == "theme":
                self.icon_entry.set_text(value)
                self._set_status(f"Icon set to '{value}'")
            else:
                self.icon_entry.set_text(value)
                self._set_status(f"Icon set to {value}")
        dlg.destroy()

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
        if self.cb_icon_override.get_active():
            wrapped = self._apply_icon_wrapper(new_exec)
            if wrapped is None:
                return  # error already shown
            new_exec = wrapped

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

    def _apply_icon_wrapper(self, exec_line):
        """Resolve the icon, install the helper if needed, and return a wrapped
        Exec line. Returns None and shows an error if any precondition fails."""
        if not icon_helpers_available():
            self._error(
                "Window-icon override needs python-xlib.\n"
                "Install it via:\n  sudo apt install python3-xlib\n"
                "(or pip install python-xlib in your environment)"
            )
            return None
        icon_path = resolve_icon_to_file(
            self.icon_entry.get_text().strip(),
            self.appid_entry.get_text().strip(),
        )
        if not icon_path:
            self._error(
                "Couldn't resolve Icon to a real file. Set Icon to an absolute "
                "path (use the Browse button) before enabling the override."
            )
            return None
        wm_class = (self.wmclass_entry.get_text().strip()
                    or self.name_entry.get_text().strip()
                    or "Vivaldi")
        try:
            install_icon_wrapper()
        except Exception as e:
            self._error(f"Could not install helper script: {e}")
            return None
        return wrap_with_icon_helper(exec_line, icon_path, wm_class)

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
