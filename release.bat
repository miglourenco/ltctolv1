@echo off
REM -------------------------------------------------------------------------
REM  release.bat -- bump + commit + tag + build + GitHub release
REM
REM  Usage:   release.bat 1.0.1
REM
REM  Idempotent: safe to re-run. Skips steps that are already done. Builds
REM  the Windows .exe AND a .zip wrapper (the .zip helps work around the
REM  browser "this file is dangerous" download warning that Chrome / Edge
REM  show for unsigned PyInstaller .exe downloads), and uploads both to
REM  the GitHub release for that tag. Run release.sh on a Mac afterwards
REM  (or first -- order does not matter) to also build + attach the .dmg.
REM -------------------------------------------------------------------------

setlocal

if "%~1"=="" (
    echo Usage: release.bat ^<version^>
    echo    e.g. release.bat 1.0.1
    exit /b 1
)
set VERSION=%~1
set ZIPNAME=LTCtoLV1-v%VERSION%.zip

echo === [1/7] Sanity checks ===
git rev-parse --is-inside-work-tree >NUL 2>&1
if errorlevel 1 (
    echo ERROR: not inside a git work tree.
    exit /b 1
)

echo === [2/7] Bumping version in main_window.py + version_info.txt ===
python _bump_version.py %VERSION%
if errorlevel 1 exit /b 1

echo === [3/7] Building dist\LTCtoLV1.exe ===
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

echo === [4/7] Bundling dist\%ZIPNAME% (.exe + INSTALL.txt) ===
REM Use PowerShell's Compress-Archive — built into Windows since 10, no
REM external dep needed. Force overwrites any prior .zip from a re-run.
if exist dist\%ZIPNAME% del /q dist\%ZIPNAME%
powershell -NoProfile -Command "Compress-Archive -Path dist\LTCtoLV1.exe,INSTALL.txt -DestinationPath dist\%ZIPNAME% -Force"
if errorlevel 1 (
    echo ERROR: Compress-Archive failed.
    exit /b 1
)

echo === [5/7] Commit + push (if needed) ===
git add main_window.py version_info.txt
git diff --cached --quiet
if errorlevel 1 (
    git commit -m "chore: bump version to %VERSION%"
    if errorlevel 1 exit /b 1
) else (
    echo   nothing to commit
)
git push origin main
if errorlevel 1 exit /b 1

echo === [6/7] Tag v%VERSION% + push (if needed) ===
git rev-parse v%VERSION% >NUL 2>&1
if errorlevel 1 (
    git tag -a v%VERSION% -m "v%VERSION%"
    git push origin v%VERSION%
) else (
    echo   tag v%VERSION% already exists
)

echo === [7/7] GitHub release ===
gh release view v%VERSION% --repo miglourenco/ltctolv1 >NUL 2>&1
if errorlevel 1 (
    echo   creating release v%VERSION%
    gh release create v%VERSION% --repo miglourenco/ltctolv1 --title "v%VERSION%" --generate-notes dist\LTCtoLV1.exe dist\%ZIPNAME%
) else (
    echo   release v%VERSION% exists -- uploading .exe + .zip as assets (overwriting)
    gh release upload v%VERSION% dist\LTCtoLV1.exe dist\%ZIPNAME% --repo miglourenco/ltctolv1 --clobber
)

echo.
echo -------------------------------------------------------------
echo  Release v%VERSION% published.
echo  https://github.com/miglourenco/ltctolv1/releases/tag/v%VERSION%
echo -------------------------------------------------------------
endlocal
