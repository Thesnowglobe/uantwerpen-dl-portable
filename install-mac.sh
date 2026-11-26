#!/usr/bin/env bash
set -e
trap 'echo ""; echo "✗ Installation failed on macOS. Check your internet connection and re-run: bash install-mac.sh"; echo "  If Homebrew, Python, or the browser is missing, run: uantwerpen-dl doctor"; exit 1' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/uantwerpen-dl-app"
BIN_DIR="$HOME/.local/bin"
PLIST_FILE="$APP_DIR/com.uantwerpen.dailysync.plist"

echo "=== Installing UAntwerpen DL (macOS) ==="

# 1. System dependencies via Homebrew
echo "[1/5] Checking Homebrew, Python 3, and FFmpeg..."
if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not found. Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    if [ -x /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -x /usr/local/bin/brew ]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
fi

if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
fi
brew install python ffmpeg || true
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v python3 >/dev/null 2>&1; then
    echo "✗ Python 3 was not found. Install Python 3 from python.org or Homebrew, then re-run: bash install-mac.sh"
    exit 1
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

if [ ! -d "/Applications/Google Chrome.app" ]; then
    echo "Google Chrome not found; installing Playwright Chromium..."
    "$APP_DIR/.venv/bin/playwright" install chromium
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

PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
for rc in "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.bashrc"; do
    touch "$rc"
    if ! grep -q '\.local/bin' "$rc"; then
        echo "" >> "$rc"
        echo "# Added by uantwerpen-dl installer" >> "$rc"
        echo "$PATH_LINE" >> "$rc"
        echo "Added ~/.local/bin to PATH in $rc"
    fi
done

# 5. Stage the schedule outside LaunchAgents until the student opts in.
echo "[5/5] Preparing the optional macOS sync schedule..."
cat > "$PLIST_FILE" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.uantwerpen.dailysync</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BIN_DIR/uantwerpen-daily-sync</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>
</dict>
</plist>
EOF

echo ""
echo "✓ UAntwerpen DL installed successfully on macOS!"
echo ""
echo "Open a new terminal, then run:"
echo "  uantwerpen-sync run      (opens KU Leuven sign-in when needed)"
echo "  uantwerpen-sync enable   (optional daily schedule)"
echo "  uantwerpen-dl doctor     (if something is missing)"
echo ""
