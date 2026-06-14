@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM  LTCtoLV1 — Windows build script
REM  Run from the project root: build.bat
REM ─────────────────────────────────────────────────────────────────────────

echo [1/3] Installing / upgrading PyInstaller...
pip install --upgrade pyinstaller
if errorlevel 1 (
    echo.
    echo ERROR: pip failed. Make sure Python is in PATH.
    pause & exit /b 1
)

echo.
echo [2/3] Generating icon (ltctolv1.ico)...
python make_icons.py
if errorlevel 1 (
    echo WARNING: icon generation failed — build will continue without an .ico
)

echo.
echo [3/3] Building LTCtoLV1.exe ...
python -m PyInstaller --clean ltctolv1.spec

echo.
if errorlevel 1 (
    echo *** BUILD FAILED — see output above ***
) else (
    echo ─────────────────────────────────────────────────────────────
    echo  SUCCESS:  dist\LTCtoLV1.exe
    echo ─────────────────────────────────────────────────────────────
)

pause
