#!/bin/bash
# Fix the Pi setup: switch to desktop boot + autologin, correct chromium binary name.
set -e

echo "[1/3] Patching autostart entry: chromium-browser -> chromium"
sed -i 's/chromium-browser/chromium/g' ~/.config/autostart/claude-monitor.desktop
echo "Updated content:"
cat ~/.config/autostart/claude-monitor.desktop
echo ""

echo "[2/3] Switching boot mode to desktop autologin..."
sudo raspi-config nonint do_boot_behaviour B4

echo ""
echo "[3/3] Rebooting in 3 seconds..."
sleep 3
sudo /sbin/reboot
