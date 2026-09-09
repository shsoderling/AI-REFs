@echo off
REM Build AI REFs for Windows — produces an NSIS installer .exe
setlocal

set APP_NAME=AI REFs
set VERSION=1.3.0

echo === Building %APP_NAME% %VERSION% for Windows ===
echo.

REM ── 0. Clear any saved credentials from the Windows registry ──
echo --- Clearing saved credentials from registry (QSettings) ---
reg delete "HKCU\Software\AIREFs\AIREFs" /v ncbi_email /f >nul 2>&1
reg delete "HKCU\Software\AIREFs\AIREFs" /v ncbi_api_key /f >nul 2>&1
reg delete "HKCU\Software\AIREFs\AIREFs" /v anthropic_api_key /f >nul 2>&1
reg delete "HKCU\Software\AIREFs\AIREFs" /v orcid_id /f >nul 2>&1
echo Credentials cleared (if any existed).
echo.

REM ── 1. Install dependencies ──
cd /d "%~dp0\.."
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install Python dependencies.
    pause
    exit /b 1
)

REM Download spaCy model (optional — pipeline degrades gracefully without it)
python -m spacy download en_core_web_sm 2>nul || echo spaCy model download skipped (optional)

REM ── 2. Build with PyInstaller ──
cd packaging
pyinstaller ai_refs.spec --clean --noconfirm
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    pause
    exit /b 1
)

if not exist "dist\%APP_NAME%\%APP_NAME%.exe" (
    echo ERROR: PyInstaller did not produce the expected executable.
    pause
    exit /b 1
)

echo.
echo === PyInstaller build complete ===
echo.

REM ── 3. Create NSIS installer ──
where makensis >nul 2>nul
if errorlevel 1 (
    echo WARNING: NSIS not found. Install NSIS from https://nsis.sourceforge.io
    echo          Then add it to your PATH and re-run this script.
    echo.
    echo Standalone build is available at: dist\%APP_NAME%\%APP_NAME%.exe
    pause
    exit /b 0
)

echo === Creating installer with NSIS ===
makensis installer.nsi
if errorlevel 1 (
    echo ERROR: NSIS installer build failed.
    pause
    exit /b 1
)

echo.
echo === Build complete ===
echo Installer: dist\%APP_NAME% %VERSION% Setup.exe
echo.
echo Users run the Setup.exe, which installs to Program Files,
echo creates a Desktop shortcut and Start Menu entry.
echo.
pause
