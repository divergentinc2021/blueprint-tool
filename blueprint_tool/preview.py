"""
Live viewport preview: draws a generated Sheet over the 3D view.

It consumes the same Sheet the SVG and PDF backends do, so the preview shows the actual
drawing rather than an approximation of it.

The Sheet is cached and redrawn each frame; it is NOT rebuilt on every redraw. Rebuilding
means baking Line Art, which needs a camera and mutates the scene — far too heavy to run
per frame, and it would fight the user's own edits. Refresh is therefore explicit, and the
panel says when the cached preview is stale.

Two honest differences from the file output: dashed hidden lines are drawn solid but
translucent, since GPU line drawing has no dash pattern; and text is drawn with Blender's
own font rather than Helvetica, so widths differ slightly.
"""

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from . import render

_handle = None
_state = {"sheet": None, "view": "", "stale": True}


# ---------------------------------------------------------------- cache
def set_sheet(sheet, view_name):
    _state["sheet"] = sheet
    _state["view"] = view_name
    _state["stale"] = False
    redraw()


def mark_stale():
    _state["stale"] = True
    redraw()


def status():
    return dict(_state, has=bool(_state["sheet"]))


def is_enabled():
    return _handle is not None


def redraw():
    for w in bpy.context.window_manager.windows:
        for area in w.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


# ---------------------------------------------------------------- drawing
def _fit(sheet, region, pad=16.0):
    """Letterbox the page into the region. Returns (scale, origin_x, origin_y)."""
    aw = max(region.width - 2 * pad, 1.0)
    ah = max(region.height - 2 * pad, 1.0)
    s = min(aw / sheet.px_w, ah / sheet.px_h)
    ox = (region.width - sheet.px_w * s) / 2.0
    oy = (region.height - sheet.px_h * s) / 2.0
    return s, ox, oy


def _rgba(hexcolour, alpha=1.0):
    r, g, b = render.hex_rgb(hexcolour)
    return (r, g, b, alpha)


def _set_font_size(size):
    # blf.size lost its dpi argument in 4.0; accept either signature
    try:
        blf.size(0, size)
    except TypeError:
        blf.size(0, size, 72)


def _draw():
    sheet = _state["sheet"]
    if sheet is None:
        return
    region = bpy.context.region
    if region is None:
        return
    s, ox, oy = _fit(sheet, region)

    def P(x, y):
        # sheet space is y-down from the page top; the region is y-up from its bottom
        return (ox + x * s, oy + (sheet.px_h - y) * s)

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    poly = gpu.shader.from_builtin("POLYLINE_UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")

    # --- filled shapes first, in sheet order
    tris = {}
    for op in sheet.ops:
        if op[0] == "rect" and op[5]:
            _, x, y, w, h, fill, _st, _wd = op
            a, b, c, d = P(x, y), P(x + w, y), P(x + w, y + h), P(x, y + h)
            tris.setdefault(fill, []).extend([a, b, c, a, c, d])
        elif op[0] == "polygon":
            _, pts, fill = op
            p = [P(*q) for q in pts]
            for i in range(1, len(p) - 1):
                tris.setdefault(fill, []).extend([p[0], p[i], p[i + 1]])
    for colour, verts in tris.items():
        shader.uniform_float("color", _rgba(colour))
        batch_for_shader(shader, "TRIS", {"pos": verts}).draw(shader)

    # --- lines, batched by colour and width so a 2000-stroke drawing is a handful of
    #     draw calls rather than one per stroke
    groups = {}
    for op in sheet.ops:
        if op[0] == "polyline":
            _, pts, stroke, width, dash = op
            key = (stroke, round(max(width * s, 1.0), 2), bool(dash))
            seg = groups.setdefault(key, [])
            prev = P(*pts[0])
            for q in pts[1:]:
                cur = P(*q)
                seg.append(prev)
                seg.append(cur)
                prev = cur
        elif op[0] == "rect" and op[6]:
            _, x, y, w, h, _f, stroke, width = op
            key = (stroke, round(max(width * s, 1.0), 2), False)
            seg = groups.setdefault(key, [])
            c = [P(x, y), P(x + w, y), P(x + w, y + h), P(x, y + h)]
            for i in range(4):
                seg.append(c[i])
                seg.append(c[(i + 1) % 4])

    poly.uniform_float("viewportSize", (region.width, region.height))
    for (colour, width, dashed), verts in groups.items():
        if not verts:
            continue
        poly.uniform_float("lineWidth", width)
        # no dash pattern in GPU line drawing; hidden lines read as translucent instead
        poly.uniform_float("color", _rgba(colour, 0.4 if dashed else 1.0))
        batch_for_shader(poly, "LINES", {"pos": verts}).draw(poly)

    # --- text
    for op in sheet.ops:
        if op[0] != "text":
            continue
        _, x, y, txt, size, fill, anchor, bold, rotate = op
        px = size * s
        if px < 5.0:            # unreadable at this zoom; drawing it is just noise
            continue
        _set_font_size(px)
        r, g, b = render.hex_rgb(fill)
        blf.color(0, r, g, b, 1.0)
        tw, th = blf.dimensions(0, txt)
        sx, sy = P(x, y)
        if rotate:
            blf.enable(0, blf.ROTATION)
            blf.rotation(0, 1.5707963)
            off = 0.0 if anchor == "start" else (-tw / 2.0 if anchor == "middle" else -tw)
            blf.position(0, sx, sy + off, 0)
            blf.draw(0, txt)
            blf.disable(0, blf.ROTATION)
        else:
            off = 0.0 if anchor == "start" else (-tw / 2.0 if anchor == "middle" else -tw)
            blf.position(0, sx + off, sy, 0)
            blf.draw(0, txt)

    gpu.state.blend_set("NONE")


# ---------------------------------------------------------------- handler
def enable():
    global _handle
    if _handle is None:
        _handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_PIXEL")
    redraw()


def disable():
    global _handle
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, "WINDOW")
        _handle = None
    redraw()


def toggle():
    if is_enabled():
        disable()
    else:
        enable()
    return is_enabled()
