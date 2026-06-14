#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# release.sh — bump + commit + tag + build + GitHub release
#
# Usage:   bash release.sh 1.0.1
#
# Idempotent: safe to re-run. Skips steps that are already done. Builds
# the macOS .app + .dmg and uploads the .dmg to the GitHub release for
# that tag. Run release.bat on Windows afterwards (or first — order
# doesn't matter) to also build + attach the .exe.
# ─────────────────────────────────────────────────────────────────────────
set -e

VERSION="${1:?Usage: bash release.sh <version>  (e.g. bash release.sh 1.0.1)}"
REPO="miglourenco/ltctolv1"

echo "=== [1/6] Sanity checks ==="
git rev-parse --is-inside-work-tree >/dev/null

echo "=== [2/6] Pulling latest (in case release.bat already bumped) ==="
git fetch origin
git pull --ff-only origin main || true   # don't die if branch is already up-to-date

echo "=== [3/6] Bumping _VERSION in main_window.py to ${VERSION} (if needed) ==="
python3 - <<PY
import re
p = "main_window.py"
s = open(p, encoding="utf-8").read()
n = re.sub(r'_VERSION\s*=\s*"[^"]+"', f'_VERSION = "${VERSION}"', s, count=1)
if n != s:
    open(p, "w", encoding="utf-8", newline="").write(n)
    print("  bumped")
else:
    print("  already at ${VERSION}, no change")
PY

echo "=== [4/6] Building dist/LTCtoLV1.app + dist/LTCtoLV1.dmg ==="
bash build.sh
if [ ! -f "dist/LTCtoLV1.dmg" ]; then
    echo "ERROR: build did not produce dist/LTCtoLV1.dmg"
    exit 1
fi

echo "=== [5/6] Commit + push + tag (if needed) ==="
git add main_window.py
if ! git diff --cached --quiet; then
    git commit -m "chore: bump version to ${VERSION}"
else
    echo "  nothing to commit"
fi
git push origin main
if git rev-parse "v${VERSION}" >/dev/null 2>&1; then
    echo "  tag v${VERSION} already exists locally"
else
    git tag -a "v${VERSION}" -m "v${VERSION}"
fi
git push origin "v${VERSION}" || true

echo "=== [6/6] GitHub release ==="
if gh release view "v${VERSION}" --repo "${REPO}" >/dev/null 2>&1; then
    echo "  release v${VERSION} exists — uploading LTCtoLV1.dmg as asset (overwriting)"
    gh release upload "v${VERSION}" "dist/LTCtoLV1.dmg" --repo "${REPO}" --clobber
else
    echo "  creating release v${VERSION}"
    gh release create "v${VERSION}" --repo "${REPO}" --title "v${VERSION}" --generate-notes "dist/LTCtoLV1.dmg"
fi

echo ""
echo "─────────────────────────────────────────────────────────────"
echo " Release v${VERSION} published."
echo " https://github.com/${REPO}/releases/tag/v${VERSION}"
echo "─────────────────────────────────────────────────────────────"
