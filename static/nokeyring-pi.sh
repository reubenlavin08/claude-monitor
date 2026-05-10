#!/bin/bash
# Stop the gnome-keyring "Unlock Keyring" popup that appears each boot.
#
# Two-part fix:
#   1. Add --password-store=basic to the chromium command so it doesn't
#      try to read/write gnome-keyring at all.
#   2. Wipe any existing keyring files so even if some other app pokes the
#      keyring, there's nothing locked to prompt about.
set -e

AUTOSTART=~/.config/autostart/claude-monitor.desktop

if grep -q -- "--password-store=basic" "$AUTOSTART"; then
    echo "[1/3] --password-store=basic flag already present, skipping"
else
    echo "[1/3] Adding --password-store=basic flag to chromium command"
    sed -i 's|chromium --kiosk|chromium --kiosk --password-store=basic|' "$AUTOSTART"
fi
echo "Updated autostart:"
cat "$AUTOSTART"
echo ""

echo "[2/3] Clearing any existing keyring data"
rm -rf ~/.local/share/keyrings/*

echo "[3/3] Rebooting in 3 seconds..."
sleep 3
sudo /sbin/reboot
