#!/bin/bash
# Install_OpenBOR.sh — Downloads and installs OpenBOR 7533 for MiSTer
#
# Run from MiSTer Scripts menu. Downloads all files from GitHub
# and sets up auto-launch. After install, just load the OpenBOR
# core from the console menu.
#

REPO="MiSTerOrganize/MiSTer_OpenBOR_7533"
BRANCH="main"
BASE_URL="https://raw.githubusercontent.com/$REPO/$BRANCH"

# Read current RBF name from version.txt manifest
RBF_NAME=$(wget -q -O - "$BASE_URL/version.txt" | tr -d '\r\n')
if [ -z "$RBF_NAME" ]; then
    echo "Error: Could not fetch version.txt"
    exit 1
fi

echo "=== OpenBOR 7533 Installer for MiSTer ==="
echo ""

# ── Kill ALL existing OpenBOR processes and daemons ─────────────────
killall OpenBOR 2>/dev/null
killall openbor_7533_daemon.sh 2>/dev/null
killall openbor_4086_daemon.sh 2>/dev/null
kill $(cat /tmp/openbor_7533_arm.pid 2>/dev/null) 2>/dev/null
rm -f /tmp/openbor_7533_arm.pid
rm -rf /tmp/openbor_7533_daemon.lock
sleep 1

# ── Download files from GitHub repo ───────────────────────────────
echo "Downloading OpenBOR 7533..."

mkdir -p /media/fat/_Other
mkdir -p /media/fat/games/OpenBOR_7533/Paks
mkdir -p /media/fat/logs/OpenBOR_7533
mkdir -p /media/fat/saves/OpenBOR_7533
mkdir -p /media/fat/savestates/OpenBOR_7533
mkdir -p /media/fat/config
mkdir -p /media/fat/config/inputs
mkdir -p /media/fat/docs/OpenBOR_7533

# Remove old log folders from games directory
rm -rf /media/fat/games/OpenBOR_7533/.Logs /media/fat/games/OpenBOR_7533/Logs

FAIL=0

echo "  Downloading FPGA core ($RBF_NAME)..."
# Remove old RBFs (this repo only — leave 4086 install untouched)
rm -f /media/fat/_Other/OpenBOR_7533_*.rbf
wget -q --show-progress -O "/media/fat/_Other/$RBF_NAME" "$BASE_URL/_Other/$RBF_NAME" || FAIL=1

echo "  Downloading ARM binary..."
wget -q --show-progress -O /media/fat/games/OpenBOR_7533/OpenBOR "$BASE_URL/games/OpenBOR_7533/OpenBOR" || FAIL=1

echo "  Downloading daemon..."
wget -q --show-progress -O /media/fat/games/OpenBOR_7533/openbor_7533_daemon.sh "$BASE_URL/games/OpenBOR_7533/openbor_7533_daemon.sh" || FAIL=1

echo "  Downloading README..."
wget -q --show-progress -O /media/fat/docs/OpenBOR_7533/README.md "$BASE_URL/docs/OpenBOR_7533/README.md" || FAIL=1

if [ "$FAIL" -ne 0 ]; then
    echo ""
    echo "Error: One or more downloads failed. Check your internet connection."
    exit 1
fi

# Make files executable
chmod +x /media/fat/games/OpenBOR_7533/OpenBOR
chmod +x /media/fat/games/OpenBOR_7533/openbor_7533_daemon.sh

# ── Install daemon into user-startup.sh ───────────────────────────
STARTUP=/media/fat/linux/user-startup.sh

# Remove ALL old OpenBOR_7533 daemon entries (preserve 4086 entry if present)
if [ -f "$STARTUP" ]; then
    sed -i '/openbor_7533_daemon\.sh/d' "$STARTUP"
    sed -i '/OpenBOR 7533 auto-launch/d' "$STARTUP"
fi

# Add single launcher line
echo "" >> "$STARTUP"
echo "# OpenBOR 7533 auto-launch daemon" >> "$STARTUP"
echo "/media/fat/games/OpenBOR_7533/openbor_7533_daemon.sh &" >> "$STARTUP"

echo "Auto-launcher installed."

# ── Start daemon now ──────────────────────────────────────────────
/media/fat/games/OpenBOR_7533/openbor_7533_daemon.sh &

echo ""
echo "=== OpenBOR 7533 installed successfully! ==="
echo ""
echo "Load the OpenBOR_7533 core from the console menu to play."
echo "Place .pak game modules in: games/OpenBOR_7533/Paks/"
echo ""
