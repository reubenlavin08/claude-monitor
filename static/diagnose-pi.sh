#!/bin/bash
# Pi-side diagnostic: figure out why the kiosk didn't autostart.

echo "=== Chromium binary ==="
which chromium-browser 2>&1
which chromium 2>&1

echo ""
echo "=== Display session type ==="
echo "XDG_SESSION_TYPE: ${XDG_SESSION_TYPE:-unset}"
echo "XDG_CURRENT_DESKTOP: ${XDG_CURRENT_DESKTOP:-unset}"
echo "WAYLAND_DISPLAY: ${WAYLAND_DISPLAY:-unset}"
echo "DISPLAY: ${DISPLAY:-unset}"

echo ""
echo "=== Autostart file ==="
ls -la ~/.config/autostart/ 2>&1
echo "---"
cat ~/.config/autostart/claude-monitor.desktop 2>&1

echo ""
echo "=== Boot config ==="
sudo raspi-config nonint get_boot_cli && echo "(0=desktop, 1=cli)"
sudo raspi-config nonint get_autologin && echo "(0=on, 1=off)"

echo ""
echo "=== Wayfire config (if Wayland) ==="
ls -la ~/.config/wayfire.ini 2>&1
echo "---"
grep -A2 "\[autostart\]" ~/.config/wayfire.ini 2>&1 || echo "(no [autostart] section)"

echo ""
echo "=== Recent journal for kiosk-relevant errors ==="
journalctl --user -n 20 --no-pager 2>&1 | tail -20
