#!/bin/bash
# Install unclutter and configure it to hide the cursor immediately on the
# kiosk display. Adds an autostart entry so it survives reboots.
set -e

echo "[1/3] Installing unclutter..."
sudo apt-get install -y unclutter

echo "[2/3] Adding unclutter autostart entry..."
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/unclutter.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Hide Cursor
Exec=unclutter -idle 0 -root
X-GNOME-Autostart-enabled=true
EOF

echo "[3/3] Killing any running unclutter and starting fresh (no reboot needed)..."
pkill unclutter 2>/dev/null || true
DISPLAY=:0 unclutter -idle 0 -root &> /dev/null &

echo "Done. Cursor should disappear immediately."
echo "If it doesn't, reboot the Pi (sudo /sbin/reboot) and it will hide on next login."
