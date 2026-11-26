#!/usr/bin/env bash
set -e
trap 'echo ""; echo "✗ Installation failed. Check your internet connection and re-run: bash install.sh"; echo "  If a dependency or browser is missing, run: uantwerpen-dl doctor"; exit 1' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/uantwerpen-dl-app"
BIN_DIR="$HOME/.local/bin"
SYSTEMD_DIR="$HOME/.config/systemd/user"

echo "=== Installing UAntwerpen DL ==="

# 1. System dependencies on Ubuntu/Debian
if command -v apt-get >/dev/null 2>&1; then
    echo "[1/5] Installing system dependencies (python3-venv, ffmpeg, libnotify-bin)..."
    sudo apt-get update -qq
    sudo apt-get install -y python3 python3-venv python3-pip ffmpeg libnotify-bin
else
    echo "[1/5] Non-apt system detected; make sure python3, ffmpeg, and notify-send are installed."
fi

# 2. Copy application files
echo "[2/5] Copying application files to $APP_DIR..."
mkdir -p "$APP_DIR"
cp "$SCRIPT_DIR/uantwerpen_dl.py" "$APP_DIR/uantwerpen_dl.py"
cp "$SCRIPT_DIR/requirements.txt" "$APP_DIR/requirements.txt"
chmod +x "$APP_DIR/uantwerpen_dl.py"

# 3. Create Python virtual environment and install dependencies
echo "[3/5] Setting up Python virtual environment and installing packages..."
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
"$APP_DIR/.venv/bin/pip" install --upgrade -r "$APP_DIR/requirements.txt"

# Install Playwright Chromium if Google Chrome is not already installed
if ! command -v google-chrome >/dev/null 2>&1 && ! command -v google-chrome-stable >/dev/null 2>&1; then
    echo "Google Chrome not found; installing Playwright Chromium..."
    "$APP_DIR/.venv/bin/playwright" install chromium
    if command -v apt-get >/dev/null 2>&1; then
        "$APP_DIR/.venv/bin/playwright" install-deps chromium || true
    fi
else
    echo "Google Chrome detected; skipping Chromium download."
fi

# 4. Install CLI commands into ~/.local/bin
echo "[4/5] Installing commands into $BIN_DIR..."
mkdir -p "$BIN_DIR"
for cmd in uantwerpen uantwerpen-dl uantwerpen-login uantwerpen-daily-sync uantwerpen-sync; do
    cp "$SCRIPT_DIR/bin/$cmd" "$BIN_DIR/$cmd"
    chmod +x "$BIN_DIR/$cmd"
done

# Ensure ~/.local/bin is in PATH
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
touch "$HOME/.bashrc"
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    if [ -f "$rc" ] && ! grep -q '\.local/bin' "$rc"; then
        echo "" >> "$rc"
        echo "# Added by uantwerpen-dl installer" >> "$rc"
        echo "$PATH_LINE" >> "$rc"
        echo "Added ~/.local/bin to PATH in $rc"
    fi
done

# 5. Install systemd user timer
echo "[5/5] Setting up systemd user timer..."
mkdir -p "$SYSTEMD_DIR"
cp "$SCRIPT_DIR/systemd/uantwerpen-daily-sync.service" "$SYSTEMD_DIR/"
cp "$SCRIPT_DIR/systemd/uantwerpen-daily-sync.timer" "$SYSTEMD_DIR/"
systemctl --user daemon-reload 2>/dev/null || true

echo ""
echo "✓ UAntwerpen DL installed successfully!"
echo ""
echo "Open a new terminal, then run:"
echo "  uantwerpen-sync run      (opens KU Leuven sign-in when needed)"
echo "  uantwerpen-sync enable   (optional daily schedule)"
echo "  uantwerpen-dl doctor     (if something is missing)"
echo ""
