#!/usr/bin/env bash
# Install (or refresh) a menu entry for Vivaldi PWA Manager.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/applications/vivaldi-pwa-manager.desktop"
ICON_SRC="$HERE/docs/icon.svg"
ICON_DEST="$HOME/.local/share/icons/hicolor/scalable/apps/vivaldi-pwa-manager.svg"

# Install the icon into the hicolor theme so it works in menus / alt-tab /
# panel pinning regardless of which theme the user has active. Falls back
# to the absolute path in Icon= if for some reason the copy fails.
ICON_FIELD="$ICON_SRC"
if [ -f "$ICON_SRC" ]; then
    mkdir -p "$(dirname "$ICON_DEST")"
    if cp "$ICON_SRC" "$ICON_DEST" 2>/dev/null; then
        ICON_FIELD="vivaldi-pwa-manager"
        command -v gtk-update-icon-cache >/dev/null && \
            gtk-update-icon-cache "$HOME/.local/share/icons/hicolor" \
            >/dev/null 2>&1 || true
    fi
fi

cat > "$DEST" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Vivaldi PWA Manager
Comment=Manage Vivaldi-launched PWA desktop entries
Exec=python3 $HERE/vivaldi_pwa_manager.py
Icon=$ICON_FIELD
Terminal=false
Categories=Utility;GTK;
StartupNotify=true
EOF

chmod +x "$DEST"
command -v update-desktop-database >/dev/null && \
  update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "Installed: $DEST"
echo "Icon: $ICON_FIELD"