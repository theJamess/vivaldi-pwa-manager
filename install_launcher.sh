#!/usr/bin/env bash
# Install (or refresh) a menu entry for Vivaldi PWA Manager.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/applications/vivaldi-pwa-manager.desktop"

cat > "$DEST" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Vivaldi PWA Manager
Comment=Manage Vivaldi-launched PWA desktop entries
Exec=python3 $HERE/vivaldi_pwa_manager.py
Icon=vivaldi
Terminal=false
Categories=Utility;GTK;
StartupNotify=true
EOF

chmod +x "$DEST"
command -v update-desktop-database >/dev/null && \
  update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "Installed: $DEST"