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

import bmesh
import bpy
from mathutils import Euler, Vector
from bpy_extras.object_utils import world_to_camera_view

from . import annotate, render
from .render import fmt

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

# Standard multiview arrangement, as (column, row) on a 4x3 grid with row 0 at the top.
# Third angle (US/Canada) puts each view on the side of Front it is seen from; first angle
# (ISO/Europe) puts it on the opposite side. Getting this wrong inverts the drawing's
# meaning, so both are offered and the sheet states which is in use.
PROJECTION = {
    "third": {"top": (1, 0), "left": (0, 1), "front": (1, 1), "right": (2, 1),
              "back": (3, 1), "bottom": (1, 2), "iso": (3, 0)},
    "first": {"bottom": (1, 0), "right": (0, 1), "front": (1, 1), "left": (2, 1),
              "back": (3, 1), "top": (1, 2), "iso": (3, 0)},
}
PROJECTION_COLS, PROJECTION_ROWS = 4, 3
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

DEFAULTS = dict(
    source=None,            # path to import; ignored when use_current_scene is True
    use_current_scene=False,
    out_dir="./blueprint_out",
    views=("front", "right", "top", "iso"),
    unit="m",
    page="a4l",
    theme="white",
    layout="sheet",         # "sheet" (page per view) | "plate" (grid) | "projection"
    angle="third",          # projection layout convention: "third" or "first"
    uniform_scale=True,     # one scale across all views of a multiview drawing
    grid=False,
    margin=0.18,
    crease=140.0,
    silhouette=True,
    hidden=False,
    dims=True,
    png=False,
    pdf=False,
    title=None,
    log=print,
    # dimension sources; see annotate.collect
    dim_overall=True,
    dim_objects=False,
    dim_edges=False,
    dim_auto=False,
    dim_empties=False,
    dim_angle=True,
    dim_perimeter=False,
    dim_radius_not_dia=False,
    dim_kinds=None,         # None means every kind
    dim_objects_only=None,  # list of object names to measure; None means all of them
    dim_offset=1.0,         # multiplier on how far dimensions sit from the geometry
    dim_step=1.0,           # multiplier on the collision-avoidance stagger
    dim_text=1.0,           # multiplier on dimension text size
    centre_marks=True,      # dash-dot cross through circular features seen face-on
    # Title block fields. Every entry is (label, value); a value of None is filled in from
    # the drawing itself. The caller owns this list, so a sheet can carry whatever the job
    # needs rather than a fixed set baked into the tool.
    fields=None,
    # Detail callouts: [{"name": "A", "objects": [...], "on": "front"}]. Each rings its
    # region on the parent view and gets an enlarged view of its own.
    details=None,
)

# Filled in from the drawing when a field's value is left as None.
AUTO_FIELDS = ("view", "views", "scale", "overall", "unit", "page", "date", "projection",
               "sheet", "drawn")


def resolve_fields(fields, auto, plate=False):
    """
    Turn the caller's field list into (label, value) pairs ready for the title block.

    A value of None means "fill this in from the drawing", matched case-insensitively
    against AUTO_FIELDS by the field's own label. Anything else is passed through verbatim,
    so a sheet can carry a drawing number, a client or a revision the tool knows nothing
    about.
    """
    out = []
    for item in (fields or default_fields()):
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            k, v = item[0], item[1]
        else:
            k, v = str(item), None
        if v is None or v == "":
            key = str(k).strip().lower()
            if plate and key == "view":
                key = "views"
            v = auto.get(key, "")
        if str(k).strip():
            out.append((str(k), str(v)))
    return out


def default_fields():
    return [("View", None), ("Scale", None), ("Overall", None), ("Unit", None),
            ("Page", None), ("Date", None)]


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
def detail_bounds(scene, spec):
    """World bounding box of the objects a detail callout covers."""
    names = set(spec.get("objects") or [])
    objs = [o for o in mesh_objects(scene) if o.name in names]
    if not objs:
        return None
    pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
    return aabb(pts)


def axes_for(spec):
    """(right, up, out) for a view, without building a camera first."""
    if spec["right"] is not None:
        return Vector(spec["right"]), Vector(spec["up"]), Vector(spec["out"])
    m = Euler([math.radians(a) for a in spec["rot"]], "XYZ").to_matrix()
    return m.col[0].normalized(), m.col[1].normalized(), m.col[2].normalized()


def ortho_for(spec, mn, mx, aspect, margin):
    """World units across the frame width needed to fit the bbox in this view."""
    right, up, _out = axes_for(spec)
    centre = (mn + mx) / 2.0
    corners = [Vector((x, y, z)) for x in (mn.x, mx.x)
               for y in (mn.y, mx.y) for z in (mn.z, mx.z)]
    rs = [(c - centre).dot(right) for c in corners]
    us = [(c - centre).dot(up) for c in corners]
    keep = 1.0 - 2.0 * margin
    return max((max(rs) - min(rs)) / keep, ((max(us) - min(us)) / keep) * aspect)


def make_camera(name, spec, mn, mx, res_x, res_y, margin, guard=None, force_ortho=None):
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
    # A multiview drawing must share ONE scale across its views, or the reader cannot
    # compare them and the stated scale is a lie on all but one.
    if force_ortho:
        ortho = force_ortho
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
        "screen_out_axis": [round(v, 6) for v in out],
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


class Silhouette:
    """
    Per-view silhouette edges, handed to Line Art as Freestyle edge marks.

    Line Art draws creases, boundaries and intersections but produces no contour for a
    smooth curved surface: a plain cylinder comes out as two cap rings with nothing joining
    them, because its side edges meet at ~169 degrees and so are not creases. Marking the
    silhouette lets Line Art draw it AND occlusion-test it, which drawing it ourselves on
    top would not achieve.

    The attribute is created once for the whole run and only its VALUES change per view.
    Creating and removing it per view looked equivalent and was not: after the first
    create/remove cycle Line Art stopped honouring the marks, and a detail view rendered as
    bare cap rings with its silhouette missing.
    """

    def __init__(self, objs):
        self.saved = []
        for o in objs:
            me = o.data
            if not len(me.edges):
                continue
            attr = me.attributes.get("freestyle_edge")
            existed = attr is not None
            if not existed:
                attr = me.attributes.new(name="freestyle_edge", type="BOOLEAN",
                                         domain="EDGE")
            before = [False] * len(me.edges)
            attr.data.foreach_get("value", before)
            self.saved.append((o, me, before, existed))

    def apply(self, out_axis):
        """Rewrite the marks for a view direction. A silhouette is view-dependent."""
        for o, me, before, _existed in self.saved:
            nmat = o.matrix_world.to_3x3().inverted_safe().transposed()
            bm = bmesh.new()
            bm.from_mesh(me)
            bm.edges.ensure_lookup_table()
            marks = list(before)
            for e in bm.edges:
                faces = e.link_faces
                if len(faces) == 1:
                    marks[e.index] = True          # an open boundary is always a silhouette
                elif len(faces) == 2:
                    d0 = (nmat @ faces[0].normal).dot(out_axis)
                    d1 = (nmat @ faces[1].normal).dot(out_axis)
                    if (d0 > 0.0) != (d1 > 0.0):   # the faces disagree about facing us
                        marks[e.index] = True
            bm.free()
            attr = me.attributes.get("freestyle_edge")
            if attr is not None:
                attr.data.foreach_set("value", marks)
                me.update()

    def restore(self):
        for _o, me, before, existed in self.saved:
            attr = me.attributes.get("freestyle_edge")
            if attr is None:
                continue
            if existed:
                attr.data.foreach_set("value", before)
            else:
                me.attributes.remove(attr)         # we created it; leave no trace
            me.update()
        self.saved = []


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
def generate(sheets_out=None, write=True, **kwargs):
    """
    Produce drawings + dimensions.json. Returns the report dict.

    sheets_out: optional dict, filled with {view_name: Sheet} for callers that want to
                render the drawing themselves (the viewport preview does).
    write:      set False to build everything in memory and write nothing.
    """
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
    if write:
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
    sheets = {}

    projection = o["layout"] == "projection"
    plate = o["layout"] in ("plate", "projection")
    fs_page = px_h * 0.0165

    # A detail is rendered like any other view, so it gets a cell from the same layout
    # machinery rather than needing free-space hunting of its own.
    details = []
    for i, spec in enumerate(o["details"] or []):
        bounds = detail_bounds(scene, spec)
        if bounds is None:
            log("[detail] %s skipped: none of its objects are in the scene"
                % spec.get("name", "?"))
            continue
        details.append(dict(name=(spec.get("name") or chr(65 + i)).upper(),
                            on=spec.get("on") or o["views"][0],
                            mn=bounds[0], mx=bounds[1]))
    detail_keys = ["detail:" + d["name"] for d in details]
    all_views = list(o["views"]) + detail_keys

    if projection:
        # Standard multiview arrangement: views sit where they project to, on a shared
        # grid, so Top lines up over Front and Right beside it. Only the occupied part of
        # the 4x3 grid is used, so a front/top/right drawing is not padded out to full
        # width by the empty Back column.
        table = PROJECTION[o["angle"]]
        used = [v for v in o["views"] if v in table]
        # details take the free slots of the projection grid, in reading order
        free = [(c, r) for r in range(PROJECTION_ROWS) for c in range(PROJECTION_COLS)
                if (c, r) not in [table[v] for v in used]]
        table = dict(table)
        for key, slot in zip(detail_keys, free):
            table[key] = slot
        used = used + [k for k in detail_keys if k in table]
        if not used:
            raise ValueError("no placeable views for a projection layout")
        cs = [table[v][0] for v in used]
        rs = [table[v][1] for v in used]
        c0, c1, r0, r1 = min(cs), max(cs), min(rs), max(rs)
        cols, rows = (c1 - c0 + 1), (r1 - r0 + 1)
        pad = fs_page * 1.4
        band = render.title_block_height(
            fs_page, len(o["fields"] or default_fields())) + fs_page * 2.2
        area_w = px_w - 2 * pad
        area_h = px_h - 2 * pad - band
        cell_w = int((area_w - pad * (cols - 1)) / cols)
        cell_h = int((area_h - pad * (rows - 1)) / rows)
        if cell_w < 80 or cell_h < 80:
            raise ValueError("too many views for one projection sheet at this page size")
        cells = {}
        for v in used:
            c, r = table[v]
            cells[v] = (pad + (c - c0) * (cell_w + pad),
                        pad + (r - r0) * (cell_h + pad), cell_w, cell_h)
        page_sheet = render.Sheet(px_w, px_h, mm_w, mm_h, o["theme"])
        render.draw_frame(page_sheet, fs_page)
        if o["grid"]:
            render.draw_grid(page_sheet, fs_page)
    elif plate:
        # Lay the views out in a grid whose cell aspect stays close to the page aspect,
        # leaving a band at the bottom for the title block.
        n = len(all_views)
        pad = fs_page * 1.8
        band = render.title_block_height(
            fs_page, len(o["fields"] or default_fields())) + fs_page * 2.2
        page_aspect = (px_w - 2 * pad) / float(px_h - 2 * pad - band)
        # Pick the grid whose cells are closest in shape to the page, preferring one with
        # no empty slots. sqrt(n * aspect) alone puts 4 views in 3 columns and leaves a
        # hole, which reads as a mistake on a poster.
        best = None
        for c in range(1, n + 1):
            r = int(math.ceil(n / float(c)))
            cell_aspect = (page_aspect * r) / float(c)
            score = abs(math.log(cell_aspect / page_aspect)) + 0.35 * (c * r - n)
            if best is None or score < best[0]:
                best = (score, c, r)
        cols, rows = best[1], best[2]
        area_w = px_w - 2 * pad
        area_h = px_h - 2 * pad - band
        cell_w = int((area_w - pad * (cols - 1)) / cols)
        cell_h = int((area_h - pad * (rows - 1)) / rows)
        if cell_w < 80 or cell_h < 80:
            raise ValueError("too many views for one plate at this page size")
        cells = {}
        for i, nm in enumerate(all_views):
            r, c = divmod(i, cols)
            cells[nm] = (pad + c * (cell_w + pad), pad + r * (cell_h + pad), cell_w, cell_h)
        page_sheet = render.Sheet(px_w, px_h, mm_w, mm_h, o["theme"])
        render.draw_frame(page_sheet, fs_page)
        if o["grid"]:
            render.draw_grid(page_sheet, fs_page)
    else:
        cells = {nm: (0, 0, px_w, px_h) for nm in all_views}
        page_sheet = None

    # One scale across every view of a multiview drawing. Framing each view to its own
    # extents makes the same model print at a different ratio per view, which stops the
    # reader comparing them and makes the stated scale wrong on all but one.
    shared_ortho = None
    shared_scale = None
    if o["uniform_scale"] and len(o["views"]) > 1:
        # details are deliberately excluded: an enlargement that shares the sheet scale
        # would not be an enlargement
        shared_ortho = max(
            ortho_for(VIEWS[v], mn, mx,
                      cells[v][2] / float(cells[v][3]) if v in cells else px_w / float(px_h),
                      o["margin"])
            for v in o["views"] if v in cells or not plate)
        first = next((v for v in o["views"] if v in cells), None)
        if first:
            shared_scale = ((mm_w * cells[first][2] / float(px_w)) / 1000.0) / shared_ortho

    # Restore the user's scene afterwards when running inside the GUI; the CLI is a
    # throwaway process and skips the bookkeeping.
    with SceneGuard(scene, active=o["use_current_scene"]) as guard:
        # Line Art bakes the whole scene frame range; pin it or a 250-frame file bakes
        # 250 times per view.
        scene.frame_start = scene.frame_end = scene.frame_current = 1
        setup_render(scene, px_w, px_h)
        gp, mod = make_lineart(o["crease"], guard)
        silh = Silhouette(objs) if o["silhouette"] else None

        for name in all_views:
            if name not in cells:
                log("[view] %-7s skipped: no place in this layout" % name)
                continue
            detail = next((d for d in details if "detail:" + d["name"] == name), None)
            v_mn, v_mx = (detail["mn"], detail["mx"]) if detail else (mn, mx)
            spec_name = detail["on"] if detail else name
            cell_x, cell_y, cw, ch = cells[name]
            # world_to_camera_view normalises against the scene render resolution, so it
            # must match the cell the view is drawn into or the projection is stretched
            scene.render.resolution_x, scene.render.resolution_y = cw, ch
            cam, mapping = make_camera("bp_cam_" + name.replace(":", "_"),
                                       VIEWS[spec_name], v_mn, v_mx,
                                       cw, ch, o["margin"], guard,
                                       force_ortho=None if detail else shared_ortho)
            scene.camera = cam
            bpy.context.view_layer.update()

            # out_axis points from the model toward the camera
            if silh is not None:
                silh.apply(Vector(mapping["screen_out_axis"]))

            # Set the occlusion level explicitly for every pass. With use_multiple_levels
            # off, level_start IS the level drawn, so leaving it at 1 after a hidden-line
            # pass makes the NEXT view draw its once-occluded lines as if they were the
            # visible outline.
            if hasattr(mod, "use_multiple_levels"):
                mod.use_multiple_levels = False
                mod.level_start = 0
                mod.level_end = 0
            bake_strokes(gp)
            visible = read_strokes(gp, scene, cam, cw, ch)

            hidden = []
            if o["hidden"] and hasattr(mod, "use_multiple_levels"):
                mod.use_multiple_levels = True
                mod.level_start, mod.level_end = 1, 3
                bake_strokes(gp)
                hidden = read_strokes(gp, scene, cam, cw, ch)
                mod.use_multiple_levels = False
                mod.level_start = mod.level_end = 0

            # dimension the projected model bbox, not the linework extents
            corners = [Vector((x, y, z)) for x in (v_mn.x, v_mx.x)
                       for y in (v_mn.y, v_mx.y) for z in (v_mn.z, v_mx.z)]
            proj = [world_to_camera_view(scene, cam, c) for c in corners]
            bx = [p.x * cw for p in proj]
            by = [(1 - p.y) * ch for p in proj]
            bbox_px = (min(bx), min(by), max(bx), max(by))

            right, up = Vector(mapping["screen_right_axis"]), Vector(mapping["screen_up_axis"])
            span = v_mx - v_mn
            w_world = abs(span.dot(right)) * factor
            h_world = abs(span.dot(up)) * factor
            scale_ratio = ((mm_w * cw / float(px_w)) / 1000.0) / mapping["ortho_scale_world"]
            scale_text = ("1:%.1f" % (1.0 / scale_ratio)) if scale_ratio < 1 \
                else ("%.1f:1" % scale_ratio)

            auto = {
                "view": name.upper().replace("DETAIL:", "DETAIL "),
                "views": ", ".join(v.upper() for v in o["views"] if v in cells),
                "scale": scale_text,
                "overall": "%s x %s x %s %s" % (fmt(d["x"], o["unit"]),
                                                fmt(d["y"], o["unit"]),
                                                fmt(d["z"], o["unit"]), o["unit"]),
                "unit": o["unit"],
                "page": "%s  %gx%g mm" % (o["page"].upper(), mm_w, mm_h),
                "date": datetime.date.today().isoformat(),
                "projection": ("%s angle" % o["angle"]) if projection else "-",
                "sheet": "1 of 1",
                "drawn": "",
            }
            meta_rows = resolve_fields(o["fields"], auto)

            # ---- build the view at cell size; page chrome is added by whoever owns
            #      the page (this sheet when one view per page, the plate otherwise)
            sheet = render.Sheet(cw, ch, mm_w * cw / float(px_w), mm_h * ch / float(px_h),
                                 o["theme"])
            fs = ch * 0.0165 if not plate else fs_page * 0.85
            if not plate:
                render.draw_frame(sheet, fs)
                if o["grid"]:
                    render.draw_grid(sheet, fs)
            dash = sheet.px_per_mm * 1.6
            inset = 0.0 if plate else 9.0 * sheet.px_per_mm
            clip = (inset, inset, cw - inset, ch - inset)
            if hidden:
                for p in hidden:
                    for run in render.clip_polyline(p, *clip):
                        sheet.polyline(run, sheet.theme["hidden"], sheet.w("hidden"),
                                       dash=(dash, dash * 0.62))
            for p in visible:
                for run in render.clip_polyline(p, *clip):
                    sheet.polyline(run, sheet.theme["ink"], sheet.w("outline"))

            n_dims = 0
            drawn_dims = []
            if o["dims"] and name != "iso":
                scope = ([ob for ob in objs
                          if any(ob.name in (sp.get("objects") or [])
                                 for sp in (o["details"] or [])
                                 if (sp.get("name") or "").upper() == detail["name"])]
                         or objs) if detail else objs
                dim_list = annotate.collect(
                    scene, scope, v_mn, v_mx, right, up, factor, o["unit"],
                    dict(overall=o["dim_overall"], objects=o["dim_objects"],
                         edges=o["dim_edges"], auto=o["dim_auto"],
                         empties=o["dim_empties"], angle=o["dim_angle"],
                         perimeter=o["dim_perimeter"],
                         objects_only=o["dim_objects_only"],
                         radius_not_dia=o["dim_radius_not_dia"], kinds=o["dim_kinds"]))
                placer = annotate.Placer(cw, ch, fs * 1.0)
                if not plate:
                    # reserve the title block so no dimension lands underneath it
                    tbh = render.title_block_height(fs, len(meta_rows))
                    placer.reserve((cw - fs * 23.0, ch - tbh - fs * 1.2, fs * 23.0, tbh))
                if o["centre_marks"]:
                    annotate.draw_centre_marks(sheet, dim_list, scene, cam,
                                               Vector(mapping["screen_out_axis"]))
                n_dims = annotate.draw_dims(sheet, dim_list, scene, cam,
                                            fs * o["dim_text"], placer, bbox_px,
                                            offset=o["dim_offset"], step=o["dim_step"])
                drawn_dims = [{"kind": x.kind, "label": x.label, "source": x.source,
                               "value": round(x.unit_value, 4)
                               if x.unit_value is not None else None}
                              for x in dim_list]

            # ring the enlarged regions on whichever view they belong to
            for dt in details:
                if dt["on"] != name:
                    continue
                dc = (dt["mn"] + dt["mx"]) / 2.0
                cp = world_to_camera_view(scene, cam, dc)
                corners_d = [Vector((x, y, z)) for x in (dt["mn"].x, dt["mx"].x)
                             for y in (dt["mn"].y, dt["mx"].y)
                             for z in (dt["mn"].z, dt["mx"].z)]
                pj = [world_to_camera_view(scene, cam, c) for c in corners_d]
                rx = (max(p.x for p in pj) - min(p.x for p in pj)) * cw
                ry = (max(p.y for p in pj) - min(p.y for p in pj)) * ch
                render.detail_callout(sheet, cp.x * cw, (1 - cp.y) * ch,
                                      max(math.hypot(rx, ry) * 0.55, fs * 1.4),
                                      dt["name"], fs)

            if plate:
                page_sheet.blit(sheet, cell_x, cell_y)
                cap = ("DETAIL %s" % detail["name"]) if detail else name
                render.caption(page_sheet, cell_x + cw / 2.0, cell_y + ch - fs * 0.6,
                               cap, fs_page, sub=scale_text if detail else None)
            else:
                render.draw_title_block(sheet, fs, meta_rows, title=title)
                sheets[name] = sheet

            # a colon is illegal in a Windows filename, and "detail:A" is a view key,
            # not a file name
            svg_path = os.path.join(out_dir, name.replace(":", "-") + ".svg")
            if write:
                render.write_svg(svg_path, sheet)

            mapping.update({
                "svg": os.path.basename(svg_path),
                "stroke_count": len(visible), "hidden_stroke_count": len(hidden),
                "page_mm": [mm_w, mm_h], "theme": o["theme"],
                "drawing_scale": round(scale_ratio, 6), "drawing_scale_text": scale_text,
                "model_bbox_pixels": [round(v, 2) for v in bbox_px],
                "dimensions_drawn": n_dims,
                "dimensions": drawn_dims,
                "dimensioned_width_" + o["unit"]: round(w_world, 4),
                "dimensioned_height_" + o["unit"]: round(h_world, 4),
            })

            if o["png"] and write:
                gp.hide_render = True
                png_path = os.path.join(out_dir, name.replace(":", "-") + ".png")
                scene.render.filepath = png_path
                bpy.ops.render.render(write_still=True)
                gp.hide_render = False
                mapping["png"] = os.path.basename(png_path)

            views_meta[name] = mapping
            log("[view] %-7s %4d strokes%s%s  scale %s  -> %s"
                % (name, len(visible), (" +%d hidden" % len(hidden)) if hidden else "",
                   (" %d dims" % n_dims) if n_dims else "", scale_text, mapping["svg"]))

        # the marks are ours; put the meshes back exactly as they were
        if silh is not None:
            silh.restore()

    if plate:
        plate_auto = dict(auto)
        plate_auto["view"] = plate_auto["views"]
        plate_auto["scale"] = ("1:%.1f" % (1.0 / shared_scale)) if shared_scale and \
            shared_scale < 1 else (auto["scale"])
        render.draw_title_block(page_sheet, fs_page,
                                resolve_fields(o["fields"], plate_auto, plate=True),
                                title=title)
        name_out = "projection" if projection else "plate"
        sheets[name_out] = page_sheet
        if write:
            render.write_svg(os.path.join(out_dir, name_out + ".svg"), page_sheet)
        log("[%s] %d views on one %s sheet -> %s.svg"
            % (name_out, len(cells), o["page"].upper(), name_out))

    if sheets_out is not None:
        sheets_out.update(sheets)

    pdf_path = None
    if o["pdf"] and write:
        pdf_path = os.path.join(out_dir, "blueprints.pdf")
        order = ([("projection" if projection else "plate")] if plate
                 else [v for v in o["views"] if v in sheets])
        render.write_pdf(pdf_path, [sheets[v] for v in order], title)
        log("[pdf]   %d page%s -> %s"
            % (len(sheets), "" if len(sheets) == 1 else "s", os.path.basename(pdf_path)))

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
        "details": [{"name": dt["name"], "on": dt["on"]} for dt in details],
        "fields": [list(f) for f in (o["fields"] or default_fields())],
        "output_dir": out_dir, "theme": o["theme"], "layout": o["layout"],
        "pdf": os.path.basename(pdf_path) if pdf_path else None,
        "caveats": [
            "Dimensions are axis-aligned bounding boxes. A part rotated off-axis reports a "
            "larger box than its true size; use local_dimensions for that part.",
            "Line Art silhouettes follow the mesh, so a tessellated curve reads as facets.",
            "Scale assumes the SVG is printed at its declared physical page size.",
        ],
    }
    if write:
        with open(os.path.join(out_dir, "dimensions.json"), "w") as f:
            json.dump(report, f, indent=2)

    log("\n=== %s ===" % title)
    log("  overall  %s x %s x %s %s   (X x Y x Z)"
        % (fmt(d["x"], o["unit"]), fmt(d["y"], o["unit"]), fmt(d["z"], o["unit"]), o["unit"]))
    log("  meshes   %d   polygons %d   vertices %d"
        % (scene_dims["mesh_count"], scene_dims["polygon_count"], scene_dims["vertex_count"]))
    log("  output   %s" % out_dir)
    return report
