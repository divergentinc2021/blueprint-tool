"""
Core of blueprint-tool: 3D model -> dimensions + orthographic blueprint drawings.

Imported by both the CLI (`cli.py`, run through `blender -b --python`) and the Blender
add-on (`__init__.py`). Everything here is plain functions over bpy — nothing registers.

The single entry point is `generate(**options)`; see DEFAULTS for the option set.

Line Art gives the linework. The SVG is written here rather than by Blender's Grease
Pencil exporter, whose output coordinates are degenerate on 5.0 — and writing it locally
means the world->pixel mapping is ours, so annotations land exactly.
"""

import datetime
import json
import math
import os

import bpy
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view

# ---------------------------------------------------------------- constants
# Blender is Z-up. Each view records which world axis maps to screen right / up so the
# projection stays explicit. `out` points from the model toward the camera.
VIEWS = {
    "front":  dict(rot=(90.0,   0.0,   0.0), right=(1, 0, 0),  up=(0, 0, 1),  out=(0, -1, 0)),
    "back":   dict(rot=(90.0,   0.0, 180.0), right=(-1, 0, 0), up=(0, 0, 1),  out=(0, 1, 0)),
    "right":  dict(rot=(90.0,   0.0,  90.0), right=(0, 1, 0),  up=(0, 0, 1),  out=(1, 0, 0)),
    "left":   dict(rot=(90.0,   0.0, -90.0), right=(0, -1, 0), up=(0, 0, 1),  out=(-1, 0, 0)),
    "top":    dict(rot=(0.0,    0.0,   0.0), right=(1, 0, 0),  up=(0, 1, 0),  out=(0, 0, 1)),
    "bottom": dict(rot=(180.0,  0.0,   0.0), right=(1, 0, 0),  up=(0, -1, 0), out=(0, 0, -1)),
    # true isometric: atan(1/sqrt(2)) elevation, 45 deg azimuth. Pictorial, not dimensioned.
    "iso":    dict(rot=(54.736, 0.0,  45.0), right=None,       up=None,       out=None),
}
VIEW_ORDER = ["front", "back", "left", "right", "top", "bottom", "iso"]
AXIS_LABEL = {(1, 0, 0): "X", (-1, 0, 0): "X", (0, 1, 0): "Y",
              (0, -1, 0): "Y", (0, 0, 1): "Z", (0, 0, -1): "Z"}

UNIT_FACTORS = {"m": 1.0, "cm": 100.0, "mm": 1000.0, "in": 39.3700787402, "ft": 3.28083989501}

# name -> (page_w_mm, page_h_mm, px_w, px_h); pixel sizes are 300 dpi
PAGES = {
    "a4l": (297.0, 210.0, 3508, 2480), "a4p": (210.0, 297.0, 2480, 3508),
    "a3l": (420.0, 297.0, 4961, 3508), "a3p": (297.0, 420.0, 3508, 4961),
    "a2l": (594.0, 420.0, 7016, 4961),
}

IMPORTABLE = (".fbx", ".blend", ".obj", ".glb", ".gltf", ".stl", ".ply",
              ".usd", ".usda", ".usdc", ".usdz")

INK = "#1a2b4c"
FAINT = "#8a97ad"

DEFAULTS = dict(
    source=None,            # path to import; ignored when use_current_scene is True
    use_current_scene=False,
    out_dir="./blueprint_out",
    views=("front", "right", "top", "iso"),
    unit="m",
    page="a4l",
    margin=0.18,
    crease=140.0,
    hidden=False,
    dims=True,
    png=False,
    title=None,
    log=print,
)


# ---------------------------------------------------------------- scene loading
def load(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
        return "blend"

    bpy.ops.wm.read_factory_settings(use_empty=True)
    import_into_current(path)
    return ext.lstrip(".")


def import_into_current(path):
    """Import a model into the scene as it stands. Used by the add-on's Import button."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fbx":
        # new C++ importer (4.5+) applies the file's own UnitScaleFactor
        bpy.ops.wm.fbx_import(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif ext == ".ply":
        bpy.ops.wm.ply_import(filepath=path)
    elif ext in (".usd", ".usda", ".usdc", ".usdz"):
        bpy.ops.wm.usd_import(filepath=path)
    elif ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
    else:
        raise ValueError("unsupported input extension: " + ext)
    return ext.lstrip(".")


def mesh_objects(scene=None):
    scene = scene or bpy.context.scene
    return [o for o in scene.objects
            if o.type == "MESH" and o.data and len(o.data.polygons) > 0
            and not o.name.startswith("BlueprintLineArt")]


class SceneGuard:
    """
    Save and restore everything generate() touches, and delete anything it creates.

    The CLI runs in a throwaway process and does not care. The add-on runs against a live
    scene the user is working in, where leaving stray cameras, a changed frame range or a
    swapped render engine behind would be its own bug.
    """

    def __init__(self, scene, active=True):
        self.scene = scene
        self.active = active
        self.created = []

    def track(self, obj):
        self.created.append(obj)
        return obj

    def __enter__(self):
        if not self.active:
            return self
        sc, r, sh = self.scene, self.scene.render, self.scene.display.shading
        self.saved = dict(
            camera=sc.camera,
            frame_start=sc.frame_start, frame_end=sc.frame_end, frame_current=sc.frame_current,
            engine=r.engine, res_x=r.resolution_x, res_y=r.resolution_y,
            res_pct=r.resolution_percentage, filepath=r.filepath,
            film_transparent=r.film_transparent,
            file_format=r.image_settings.file_format, color_mode=r.image_settings.color_mode,
            light=sh.light, color_type=sh.color_type, single_color=tuple(sh.single_color),
            outline=sh.show_object_outline, outline_color=tuple(sh.object_outline_color),
            world_color=tuple(sc.world.color) if sc.world else None,
        )
        return self

    def __exit__(self, *exc):
        for obj in reversed(self.created):
            try:
                data = getattr(obj, "data", None)
                bpy.data.objects.remove(obj, do_unlink=True)
                if data is not None and data.users == 0:
                    for coll in (bpy.data.cameras, bpy.data.grease_pencils_v3
                                 if hasattr(bpy.data, "grease_pencils_v3") else None):
                        if coll is not None:
                            try:
                                coll.remove(data)
                                break
                            except (TypeError, RuntimeError):
                                pass
            except (ReferenceError, RuntimeError):
                pass
        self.created = []

        if not self.active:
            return False
        sc, r, sh, s = self.scene, self.scene.render, self.scene.display.shading, self.saved
        sc.camera = s["camera"]
        sc.frame_start, sc.frame_end = s["frame_start"], s["frame_end"]
        sc.frame_current = s["frame_current"]
        r.engine = s["engine"]
        r.resolution_x, r.resolution_y = s["res_x"], s["res_y"]
        r.resolution_percentage, r.filepath = s["res_pct"], s["filepath"]
        r.film_transparent = s["film_transparent"]
        r.image_settings.file_format = s["file_format"]
        r.image_settings.color_mode = s["color_mode"]
        sh.light, sh.color_type = s["light"], s["color_type"]
        sh.single_color = s["single_color"]
        sh.show_object_outline = s["outline"]
        sh.object_outline_color = s["outline_color"]
        if s["world_color"] is not None and sc.world:
            sc.world.color = s["world_color"]
        return False


# ---------------------------------------------------------------- measurement
def world_corners(obj):
    return [obj.matrix_world @ Vector(c) for c in obj.bound_box]


def aabb(points):
    mn = Vector((float("inf"),) * 3)
    mx = Vector((float("-inf"),) * 3)
    for p in points:
        for i in range(3):
            mn[i] = min(mn[i], p[i])
            mx[i] = max(mx[i], p[i])
    return mn, mx


def measure(objs, factor, nd=4):
    def conv(v):
        return [round(c * factor, nd) for c in v]

    all_pts, per_object = [], []
    polys = verts = 0
    for o in objs:
        pts = world_corners(o)
        all_pts += pts
        omn, omx = aabb(pts)
        polys += len(o.data.polygons)
        verts += len(o.data.vertices)
        per_object.append({
            "name": o.name,
            "polygons": len(o.data.polygons),
            "vertices": len(o.data.vertices),
            # the object's bbox in its OWN orientation (includes scale). Truer part size
            # than the world AABB for anything rotated off-axis.
            "local_dimensions": conv(o.dimensions),
            "world_bbox_min": conv(omn),
            "world_bbox_max": conv(omx),
            "world_dimensions": conv(omx - omn),
            "location": conv(o.location),
            "rotation_euler_deg": [round(math.degrees(a), 3) for a in o.rotation_euler],
            "scale": [round(s, 6) for s in o.scale],
        })

    mn, mx = aabb(all_pts)
    d = mx - mn
    return {
        "bbox_min": conv(mn), "bbox_max": conv(mx),
        "dimensions": {"x": round(d.x * factor, nd), "y": round(d.y * factor, nd),
                       "z": round(d.z * factor, nd)},
        "width_x": round(d.x * factor, nd),
        "depth_y": round(d.y * factor, nd),
        "height_z": round(d.z * factor, nd),
        "diagonal": round(d.length * factor, nd),
        "centre": conv((mn + mx) / 2.0),
        "mesh_count": len(objs), "polygon_count": polys, "vertex_count": verts,
    }, per_object, mn, mx


# ---------------------------------------------------------------- camera
def make_camera(name, spec, mn, mx, res_x, res_y, margin, guard=None):
    cd = bpy.data.cameras.new(name)
    cd.type = "ORTHO"
    # HORIZONTAL fit fixes ortho_scale to mean "world units across the image width"
    # whatever the aspect. AUTO would flip that meaning on a portrait page.
    cd.sensor_fit = "HORIZONTAL"

    cam = bpy.data.objects.new(name, cd)
    bpy.context.scene.collection.objects.link(cam)
    if guard is not None:
        guard.track(cam)
    cam.rotation_euler = tuple(math.radians(a) for a in spec["rot"])
    # matrix_world is stale until the depsgraph runs. The iso view derives its axes from
    # it; reading it early yields the identity matrix and an empty drawing.
    bpy.context.view_layer.update()

    centre = (mn + mx) / 2.0
    diag = (mx - mn).length or 1.0
    aspect = res_x / res_y

    if spec["right"] is None:
        m = cam.matrix_world
        right = m.col[0].xyz.normalized()
        up = m.col[1].xyz.normalized()
        out = m.col[2].xyz.normalized()
        axes = ("", "")
    else:
        right, up, out = Vector(spec["right"]), Vector(spec["up"]), Vector(spec["out"])
        axes = (AXIS_LABEL[tuple(spec["right"])], AXIS_LABEL[tuple(spec["up"])])

    corners = [Vector((x, y, z)) for x in (mn.x, mx.x)
               for y in (mn.y, mx.y) for z in (mn.z, mx.z)]
    rs = [(c - centre).dot(right) for c in corners]
    us = [(c - centre).dot(up) for c in corners]
    ds = [(c - centre).dot(out) for c in corners]
    keep = 1.0 - 2.0 * margin
    ortho = max((max(rs) - min(rs)) / keep, ((max(us) - min(us)) / keep) * aspect)
    cd.ortho_scale = ortho

    dist = max(ds) + diag
    cam.location = centre + out * dist
    cd.clip_start = 0.001
    cd.clip_end = dist + diag * 4.0
    bpy.context.view_layer.update()

    return cam, {
        "ortho_scale_world": round(ortho, 6),
        "resolution": [res_x, res_y],
        "px_per_world_unit": round(res_x / ortho, 6),
        "screen_right_axis": [round(v, 6) for v in right],
        "screen_up_axis": [round(v, 6) for v in up],
        "screen_right_label": axes[0], "screen_up_label": axes[1],
        "camera_location": [round(v, 6) for v in cam.location],
        "view_centre_world": [round(v, 6) for v in centre],
        "projection": "px = world_to_camera_view(scene, cam, P); "
                      "x = px.x * res_x; y = (1 - px.y) * res_y",
    }


# ---------------------------------------------------------------- line art
def make_lineart(crease_deg, guard=None):
    bpy.ops.object.grease_pencil_add(type="LINEART_SCENE", align="WORLD", location=(0, 0, 0))
    gp = bpy.context.object
    gp.name = "BlueprintLineArt"
    if guard is not None:
        guard.track(gp)
    mod = next((m for m in gp.modifiers if m.type == "LINEART"), None)
    if mod is None:
        raise RuntimeError("Line Art modifier was not created")

    for k, v in (("use_contour", True), ("use_crease", True), ("use_material", True),
                 ("use_intersection", True), ("use_edge_mark", True), ("use_loose", False),
                 ("crease_threshold", math.radians(crease_deg)),
                 ("use_overlap_edge_type_support", True), ("smooth_tolerance", 0.0)):
        if hasattr(mod, k):
            try:
                setattr(mod, k, v)
            except Exception:
                pass
    return gp, mod


def bake_strokes(gp):
    bpy.ops.object.select_all(action="DESELECT")
    gp.select_set(True)
    bpy.context.view_layer.objects.active = gp
    try:
        bpy.ops.object.lineart_clear()
    except Exception:
        pass
    bpy.ops.object.lineart_bake_strokes()


def read_strokes(gp, scene, cam, res_x, res_y):
    """Return [[(x_px, y_px), ...], ...] for the baked Line Art at the current frame."""
    polylines = []
    mw = gp.matrix_world
    for layer in gp.data.layers:
        if not layer.frames:
            continue
        # A bake leaves an empty keyframe at 0 alongside the real one, so index 0 is the
        # wrong frame. Take the current frame; fall back to whichever holds the most points.
        frame = next((f for f in layer.frames if f.frame_number == scene.frame_current), None)
        if frame is None:
            frame = max(layer.frames, key=lambda f: len(f.drawing.curve_offsets))
        drawing = frame.drawing
        pos = drawing.attributes.get("position")
        if pos is None:
            continue
        # GPv3 stores points flat; curve_offsets[i]..curve_offsets[i+1] slices stroke i.
        offsets = [o.value for o in drawing.curve_offsets]
        cyclic_attr = drawing.attributes.get("cyclic")
        cyclic = [c.value for c in cyclic_attr.data] if cyclic_attr else []

        projected = []
        for p in pos.data:
            n = world_to_camera_view(scene, cam, mw @ Vector(p.vector))
            projected.append((n.x * res_x, (1.0 - n.y) * res_y))

        for i in range(len(offsets) - 1):
            a, b = offsets[i], offsets[i + 1]
            if b - a < 2:
                continue
            line = projected[a:b]
            if i < len(cyclic) and cyclic[i]:
                line = line + [line[0]]
            polylines.append(line)
    return polylines


# ---------------------------------------------------------------- svg
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def fmt(v, unit):
    if unit in ("mm", "cm"):
        return ("%.1f" % v).rstrip("0").rstrip(".")
    return "%.3f" % v


def path_d(points):
    out = ["M%.2f,%.2f" % points[0]]
    for x, y in points[1:]:
        out.append("L%.2f,%.2f" % (x, y))
    return "".join(out)


def dim_group(bbox_px, w_val, h_val, unit, w_lbl, h_lbl, fs, pad):
    """Horizontal dimension under the model, vertical dimension to its left."""
    x0, y0, x1, y1 = bbox_px
    g = ['<g id="dimensions" fill="%s" stroke="%s" stroke-width="2" '
         'font-family="Helvetica,Arial,sans-serif" font-size="%.0f">' % (INK, INK, fs)]
    ah = fs * 0.42

    def arrow(x, y, dx, dy):
        px, py = -dy, dx
        return ('<polygon points="%.1f,%.1f %.1f,%.1f %.1f,%.1f" stroke="none"/>'
                % (x, y, x - dx * ah * 2 + px * ah * 0.5, y - dy * ah * 2 + py * ah * 0.5,
                   x - dx * ah * 2 - px * ah * 0.5, y - dy * ah * 2 - py * ah * 0.5))

    dy = y1 + pad
    g.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.5"/>'
             % (x0, y1, x0, dy + fs * 0.6, FAINT))
    g.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.5"/>'
             % (x1, y1, x1, dy + fs * 0.6, FAINT))
    g.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>' % (x0, dy, x1, dy))
    g.append(arrow(x0, dy, -1, 0))
    g.append(arrow(x1, dy, 1, 0))
    g.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#ffffff" stroke="none"/>'
             % ((x0 + x1) / 2 - fs * 3.2, dy - fs * 0.78, fs * 6.4, fs * 1.15))
    g.append('<text x="%.1f" y="%.1f" text-anchor="middle" stroke="none">%s %s</text>'
             % ((x0 + x1) / 2, dy + fs * 0.33, fmt(w_val, unit), unit))
    if w_lbl:
        g.append('<text x="%.1f" y="%.1f" text-anchor="middle" stroke="none" fill="%s" '
                 'font-size="%.0f">%s</text>'
                 % ((x0 + x1) / 2, dy + fs * 1.5, FAINT, fs * 0.72, w_lbl))

    dx = x0 - pad
    g.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.5"/>'
             % (x0, y0, dx - fs * 0.6, y0, FAINT))
    g.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.5"/>'
             % (x0, y1, dx - fs * 0.6, y1, FAINT))
    g.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>' % (dx, y0, dx, y1))
    g.append(arrow(dx, y0, 0, -1))
    g.append(arrow(dx, y1, 0, 1))
    cy = (y0 + y1) / 2
    g.append('<g transform="translate(%.1f,%.1f) rotate(-90)">' % (dx, cy))
    g.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#ffffff" stroke="none"/>'
             % (-fs * 3.2, -fs * 0.78, fs * 6.4, fs * 1.15))
    g.append('<text x="0" y="%.1f" text-anchor="middle" stroke="none">%s %s</text>'
             % (fs * 0.33, fmt(h_val, unit), unit))
    if h_lbl:
        g.append('<text x="0" y="%.1f" text-anchor="middle" stroke="none" fill="%s" '
                 'font-size="%.0f">%s</text>' % (fs * 1.5, FAINT, fs * 0.72, h_lbl))
    g.append("</g></g>")
    return "".join(g)


def title_block(px_w, px_h, fs, rows):
    bw, bh = fs * 20.0, fs * (1.6 + 1.35 * len(rows))
    x, y = px_w - bw - fs * 1.6, px_h - bh - fs * 1.6
    g = ['<g id="titleblock" font-family="Helvetica,Arial,sans-serif">']
    g.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#ffffff" '
             'stroke="%s" stroke-width="2.5"/>' % (x, y, bw, bh, INK))
    ty = y + fs * 1.5
    for i, (k, v) in enumerate(rows):
        if i == 0:
            # shrink then truncate so a long filename cannot run outside the box
            title_fs = fs * 1.05
            avail = bw - fs * 1.4
            while title_fs > fs * 0.62 and len(v) * title_fs * 0.56 > avail:
                title_fs *= 0.94
            maxc = max(8, int(avail / (title_fs * 0.56)))
            shown = v if len(v) <= maxc else v[:maxc - 1] + "…"
            g.append('<text x="%.1f" y="%.1f" font-size="%.0f" font-weight="700" fill="%s">'
                     '%s</text>' % (x + fs * 0.7, ty, title_fs, INK, esc(shown)))
            g.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                     'stroke-width="1.5"/>' % (x, ty + fs * 0.45, x + bw, ty + fs * 0.45, INK))
            ty += fs * 1.75
            continue
        g.append('<text x="%.1f" y="%.1f" font-size="%.0f" fill="%s">%s</text>'
                 % (x + fs * 0.7, ty, fs * 0.8, FAINT, esc(k)))
        g.append('<text x="%.1f" y="%.1f" font-size="%.0f" fill="%s" text-anchor="end">%s</text>'
                 % (x + bw - fs * 0.7, ty, fs * 0.8, INK, esc(v)))
        ty += fs * 1.35
    g.append("</g>")
    return "".join(g)


def write_svg(path, page, polylines, hidden_lines, bbox_px, dims, meta):
    mm_w, mm_h, px_w, px_h = page
    fs = px_h * 0.0165
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
        'width="%gmm" height="%gmm" viewBox="0 0 %d %d">' % (mm_w, mm_h, px_w, px_h),
        '<rect width="%d" height="%d" fill="#ffffff"/>' % (px_w, px_h),
        '<rect x="%.0f" y="%.0f" width="%.0f" height="%.0f" fill="none" stroke="%s" '
        'stroke-width="3"/>' % (fs, fs, px_w - 2 * fs, px_h - 2 * fs, INK),
    ]
    if hidden_lines:
        parts.append('<g id="hidden" fill="none" stroke="%s" stroke-width="2" '
                     'stroke-dasharray="14 10" stroke-linecap="round">' % FAINT)
        parts += ['<path d="%s"/>' % path_d(p) for p in hidden_lines]
        parts.append("</g>")
    parts.append('<g id="linework" fill="none" stroke="%s" stroke-width="3.2" '
                 'stroke-linecap="round" stroke-linejoin="round">' % INK)
    parts += ['<path d="%s"/>' % path_d(p) for p in polylines]
    parts.append("</g>")
    if dims:
        parts.append(dim_group(bbox_px, dims["w"], dims["h"], dims["unit"],
                               dims["w_label"], dims["h_label"], fs, fs * 3.0))
    parts.append(title_block(px_w, px_h, fs, meta))
    parts.append("</svg>")
    with open(path, "w") as f:
        f.write("\n".join(parts))


# ---------------------------------------------------------------- png
def setup_render(scene, px_w, px_h):
    r = scene.render
    r.resolution_x, r.resolution_y, r.resolution_percentage = px_w, px_h, 100
    r.film_transparent = False
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    r.engine = "BLENDER_WORKBENCH"
    sh = scene.display.shading
    sh.light = "STUDIO"          # FLAT + white single colour renders white-on-white
    sh.color_type = "SINGLE"
    sh.single_color = (0.62, 0.66, 0.72)
    sh.show_object_outline = True
    sh.object_outline_color = (0.1, 0.17, 0.30)
    if scene.world is None:
        scene.world = bpy.data.worlds.new("W")
    scene.world.color = (1.0, 1.0, 1.0)


# ---------------------------------------------------------------- entry point
def generate(**kwargs):
    """Produce drawings + dimensions.json. Returns the report dict."""
    o = dict(DEFAULTS)
    o.update(kwargs)
    log = o["log"] or (lambda *a: None)

    for v in o["views"]:
        if v not in VIEWS:
            raise ValueError("unknown view %r; choose from %s" % (v, ", ".join(VIEW_ORDER)))
    if not o["views"]:
        raise ValueError("no views selected")
    if o["unit"] not in UNIT_FACTORS:
        raise ValueError("unknown unit " + str(o["unit"]))
    if o["page"] not in PAGES:
        raise ValueError("unknown page " + str(o["page"]))

    out_dir = os.path.abspath(bpy.path.abspath(o["out_dir"]))
    os.makedirs(out_dir, exist_ok=True)
    page = PAGES[o["page"]]
    mm_w, mm_h, px_w, px_h = page

    if o["use_current_scene"]:
        src = bpy.data.filepath or "<current scene>"
        kind = "scene"
    else:
        if not o["source"]:
            raise ValueError("no source file given")
        src = os.path.abspath(bpy.path.abspath(o["source"]))
        if not os.path.exists(src):
            raise ValueError("input not found: " + src)
        kind = load(src)

    scene = bpy.context.scene
    u = scene.unit_settings
    # Blender stores geometry in metres; scale_length is a display setting.
    factor = UNIT_FACTORS[o["unit"]] * (u.scale_length if u.system != "NONE" else 1.0)

    objs = mesh_objects(scene)
    if not objs:
        raise ValueError("no mesh geometry found in " + src)
    log("[scene] %s (%s): %d meshes" % (os.path.basename(src), kind, len(objs)))

    scene_dims, per_object, mn, mx = measure(objs, factor)
    title = o["title"] or (os.path.splitext(os.path.basename(src))[0]
                           if src != "<current scene>" else "Untitled")
    d = scene_dims["dimensions"]
    views_meta = {}

    # Restore the user's scene afterwards when running inside the GUI; the CLI is a
    # throwaway process and skips the bookkeeping.
    with SceneGuard(scene, active=o["use_current_scene"]) as guard:
        # Line Art bakes the whole scene frame range; pin it or a 250-frame file bakes
        # 250 times per view.
        scene.frame_start = scene.frame_end = scene.frame_current = 1
        setup_render(scene, px_w, px_h)
        gp, mod = make_lineart(o["crease"], guard)

        for name in o["views"]:
            cam, mapping = make_camera("bp_cam_" + name, VIEWS[name], mn, mx,
                                       px_w, px_h, o["margin"], guard)
            scene.camera = cam
            bpy.context.view_layer.update()

            if hasattr(mod, "use_multiple_levels"):
                mod.use_multiple_levels = False
            bake_strokes(gp)
            visible = read_strokes(gp, scene, cam, px_w, px_h)

            hidden = []
            if o["hidden"] and hasattr(mod, "use_multiple_levels"):
                mod.use_multiple_levels = True
                mod.level_start, mod.level_end = 1, 3
                bake_strokes(gp)
                hidden = read_strokes(gp, scene, cam, px_w, px_h)
                mod.use_multiple_levels = False

            # dimension the projected model bbox, not the linework extents
            corners = [Vector((x, y, z)) for x in (mn.x, mx.x)
                       for y in (mn.y, mx.y) for z in (mn.z, mx.z)]
            proj = [world_to_camera_view(scene, cam, c) for c in corners]
            bx = [p.x * px_w for p in proj]
            by = [(1 - p.y) * px_h for p in proj]
            bbox_px = (min(bx), min(by), max(bx), max(by))

            right, up = Vector(mapping["screen_right_axis"]), Vector(mapping["screen_up_axis"])
            span = mx - mn
            w_world = abs(span.dot(right)) * factor
            h_world = abs(span.dot(up)) * factor
            scale_ratio = (mm_w / 1000.0) / mapping["ortho_scale_world"]
            scale_text = ("1:%.1f" % (1.0 / scale_ratio)) if scale_ratio < 1 \
                else ("%.1f:1" % scale_ratio)

            dims = None if (not o["dims"] or name == "iso") else {
                "w": w_world, "h": h_world, "unit": o["unit"],
                "w_label": mapping["screen_right_label"],
                "h_label": mapping["screen_up_label"],
            }
            meta_rows = [
                ("", title),
                ("View", name.upper()),
                ("Overall", "%s x %s x %s %s" % (fmt(d["x"], o["unit"]), fmt(d["y"], o["unit"]),
                                                 fmt(d["z"], o["unit"]), o["unit"])),
                ("Scale", scale_text),
                ("Page", "%s (%gx%g mm)" % (o["page"].upper(), mm_w, mm_h)),
                ("Date", datetime.date.today().isoformat()),
            ]

            svg_path = os.path.join(out_dir, name + ".svg")
            write_svg(svg_path, page, visible, hidden, bbox_px, dims, meta_rows)

            mapping.update({
                "svg": os.path.basename(svg_path),
                "stroke_count": len(visible), "hidden_stroke_count": len(hidden),
                "page_mm": [mm_w, mm_h],
                "drawing_scale": round(scale_ratio, 6), "drawing_scale_text": scale_text,
                "model_bbox_pixels": [round(v, 2) for v in bbox_px],
                "dimensioned_width_" + o["unit"]: round(w_world, 4),
                "dimensioned_height_" + o["unit"]: round(h_world, 4),
            })

            if o["png"]:
                gp.hide_render = True
                png_path = os.path.join(out_dir, name + ".png")
                scene.render.filepath = png_path
                bpy.ops.render.render(write_still=True)
                gp.hide_render = False
                mapping["png"] = os.path.basename(png_path)

            views_meta[name] = mapping
            log("[view] %-7s %4d strokes%s  scale %s  -> %s"
                % (name, len(visible), (" +%d hidden" % len(hidden)) if hidden else "",
                   scale_text, mapping["svg"]))

    report = {
        "source": src, "source_type": kind,
        "title": title,
        "blender_version": bpy.app.version_string,
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "units": {"system": u.system, "length_unit": u.length_unit,
                  "scale_length": u.scale_length, "reported_in": o["unit"],
                  "note": "Blender stores geometry in metres; reported values are "
                          "metres * scale_length * unit factor."},
        "scene": scene_dims, "objects": per_object, "views": views_meta,
        "output_dir": out_dir,
        "caveats": [
            "Dimensions are axis-aligned bounding boxes. A part rotated off-axis reports a "
            "larger box than its true size; use local_dimensions for that part.",
            "Line Art silhouettes follow the mesh, so a tessellated curve reads as facets.",
            "Scale assumes the SVG is printed at its declared physical page size.",
        ],
    }
    with open(os.path.join(out_dir, "dimensions.json"), "w") as f:
        json.dump(report, f, indent=2)

    log("\n=== %s ===" % title)
    log("  overall  %s x %s x %s %s   (X x Y x Z)"
        % (fmt(d["x"], o["unit"]), fmt(d["y"], o["unit"]), fmt(d["z"], o["unit"]), o["unit"]))
    log("  meshes   %d   polygons %d   vertices %d"
        % (scene_dims["mesh_count"], scene_dims["polygon_count"], scene_dims["vertex_count"]))
    log("  output   %s" % out_dir)
    return report
