#!/bin/bash
# Install_OpenBOR.sh — Downloads and installs OpenBOR for MiSTer
#
# Run from MiSTer Scripts menu. Downloads all files from GitHub
# and sets up auto-launch. After install, just load the OpenBOR
# core from the console menu.
#

REPO="MiSTerOrganize/MiSTer_OpenBOR"
BRANCH="main"
BASE_URL="https://raw.githubusercontent.com/$REPO/$BRANCH"

# Read current RBF name from version.txt manifest
RBF_NAME=$(wget -q -O - "$BASE_URL/version.txt" | tr -d '\r\n')
if [ -z "$RBF_NAME" ]; then
    echo "Error: Could not fetch version.txt"
    exit 1
fi

echo "=== OpenBOR Installer for MiSTer ==="
echo ""

# ── Kill ALL existing OpenBOR processes and daemons ─────────────────
killall OpenBOR 2>/dev/null
killall openbor_4086_daemon.sh 2>/dev/null
kill $(cat /tmp/openbor_arm.pid 2>/dev/null) 2>/dev/null
rm -f /tmp/openbor_arm.pid
rm -rf /tmp/openbor_4086_daemon.lock
sleep 1

# ── Download files from GitHub repo ───────────────────────────────
echo "Downloading OpenBOR..."

mkdir -p /media/fat/_Other
mkdir -p /media/fat/games/OpenBOR_4086/Paks
mkdir -p /media/fat/logs/OpenBOR_4086
mkdir -p /media/fat/saves/OpenBOR_4086
mkdir -p /media/fat/savestates/OpenBOR_4086
mkdir -p /media/fat/config
mkdir -p /media/fat/config/inputs
mkdir -p /media/fat/docs/OpenBOR_4086

# Remove old log folders from games directory
rm -rf /media/fat/games/OpenBOR_4086/.Logs /media/fat/games/OpenBOR_4086/Logs

FAIL=0

echo "  Downloading FPGA core ($RBF_NAME)..."
# Remove old RBFs from both _Other and legacy _Console location
rm -f /media/fat/_Other/OpenBOR_*.rbf /media/fat/_Other/OpenBOR.rbf
rm -f /media/fat/_Console/OpenBOR_*.rbf /media/fat/_Console/OpenBOR.rbf
wget -q --show-progress -O "/media/fat/_Other/$RBF_NAME" "$BASE_URL/_Other/$RBF_NAME" || FAIL=1

echo "  Downloading ARM binary..."
wget -q --show-progress -O /media/fat/games/OpenBOR_4086/OpenBOR "$BASE_URL/games/OpenBOR_4086/OpenBOR" || FAIL=1

echo "  Downloading daemon..."
wget -q --show-progress -O /media/fat/games/OpenBOR_4086/openbor_4086_daemon.sh "$BASE_URL/games/OpenBOR_4086/openbor_4086_daemon.sh" || FAIL=1

echo "  Downloading README..."
wget -q --show-progress -O /media/fat/docs/OpenBOR_4086/README.md "$BASE_URL/docs/OpenBOR_4086/README.md" || FAIL=1

if [ "$FAIL" -ne 0 ]; then
    echo ""
    echo "Error: One or more downloads failed. Check your internet connection."
    exit 1
fi

# Make files executable
chmod +x /media/fat/games/OpenBOR_4086/OpenBOR
chmod +x /media/fat/games/OpenBOR_4086/openbor_4086_daemon.sh

# ── Install daemon into user-startup.sh ───────────────────────────
STARTUP=/media/fat/linux/user-startup.sh

# Remove ALL old OpenBOR daemon entries
if [ -f "$STARTUP" ]; then
    sed -i '/openbor_4086_daemon\.sh/d' "$STARTUP"
    sed -i '/OpenBOR auto-launch/d' "$STARTUP"
fi

# Add single launcher line
echo "" >> "$STARTUP"
echo "# OpenBOR auto-launch daemon" >> "$STARTUP"
echo "/media/fat/games/OpenBOR_4086/openbor_4086_daemon.sh &" >> "$STARTUP"

echo "Auto-launcher installed."

# ── Start daemon now ──────────────────────────────────────────────
/media/fat/games/OpenBOR_4086/openbor_4086_daemon.sh &

echo ""
echo "=== OpenBOR installed successfully! ==="
echo ""
echo "Load the OpenBOR core from the console menu to play."
echo "Place .pak game modules in: games/OpenBOR_4086/Paks/"
echo ""
