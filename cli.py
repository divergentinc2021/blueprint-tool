#!/usr/bin/env python3
"""
Command-line entry point.

    blender -b --python cli.py -- --input model.fbx --out ./out

Shares blueprint_tool/core.py with the add-on, so the two cannot drift apart.
"""

import argparse
import os
import sys

# Blender runs this as a loose script, so the repo root is not importable by default.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from blueprint_tool import core  # noqa: E402


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser(prog="cli.py", description=__doc__)
    p.add_argument("--input", required=True)
    p.add_argument("--out", default="./blueprint_out")
    p.add_argument("--views", default="front,right,top,iso",
                   help="comma list from: " + ",".join(core.VIEW_ORDER))
    p.add_argument("--unit", default="m", choices=list(core.UNIT_FACTORS))
    p.add_argument("--page", default="a4l", choices=list(core.PAGES))
    p.add_argument("--margin", type=float, default=0.18)
    p.add_argument("--crease", type=float, default=140.0)
    p.add_argument("--hidden", action="store_true")
    p.add_argument("--no-dims", action="store_true")
    p.add_argument("--png", action="store_true")
    p.add_argument("--title", default=None)
    return p.parse_args(argv)


def main():
    a = parse_args()
    try:
        core.generate(
            source=a.input, out_dir=a.out,
            views=[v.strip() for v in a.views.split(",") if v.strip()],
            unit=a.unit, page=a.page, margin=a.margin, crease=a.crease,
            hidden=a.hidden, dims=not a.no_dims, png=a.png, title=a.title,
        )
    except ValueError as e:
        raise SystemExit("error: %s" % e)


if __name__ == "__main__":
    main()
