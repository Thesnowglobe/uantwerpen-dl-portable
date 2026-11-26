import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_shell_scripts_syntax():
    scripts = [REPO_ROOT / "install.sh", REPO_ROOT / "install-mac.sh"] + sorted((REPO_ROOT / "bin").iterdir())
    for s in scripts:
        res = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True, check=False)
        assert res.returncode == 0, f"bash -n failed for {s}: {res.stderr}"


def test_path_export_idempotency(tmp_path):
    rc_file = tmp_path / ".bashrc"
    rc_file.write_text("# initial bashrc\n", encoding="utf-8")

    # Simulate running the PATH injection block from install.sh / install-mac.sh twice
    snippet = f"""
set -e
HOME="{tmp_path}"
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
for rc in "$HOME/.bashrc"; do
    if [ -f "$rc" ] && ! grep -q '\\.local/bin' "$rc"; then
        echo "" >> "$rc"
        echo "# Added by uantwerpen-dl installer" >> "$rc"
        echo "$PATH_LINE" >> "$rc"
    fi
done
"""
    subprocess.run(["bash", "-c", snippet], check=True)
    subprocess.run(["bash", "-c", snippet], check=True)

    content = rc_file.read_text(encoding="utf-8")
    assert content.count(".local/bin") == 1
    assert content.count("# Added by uantwerpen-dl installer") == 1


def test_windows_installer_and_systemd_consistency():
    ps1 = (REPO_ROOT / "windows" / "install.ps1").read_text(encoding="utf-8")
    assert "function Find-RealPython" in ps1
    assert "WindowsApps" in ps1
    assert "Python.Python.3.12 --scope user" in ps1
    assert "^(Daily at 10:30^)" in ps1
    assert "TrimEnd('\\')" in ps1
    assert "uantwerpen-dl.bat" in ps1

    assert (REPO_ROOT / "INSTALL.bat").exists()
    assert (REPO_ROOT / "windows" / "install.bat").exists()

    service = (REPO_ROOT / "systemd" / "uantwerpen-daily-sync.service").read_text(encoding="utf-8")
    assert "ExecStart=%h/.local/bin/uantwerpen-daily-sync" in service
    install_sh = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'BIN_DIR="$HOME/.local/bin"' in install_sh
    assert "uantwerpen-daily-sync" in install_sh


def test_macos_installer_stages_schedule_until_opt_in():
    installer = (REPO_ROOT / "install-mac.sh").read_text(encoding="utf-8")
    assert 'PLIST_FILE="$APP_DIR/com.uantwerpen.dailysync.plist"' in installer
    assert "$HOME/Library/LaunchAgents" not in installer
    assert "launchctl" not in installer

    manager = (REPO_ROOT / "bin" / "uantwerpen-sync").read_text(encoding="utf-8")
    opt_in = manager.split("enable|on|start)", 1)[1].split(";;", 1)[0]
    assert 'cp "$PLIST_TEMPLATE" "$PLIST_FILE"' in opt_in
    assert 'launchctl load -w "$PLIST_FILE"' in opt_in


def test_release_readiness_schedule_off_and_error_guards():
    install_sh = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    install_mac = (REPO_ROOT / "install-mac.sh").read_text(encoding="utf-8")
    ps1 = (REPO_ROOT / "windows" / "install.ps1").read_text(encoding="utf-8")
    toledo_sync = (REPO_ROOT / "bin" / "uantwerpen-sync").read_text(encoding="utf-8")

    # Scheduled sync must remain OFF by default across all 3 OS installers
    assert "systemctl --user enable" not in install_sh
    assert "launchctl load" not in install_mac
    assert "Library/LaunchAgents" not in install_mac
    # Windows installer only mentions schtasks inside the generated toledo-sync.bat template, never executes it during install
    ps1_before_bat = ps1.split(":enable")[0]
    assert "schtasks /Create" not in ps1_before_bat

    # Actionable error guards on installer failure
    assert "trap " in install_sh and "Installation failed" in install_sh
    assert "trap " in install_mac and "Installation failed" in install_mac
    assert "function Invoke-Checked" in ps1 and "$LASTEXITCODE -ne 0" in ps1

    # Enable distinguishes login required (2) from network/check errors (1), and macOS disable removes plist
    assert 'if [ "$CHECK_STATUS" -eq 2 ]' in toledo_sync
    assert 'rm -f "$PLIST_FILE"' in toledo_sync
    assert "Could not check the Toledo session" in ps1.split(":enable")[1]
