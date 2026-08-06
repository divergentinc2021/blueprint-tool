#!/usr/bin/env python3
"""
Command-line entry point.

    blender -b --python cli.py -- --input model.fbx --out ./out

Shares blueprint_tool/core.py with the add-on, so the two cannot drift apart.
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# Blender runs this as a loose script, so the repo root is not importable by default.
# It also puts its own addons folder on sys.path and, if the add-on is installed and
# enabled, has ALREADY imported blueprint_tool from there — so without dropping that the
# command line silently runs the installed snapshot instead of the code in this repo.
for _m in [m for m in sys.modules
           if m == "blueprint_tool" or m.startswith("blueprint_tool.")]:
    del sys.modules[_m]
sys.path.insert(0, ROOT)

from blueprint_tool import core, render  # noqa: E402

if not os.path.dirname(os.path.abspath(core.__file__)).startswith(ROOT):
    raise SystemExit("error: imported blueprint_tool from %s, not this repo"
                     % core.__file__)


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser(prog="cli.py", description=__doc__)
    p.add_argument("--input", required=True)
    p.add_argument("--out", default="./blueprint_out")
    p.add_argument("--views", default="front,right,top,iso",
                   help="comma list from: " + ",".join(core.VIEW_ORDER))
    p.add_argument("--unit", default="m", choices=list(core.UNIT_FACTORS))
    p.add_argument("--page", default="a4l", choices=list(core.PAGES))
    p.add_argument("--theme", default="white", choices=render.THEME_ORDER,
                   help="blueprint/cyanotype = white lines on blue; sepia = parchment")
    p.add_argument("--plate", action="store_true",
                   help="lay every view out on ONE sheet instead of one page per view")
    p.add_argument("--grid", action="store_true",
                   help="faint graticule behind the drawing")
    p.add_argument("--margin", type=float, default=0.18)
    p.add_argument("--crease", type=float, default=140.0,
                   help="degrees; edges whose faces meet BELOW this become lines, so "
                        "HIGHER draws more interior detail")
    p.add_argument("--no-silhouette", action="store_true",
                   help="skip the computed outline of curved surfaces")
    p.add_argument("--hidden", action="store_true")
    p.add_argument("--no-dims", action="store_true")
    p.add_argument("--png", action="store_true")
    p.add_argument("--pdf", action="store_true", help="also write a multi-page PDF")
    # dimension sources
    p.add_argument("--dim-objects", action="store_true",
                   help="dimension each selected object's bounding box")
    p.add_argument("--dim-edges", action="store_true",
                   help="dimension selected mesh edges (length, angle, fitted circles)")
    p.add_argument("--dim-auto", action="store_true",
                   help="auto-detect circular features and dimension them")
    p.add_argument("--dim-empties", action="store_true",
                   help="dimension between Empties, paired in name order")
    p.add_argument("--no-overall", action="store_true",
                   help="omit the overall extents dimensions")
    p.add_argument("--radius", action="store_true",
                   help="circular features as radius instead of diameter")
    p.add_argument("--kinds", default=None,
                   help="comma list limiting dimension kinds: " + ",".join(__import__(
                       "blueprint_tool.annotate", fromlist=["KINDS"]).KINDS))
    p.add_argument("--title", default=None)
    return p.parse_args(argv)


def main():
    a = parse_args()
    try:
        core.generate(
            source=a.input, out_dir=a.out,
            views=[v.strip() for v in a.views.split(",") if v.strip()],
            unit=a.unit, page=a.page, theme=a.theme,
            layout=("plate" if a.plate else "sheet"), grid=a.grid,
            margin=a.margin, crease=a.crease,
            silhouette=not a.no_silhouette,
            hidden=a.hidden, dims=not a.no_dims, png=a.png, pdf=a.pdf, title=a.title,
            dim_overall=not a.no_overall, dim_objects=a.dim_objects,
            dim_edges=a.dim_edges, dim_auto=a.dim_auto, dim_empties=a.dim_empties,
            dim_radius_not_dia=a.radius,
            dim_kinds=([k.strip() for k in a.kinds.split(",")] if a.kinds else None),
        )
    except ValueError as e:
        raise SystemExit("error: %s" % e)


if __name__ == "__main__":
    main()
