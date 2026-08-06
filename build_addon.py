#!/usr/bin/env python3
"""
Zip blueprint_tool/ into an installable Blender add-on.

    python3 build_addon.py

Writes dist/blueprint_tool-<version>.zip, version read from bl_info so it cannot drift
from the add-on itself. Plain stdlib — runs on system Python, no Blender needed.
"""

import os
import re
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "blueprint_tool")
DIST = os.path.join(HERE, "dist")
SKIP_DIRS = {"__pycache__"}
SKIP_EXT = {".pyc", ".pyo"}


def version():
    src = open(os.path.join(PKG, "__init__.py")).read()
    m = re.search(r'"version"\s*:\s*\((\d+),\s*(\d+),\s*(\d+)\)', src)
    if not m:
        raise SystemExit("could not read version from bl_info")
    return ".".join(m.groups())


def main():
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, "blueprint_tool-%s.zip" % version())
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(PKG):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in sorted(files):
                if os.path.splitext(f)[1] in SKIP_EXT:
                    continue
                full = os.path.join(root, f)
                # paths inside the zip must start with the package dir for Blender to
                # install it as a module
                z.write(full, os.path.relpath(full, HERE))
                n += 1
    print("%s  (%d files, %.1f KB)" % (out, n, os.path.getsize(out) / 1024.0))


if __name__ == "__main__":
    main()
