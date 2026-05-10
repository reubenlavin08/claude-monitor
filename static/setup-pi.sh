#!/bin/bash
# One-shot Pi setup: install Chromium kiosk autostart, then reboot.
set -e

mkdir -p ~/.config/autostart

cat > ~/.config/autostart/claude-monitor.desktop <<'DESKTOPEOF'
[Desktop Entry]
Type=Application
Name=Claude Monitor
Exec=chromium-browser --kiosk --noerrdialogs --disable-infobars --no-first-run --incognito --disable-translate --disable-features=TranslateUI --check-for-update-interval=31536000 http://192.168.1.242:8765
X-GNOME-Autostart-enabled=true
DESKTOPEOF

echo "Autostart entry written. Rebooting in 3 seconds..."
sleep 3
sudo /sbin/reboot
