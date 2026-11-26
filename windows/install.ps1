$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir   = Split-Path -Parent $ScriptDir
$AppDir    = Join-Path $env:LOCALAPPDATA "uantwerpen-dl-app"
$BinDir    = Join-Path $env:LOCALAPPDATA "uantwerpen-dl-bin"

function Find-RealPython {
    param([int]$MinMinor = 11)
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "${env:ProgramFiles(x86)}\Python3*\python.exe"
    )
    $matches = @()
    foreach ($pattern in $candidates) {
        $matches += Get-ChildItem $pattern -ErrorAction SilentlyContinue
    }
    try {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike "*WindowsApps*") {
            $matches += [pscustomobject]@{ FullName = $cmd.Source }
        }
    } catch {}
    $valid = @()
    foreach ($m in ($matches | Select-Object -ExpandProperty FullName -Unique)) {
        try {
            $ver = & $m -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$ver" -match "^3\.(\d+)$") {
                $minor = [int]$Matches[1]
                if ($minor -ge $MinMinor) {
                    $valid += [pscustomobject]@{ Path = $m; Minor = $minor }
                }
            }
        } catch {}
    }
    $best = $valid | Sort-Object Minor -Descending | Select-Object -First 1
    if ($best) { return $best.Path }
    return $null
}

Write-Host "=== Installing UAntwerpen DL (Windows) ===" -ForegroundColor Cyan

# 1. Check Python & FFmpeg (ignoring Microsoft Store WindowsApps stub)
Write-Host "[1/5] Checking Python 3 and FFmpeg..."
$RealPython = Find-RealPython -MinMinor 11
if (-not $RealPython) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Installing Python 3.12 via winget..."
        winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        $RealPython = Find-RealPython -MinMinor 11
    }
    if (-not $RealPython) {
        $RealPython = Find-RealPython -MinMinor 8
    }
    if (-not $RealPython) {
        Write-Error "Python 3 could not be installed automatically. Please install Python 3.12 from python.org (check 'Add python.exe to PATH') and re-run."
    }
}
Write-Host "Using Python: $RealPython"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Installing FFmpeg via winget..."
        winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    } else {
        Write-Warning "FFmpeg not found. Audio extraction may not work until FFmpeg is installed."
    }
}

# 2. Copy application files
Write-Host "[2/5] Copying application files to $AppDir..."
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Copy-Item (Join-Path $RootDir "uantwerpen_dl.py") (Join-Path $AppDir "uantwerpen_dl.py") -Force
Copy-Item (Join-Path $RootDir "requirements.txt") (Join-Path $AppDir "requirements.txt") -Force

function Invoke-Checked {
    param(
        [string]$StepName,
        [string]$RecoveryHint,
        [scriptblock]$Action
    )
    & $Action
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "Installation failed during: $StepName" -ForegroundColor Red
        Write-Host "What to do next: $RecoveryHint" -ForegroundColor Yellow
        exit 1
    }
}

# 3. Create venv and install packages
Write-Host "[3/5] Setting up Python virtual environment..."
$VenvDir = Join-Path $AppDir ".venv"
$VenvPy  = Join-Path $VenvDir "Scripts\python.exe"
$VenvPip = Join-Path $VenvDir "Scripts\pip.exe"
$VenvPw  = Join-Path $VenvDir "Scripts\playwright.exe"

Invoke-Checked "creating Python virtual environment" "Make sure Python 3.12 is installed from python.org, then double-click INSTALL.bat again." {
    & $RealPython -m venv $VenvDir
}
Invoke-Checked "upgrading pip" "Check your internet connection and re-run INSTALL.bat." {
    & $VenvPy -m pip install --upgrade pip -q
}
Invoke-Checked "installing Python packages" "Check your internet connection or firewall/antivirus, then re-run INSTALL.bat." {
    & $VenvPip install --upgrade -r (Join-Path $AppDir "requirements.txt")
}

$ChromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$HasChrome = $ChromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $HasChrome) {
    Write-Host "Google Chrome not found; installing Playwright Chromium..."
    Invoke-Checked "installing browser (Playwright Chromium)" "Install Google Chrome or check your internet connection, then re-run INSTALL.bat." {
        & $VenvPw install chromium
    }
} else {
    Write-Host "Google Chrome detected; skipping Chromium download."
}

# 4. Create CLI wrappers in %LOCALAPPDATA%\uantwerpen-dl-bin
Write-Host "[4/5] Creating CLI commands in $BinDir..."
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

@"
@echo off
set "PATH=$VenvDir\Scripts;%PATH%"
"$VenvPy" "$AppDir\uantwerpen_dl.py" %*
"@ | Set-Content (Join-Path $BinDir "uantwerpen-dl.bat") -Encoding ASCII

@"
@echo off
call "$BinDir\uantwerpen-dl.bat" login %*
"@ | Set-Content (Join-Path $BinDir "uantwerpen-login.bat") -Encoding ASCII

@"
@echo off
setlocal
if "%~1"=="" goto usage
if /i "%~1"=="run" goto run
if /i "%~1"=="now" goto run
if /i "%~1"=="enable" goto enable
if /i "%~1"=="disable" goto disable
if /i "%~1"=="status" goto status
if /i "%~1"=="follow" goto follow
goto usage

:run
call "$BinDir\uantwerpen-dl.bat" check >nul 2>&1
if errorlevel 3 exit /b 1
if errorlevel 2 (
    echo Login required. Opening KU Leuven login window...
    call "$BinDir\uantwerpen-login.bat"
    if errorlevel 1 exit /b 1
) else if errorlevel 1 (
    echo Could not check the Toledo session. Check your connection and retry.
    exit /b 1
)
call "$BinDir\uantwerpen-dl.bat" sync --defer-cpu-transcription --notify
exit /b %ERRORLEVEL%

:enable
call "$BinDir\uantwerpen-dl.bat" check >nul 2>&1
if errorlevel 3 exit /b 1
if errorlevel 2 (
    echo You are not logged in yet. Opening login window first...
    call "$BinDir\uantwerpen-login.bat"
    if errorlevel 1 exit /b 1
) else if errorlevel 1 (
    echo Could not check the Toledo session. Check your connection and retry.
    exit /b 1
)
schtasks /Create /TN "ToledoDailySync" /TR "\"$BinDir\uantwerpen-sync.bat\" run" /SC DAILY /ST 10:30 /IT /F >nul
if errorlevel 1 exit /b 1
echo Automatic daily sync at 10:30 ENABLED.
exit /b 0

:disable
schtasks /Delete /TN "ToledoDailySync" /F >nul 2>&1
echo Automatic daily sync DISABLED.
exit /b 0

:status
schtasks /Query /TN "ToledoDailySync" >nul 2>&1
if errorlevel 1 (
    echo Status: DISABLED
) else (
    echo Status: ENABLED ^(Daily at 10:30^)
)
call "$BinDir\uantwerpen-dl.bat" sync-status
exit /b 0

:follow
call "$BinDir\uantwerpen-dl.bat" sync-status --watch
exit /b %ERRORLEVEL%

:usage
echo Usage: uantwerpen-sync ^<run^|enable^|disable^|status^|follow^>
exit /b 0
"@ | Set-Content (Join-Path $BinDir "uantwerpen-sync.bat") -Encoding ASCII

# 5. Add BinDir to User PATH idempotently
Write-Host "[5/5] Adding $BinDir to User PATH..."
$UserPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
$NormBin = $BinDir.TrimEnd('\')
$AlreadyInPath = ($UserPath -split ";") | Where-Object { $_.Trim().TrimEnd('\') -ieq $NormBin }
if (-not $AlreadyInPath) {
    $NewPath = if ($UserPath) { "$UserPath;$BinDir" } else { $BinDir }
    [System.Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
}

Write-Host ""
Write-Host "UAntwerpen DL installed successfully on Windows!" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps (open a NEW Command Prompt or PowerShell window):"
Write-Host "  uantwerpen-sync run     (opens KU Leuven sign-in when needed)"
Write-Host "  uantwerpen-sync enable  (optional daily schedule)"
Write-Host "  uantwerpen-dl doctor    (if something is missing)"
Write-Host ""
