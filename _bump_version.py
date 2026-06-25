"""
Bump the version string in every file that carries one.

Called by release.bat / release.sh:    python _bump_version.py 1.2.3

Two files are touched:
  - main_window.py  →  _VERSION = "1.2.3"
  - version_info.txt →  filevers / prodvers / FileVersion / ProductVersion

main_window.py's _VERSION is what the controller exposes to the snapshot
(so the web remote shows it) and what the GitHub update checker compares.

version_info.txt is the VS_VERSION_INFO resource PyInstaller embeds into
the .exe header so Explorer / SmartScreen can present a publisher name
instead of "Unknown publisher" — important for avoiding the "this file
might be harmful" warnings on unsigned PyInstaller builds.
"""

from __future__ import annotations

import re
import sys


def bump(version: str) -> None:
    parts = (version.split(".") + ["0", "0", "0"])[:4]
    tup = "(" + ", ".join(parts) + ")"
    s4 = ".".join(parts)

    _patch(
        "main_window.py",
        [(r'_VERSION\s*=\s*"[^"]+"', f'_VERSION = "{version}"')],
        count=1,
    )
    _patch(
        "version_info.txt",
        [
            (r"filevers=\([^)]*\)", f"filevers={tup}"),
            (r"prodvers=\([^)]*\)", f"prodvers={tup}"),
            (r"(u'FileVersion',\s*u')[^']+(')",   _const(s4)),
            (r"(u'ProductVersion',\s*u')[^']+(')", _const(s4)),
        ],
    )
    print(f"  bumped to {version}  (file resource {s4})")


def _patch(path: str, subs, count: int = 0) -> None:
    with open(path, encoding="utf-8") as fh:
        s = fh.read()
    for pat, repl in subs:
        s = re.sub(pat, repl, s, count=count) if count else re.sub(pat, repl, s)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(s)


def _const(value: str):
    """Make a substitution that keeps groups 1 and 2 around the new value.
    Lambda form avoids backref-escape headaches when the value contains
    characters that re could mistake for backrefs."""
    return lambda m: m.group(1) + value + m.group(2)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python _bump_version.py <X.Y.Z>")
        sys.exit(1)
    bump(sys.argv[1])
