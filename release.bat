@echo off
REM -------------------------------------------------------------------------
REM  release.bat -- bump + commit + tag + build + GitHub release
REM
REM  Usage:   release.bat 1.0.1
REM
REM  Idempotent: safe to re-run. Skips steps that are already done. Builds
REM  the Windows .exe and uploads it to the GitHub release for that tag.
REM  Run release.sh on a Mac afterwards (or first -- order does not matter)
REM  to also build + attach the .dmg.
REM -------------------------------------------------------------------------

setlocal

if "%~1"=="" (
    echo Usage: release.bat ^<version^>
    echo    e.g. release.bat 1.0.1
    exit /b 1
)
set VERSION=%~1

echo === [1/6] Sanity checks ===
git rev-parse --is-inside-work-tree >NUL 2>&1
if errorlevel 1 (
    echo ERROR: not inside a git work tree.
    exit /b 1
)

echo === [2/6] Bumping _VERSION in main_window.py to %VERSION% (if needed) ===
python -c "import re,sys; p='main_window.py'; s=open(p,encoding='utf-8').read(); n=re.sub(r'_VERSION\s*=\s*\"[^\"]+\"', '_VERSION = \"%VERSION%\"', s, count=1); changed = (n != s); open(p,'w',encoding='utf-8',newline='').write(n); print('  bumped' if changed else '  already at %VERSION%, no change')"
if errorlevel 1 exit /b 1

echo === [3/6] Building dist\LTCtoLV1.exe ===
python -m pip install --upgrade pyinstaller pillow >NUL
if not exist ltctolv1.ico (
    python make_icons.py
)
python -m PyInstaller --clean --noconfirm ltctolv1.spec
if errorlevel 1 (
    echo ERROR: PyInstaller build failed.
    exit /b 1
)
if not exist dist\LTCtoLV1.exe (
    echo ERROR: build did not produce dist\LTCtoLV1.exe
    exit /b 1
)

echo === [4/6] Commit + push (if needed) ===
git add main_window.py
git diff --cached --quiet
if errorlevel 1 (
    git commit -m "chore: bump version to %VERSION%"
    if errorlevel 1 exit /b 1
) else (
    echo   nothing to commit
)
git push origin main
if errorlevel 1 exit /b 1

echo === [5/6] Tag v%VERSION% + push (if needed) ===
git rev-parse v%VERSION% >NUL 2>&1
if errorlevel 1 (
    git tag -a v%VERSION% -m "v%VERSION%"
    git push origin v%VERSION%
) else (
    echo   tag v%VERSION% already exists
)

echo === [6/6] GitHub release ===
gh release view v%VERSION% --repo miglourenco/ltctolv1 >NUL 2>&1
if errorlevel 1 (
    echo   creating release v%VERSION%
    gh release create v%VERSION% --repo miglourenco/ltctolv1 --title "v%VERSION%" --generate-notes dist\LTCtoLV1.exe
) else (
    echo   release v%VERSION% exists -- uploading LTCtoLV1.exe as asset (overwriting)
    gh release upload v%VERSION% dist\LTCtoLV1.exe --repo miglourenco/ltctolv1 --clobber
)

echo.
echo -------------------------------------------------------------
echo  Release v%VERSION% published.
echo  https://github.com/miglourenco/ltctolv1/releases/tag/v%VERSION%
echo -------------------------------------------------------------
endlocal
