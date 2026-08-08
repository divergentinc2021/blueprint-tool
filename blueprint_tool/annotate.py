"""
Dimension extraction and placement.

Extraction gathers measurements from the scene as world-space geometry (`Dim` records),
independent of any view. Placement then projects them into one view's page pixels and
finds positions that do not collide.

Sources, all optional and combinable:
  overall   the whole model's extents along the two in-view axes
  objects   selected objects, each bounding box dimensioned
  edges     selected mesh edges, each dimensioned by length, plus the angle between a
            pair of them; a selected closed circular loop becomes a diameter or radius
  auto      circular face rings found automatically, as diameter or radius
  empties   Empties paired by name, dimensioned point to point

Placement matters more than it looks: a dimension drawn at its natural offset will
regularly land on top of another. Every label is kept as a rectangle and pushed further
out until it stops overlapping anything already placed.
"""

import math

import bmesh
import bpy
from mathutils import Vector

from . import render

KINDS = ("linear", "length", "diameter", "radius", "angle", "perimeter")


class Dim:
    """One measurement, in world space, before any view is chosen."""

    def __init__(self, kind, a, b, value, label, source, centre=None, unit_value=None,
                 normal=None, radius=None):
        self.kind = kind          # one of KINDS
        self.a = a                # world Vector: measurement start
        self.b = b                # world Vector: measurement end
        self.value = value        # raw Blender units (metres) or degrees for angles
        self.label = label        # text as drawn
        self.source = source      # human-readable provenance
        self.centre = centre      # world Vector for diameter/radius/angle
        self.unit_value = unit_value
        self.normal = normal      # circle axis, for deciding if it reads round in a view
        self.radius = radius

    def __repr__(self):
        return "<Dim %s %s %s>" % (self.kind, self.label, self.source)


# ---------------------------------------------------------------- helpers
def _label(value, unit, prefix=""):
    return "%s%s %s" % (prefix, render.fmt(value, unit), unit)


def fit_circle(points):
    """
    Best-fit circle through coplanar points.

    Returns (centre, radius, residual) with residual as a fraction of the radius, or None
    if the points are not planar enough to be a circle. The residual is what separates a
    genuine circular hole from any other closed loop, so it is returned rather than judged
    here.
    """
    if len(points) < 4:
        return None
    centre = sum(points, Vector((0, 0, 0))) / len(points)
    rel = [p - centre for p in points]

    # plane normal from the largest-area triangle fan segment
    normal = Vector((0, 0, 0))
    for i in range(len(rel)):
        normal += rel[i].cross(rel[(i + 1) % len(rel)])
    if normal.length < 1e-12:
        return None
    normal.normalize()

    flat = max(abs(r.dot(normal)) for r in rel)
    radii = [r.length for r in rel]
    radius = sum(radii) / len(radii)
    if radius < 1e-9 or flat > radius * 0.06:
        return None
    spread = max(abs(r - radius) for r in radii) / radius
    return centre, radius, spread, normal


# ---------------------------------------------------------------- sources
def from_overall(mn, mx, right, up, factor, unit):
    """
    The model's extents along whichever two axes this view shows.

    Anchored on the silhouette edges of the bounding box — the bottom edge for width, the
    left edge for height — not through its centre. Placement offsets a dimension away from
    the model along its own perpendicular, and a line through the centre gives that no
    direction to work with, so both dimensions end up drawn across the model.
    """
    centre = (mn + mx) / 2.0
    span = mx - mn
    R, U = right.normalized(), up.normalized()
    hr, hu = abs(span.dot(R)) / 2.0, abs(span.dot(U)) / 2.0
    corner = centre - R * hr - U * hu     # lower-left of the projected bounding box
    out = []
    for axis, half, tag in ((R, hr, "width"), (U, hu, "height")):
        length = half * 2.0
        if length < 1e-9:
            continue
        out.append(Dim("linear", corner, corner + axis * length, length,
                       _label(length * factor, unit), "overall:" + tag,
                       unit_value=length * factor))
    return out


def from_objects(objects, factor, unit, limit=24):
    """Each object's world bounding box, anchored on its edges rather than its centre."""
    out = []
    for o in objects[:limit]:
        corners = [o.matrix_world @ Vector(c) for c in o.bound_box]
        mn = Vector((min(c[i] for c in corners) for i in range(3)))
        mx = Vector((max(c[i] for c in corners) for i in range(3)))
        for i, axis in enumerate((Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))):
            length = mx[i] - mn[i]
            if length < 1e-9:
                continue
            # run along one edge of the box, not through the middle of the object
            out.append(Dim("linear", mn.copy(), mn + axis * length, length,
                           _label(length * factor, unit),
                           "object:%s:%s" % (o.name, "XYZ"[i]),
                           unit_value=length * factor))
    return out


def _selected_edges(obj):
    """Selected edges as (world_a, world_b, index), reading live data in Edit Mode."""
    mw = obj.matrix_world
    out = []
    if obj.mode == "EDIT":
        bm = bmesh.from_edit_mesh(obj.data)
        for e in bm.edges:
            if e.select:
                out.append((mw @ e.verts[0].co.copy(), mw @ e.verts[1].co.copy(), e.index))
    else:
        me = obj.data
        for e in me.edges:
            if e.select:
                out.append((mw @ me.vertices[e.vertices[0]].co.copy(),
                            mw @ me.vertices[e.vertices[1]].co.copy(), e.index))
    return out


def from_edges(objects, factor, unit, want_angle=True, want_circle=True, limit=64):
    """Selected edges: length each, plus the angle between a pair, plus fitted circles."""
    out = []
    for o in objects:
        if o.type != "MESH":
            continue
        edges = _selected_edges(o)
        if not edges:
            continue

        # a closed ring of selected edges may be a hole or a boss
        if want_circle and len(edges) >= 6:
            pts, seen = [], set()
            for a, b, _ in edges:
                for p in (a, b):
                    key = (round(p.x, 6), round(p.y, 6), round(p.z, 6))
                    if key not in seen:
                        seen.add(key)
                        pts.append(p)
            fit = fit_circle(pts)
            if fit and fit[2] < 0.04:
                centre, radius, _, _ = fit
                dia = radius * 2.0
                out.append(Dim("diameter", centre - Vector((radius, 0, 0)),
                               centre + Vector((radius, 0, 0)), dia,
                               _label(dia * factor, unit, "dia "),
                               "edges:%s:circle" % o.name, centre=centre,
                               unit_value=dia * factor))
                continue  # the ring is the measurement; per-edge lengths would be noise

        for a, b, idx in edges[:limit]:
            length = (b - a).length
            if length < 1e-9:
                continue
            out.append(Dim("length", a, b, length, _label(length * factor, unit),
                           "edge:%s:%d" % (o.name, idx), unit_value=length * factor))

        if want_angle and len(edges) == 2:
            (a1, b1, _), (a2, b2, _) = edges
            d1, d2 = (b1 - a1), (b2 - a2)
            if d1.length > 1e-9 and d2.length > 1e-9:
                ang = math.degrees(d1.angle(d2))
                # the shared vertex is the vertex of the angle, if there is one
                verts = [a1, b1, a2, b2]
                corner = None
                for i in range(2):
                    for j in range(2, 4):
                        if (verts[i] - verts[j]).length < 1e-6:
                            corner = verts[i]
                if corner is None:
                    corner = (a1 + b1 + a2 + b2) / 4.0
                out.append(Dim("angle", b1, b2, ang, "%.1f deg" % ang,
                               "edges:%s:angle" % o.name, centre=corner, unit_value=ang))
    return out


def from_auto(objects, factor, unit, as_radius=False, tol=0.03, limit=12, min_verts=6):
    """
    Circular face rings, as diameter (or radius).

    A cylinder cap is usually a single n-gon, so testing face vertex rings finds the
    common cases cheaply. Full edge-loop tracing would find more but costs far more on a
    dense mesh, and duplicates are collapsed by centre and radius anyway.
    """
    out, seen = [], set()
    for o in objects:
        if o.type != "MESH":
            continue
        mw = o.matrix_world
        me = o.data
        for poly in me.polygons:
            if len(poly.vertices) < min_verts:
                continue
            pts = [mw @ me.vertices[i].co.copy() for i in poly.vertices]
            fit = fit_circle(pts)
            if not fit:
                continue
            centre, radius, spread, normal = fit
            if spread > tol:
                continue
            # Both caps of a cylinder are the same feature and project to the same place,
            # so key on the axis rather than the centre: equal radius, same axis direction,
            # same axis position. Sign-canonicalise the normal, since opposite caps face
            # opposite ways.
            n = normal.copy()
            for c in (n.x, n.y, n.z):
                if abs(c) > 1e-9:
                    if c < 0:
                        n = -n
                    break
            perp = centre - n * centre.dot(n)
            key = (round(radius, 4), round(n.x, 3), round(n.y, 3), round(n.z, 3),
                   round(perp.x, 4), round(perp.y, 4), round(perp.z, 4))
            if key in seen:
                continue
            seen.add(key)
            if as_radius:
                out.append(Dim("radius", centre, centre + Vector((radius, 0, 0)), radius,
                               _label(radius * factor, unit, "R "),
                               "auto:%s:r" % o.name, centre=centre,
                               unit_value=radius * factor, normal=n, radius=radius))
            else:
                dia = radius * 2.0
                out.append(Dim("diameter", centre - Vector((radius, 0, 0)),
                               centre + Vector((radius, 0, 0)), dia,
                               _label(dia * factor, unit, "dia "),
                               "auto:%s:d" % o.name, centre=centre,
                               unit_value=dia * factor, normal=n, radius=radius))
    # biggest features first, then trim — a dense mesh can yield hundreds
    out.sort(key=lambda d: -d.value)
    return out[:limit]


def from_perimeter(objects, factor, unit, use_selection=True, limit=12):
    """
    Perimeter of a closed loop of selected edges, and of each selected face.

    Reported as a leader label at the loop centroid rather than a dimension line, because a
    perimeter is a property of a shape, not a distance between two points.
    """
    out = []
    for o in objects:
        if o.type != "MESH":
            continue
        mw = o.matrix_world

        if use_selection:
            edges = _selected_edges(o)
            if len(edges) >= 3:
                total = sum((b - a).length for a, b, _ in edges)
                pts = [p for a, b, _ in edges for p in (a, b)]
                centre = sum(pts, Vector((0, 0, 0))) / len(pts)
                out.append(Dim("perimeter", pts[0], centre, total,
                               "P %s" % _label(total * factor, unit),
                               "perimeter:%s:selection" % o.name, centre=centre,
                               unit_value=total * factor))

        # selected faces each get their own perimeter
        me = o.data
        if o.mode != "EDIT":
            for poly in me.polygons:
                if not poly.select:
                    continue
                vs = [mw @ me.vertices[i].co.copy() for i in poly.vertices]
                total = sum((vs[i] - vs[i - 1]).length for i in range(len(vs)))
                centre = sum(vs, Vector((0, 0, 0))) / len(vs)
                out.append(Dim("perimeter", vs[0], centre, total,
                               "P %s" % _label(total * factor, unit),
                               "perimeter:%s:face%d" % (o.name, poly.index),
                               centre=centre, unit_value=total * factor))
                if len(out) >= limit:
                    return out
    return out[:limit]


def from_empties(scene, factor, unit, limit=24):
    """Empties paired in name order: the 1st with the 2nd, the 3rd with the 4th, ..."""
    empties = sorted((o for o in scene.objects if o.type == "EMPTY"),
                     key=lambda o: o.name)
    out = []
    for i in range(0, len(empties) - 1, 2):
        a, b = empties[i], empties[i + 1]
        pa, pb = a.matrix_world.translation, b.matrix_world.translation
        length = (pb - pa).length
        if length < 1e-9:
            continue
        out.append(Dim("length", pa, pb, length, _label(length * factor, unit),
                       "empties:%s-%s" % (a.name, b.name), unit_value=length * factor))
    return out[:limit]


def collect(scene, objects, mn, mx, right, up, factor, unit, opts):
    """Gather dimensions from every enabled source."""
    # An explicit include list beats "whatever happens to be selected": selection is easy
    # to disturb, and a drawing should not change because someone clicked in the viewport.
    only = opts.get("objects_only")
    if only:
        chosen = [ob for ob in objects if ob.name in set(only)]
    else:
        chosen = objects

    dims = []
    if opts.get("overall", True):
        dims += from_overall(mn, mx, right, up, factor, unit)
    if opts.get("objects"):
        if only:
            sel = chosen
        else:
            sel = [ob for ob in objects if ob.select_get()] or objects
        dims += from_objects(sel, factor, unit)
    if opts.get("edges"):
        dims += from_edges(chosen, factor, unit,
                           want_angle=opts.get("angle", True),
                           want_circle=opts.get("circle", True))
    if opts.get("auto"):
        dims += from_auto(chosen, factor, unit, as_radius=opts.get("radius_not_dia", False))
    if opts.get("perimeter"):
        dims += from_perimeter(chosen, factor, unit)
    if opts.get("empties"):
        dims += from_empties(scene, factor, unit)

    kinds = opts.get("kinds")
    if kinds:
        dims = [d for d in dims if d.kind in kinds]
    return dims


# ---------------------------------------------------------------- placement
class Placer:
    """Tracks placed label rectangles and keeps new ones from landing on top of them."""

    def __init__(self, px_w, px_h, margin):
        self.taken = []
        self.px_w, self.px_h = px_w, px_h
        self.margin = margin

    def _hits(self, r):
        ax, ay, aw, ah = r
        if ax < self.margin or ay < self.margin:
            return True
        if ax + aw > self.px_w - self.margin or ay + ah > self.px_h - self.margin:
            return True
        for bx, by, bw, bh in self.taken:
            if ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah:
                return True
        return False

    def reserve(self, rect):
        self.taken.append(rect)

    def place(self, cx, cy, w, h, dirx, diry, step, tries=14):
        """Push the label along (dirx, diry) until it clears everything already placed."""
        for i in range(tries):
            ox = cx + dirx * step * i
            oy = cy + diry * step * i
            rect = (ox - w / 2.0, oy - h / 2.0, w, h)
            if not self._hits(rect):
                self.reserve(rect)
                return ox, oy, True
        return cx, cy, False


def _dim_text(sheet, cx, cy, label, fs, colour):
    """Dimension value: lightly tracked, on a punched plate so the line does not cross it."""
    track = fs * 0.05
    render.label_plate(sheet, cx, cy, label, fs, track=track)
    sheet.text(cx, cy + fs * 0.34, label, fs, colour, anchor="middle", track=track)


def _project(scene, cam, p, px_w, px_h):
    from bpy_extras.object_utils import world_to_camera_view
    n = world_to_camera_view(scene, cam, p)
    return Vector((n.x * px_w, (1.0 - n.y) * px_h))


def draw_centre_marks(sheet, dims, scene, cam, out_axis):
    """
    Dash-dot cross through every circular feature the view sees face-on.

    Only face-on: a circle viewed edge-on projects to a line, and a cross there would be
    read as geometry that is not in the part.
    """
    n = 0
    for d in dims:
        if d.kind not in ("diameter", "radius") or d.normal is None or d.centre is None:
            continue
        if abs(d.normal.normalized().dot(out_axis.normalized())) < 0.94:
            continue
        c = _project(scene, cam, d.centre, sheet.px_w, sheet.px_h)
        edge = _project(scene, cam, d.centre + _perp(d.normal) * d.radius,
                        sheet.px_w, sheet.px_h)
        r_px = (edge - c).length
        if r_px < sheet.px_w * 0.006:      # too small to read as anything but a smudge
            continue
        render.centre_mark(sheet, c.x, c.y, r_px)
        n += 1
    return n


def _perp(v):
    v = v.normalized()
    other = Vector((0, 0, 1)) if abs(v.z) < 0.9 else Vector((1, 0, 0))
    return v.cross(other).normalized()


def draw_dims(sheet, dims, scene, cam, fs, placer, model_bbox_px,
              offset=1.0, step=1.0):
    """
    Project and draw each dimension. Returns how many were drawn.

    offset scales how far a dimension sits from the geometry, step scales the stagger used
    when a position is already taken. Both are exposed because no automatic rule suits every
    model: a dense mechanical part wants dimensions pushed well clear, a simple block wants
    them tight.
    """
    t = sheet.theme
    px_w, px_h = sheet.px_w, sheet.px_h
    drawn = 0
    ah = fs * 0.30

    for d in dims:
        if d.kind == "perimeter":
            # A perimeter belongs to a shape, not to a distance, so it gets a leader line
            # out to a label rather than a dimension line with arrows.
            c = _project(scene, cam, d.centre if d.centre else d.a, px_w, px_h)
            bx0, by0, bx1, by1 = model_bbox_px
            away = Vector((c.x - (bx0 + bx1) / 2.0, c.y - (by0 + by1) / 2.0))
            away = away.normalized() if away.length > 1e-6 else Vector((1, -1)).normalized()
            lw = render.text_width(d.label, fs) + fs * 0.7
            lx, ly, ok = placer.place(c.x + away.x * fs * 3.0 * offset,
                                      c.y + away.y * fs * 3.0 * offset,
                                      lw, fs * 1.15, away.x, away.y, fs * 1.4 * step)
            if not ok:
                continue
            sheet.polyline([(c.x, c.y), (lx - away.x * lw * 0.3, ly)], t["thin"],
                           sheet.w("leader"))
            _dim_text(sheet, lx, ly, d.label, fs, t["ink"])
            drawn += 1
            continue

        a = _project(scene, cam, d.a, px_w, px_h)
        b = _project(scene, cam, d.b, px_w, px_h)
        v = b - a
        if v.length < fs * 0.9:      # degenerate in this view; nothing legible to draw
            continue

        if d.kind == "angle" and d.centre is not None:
            c = _project(scene, cam, d.centre, px_w, px_h)
            r = max((a - c).length, (b - c).length) * 0.55
            a1 = math.atan2(a.y - c.y, a.x - c.x)
            a2 = math.atan2(b.y - c.y, b.x - c.x)
            if a2 < a1:
                a1, a2 = a2, a1
            if a2 - a1 > math.pi:
                a1, a2 = a2, a1 + 2 * math.pi
            steps = max(6, int(abs(a2 - a1) * 14))
            arc = [(c.x + r * math.cos(a1 + (a2 - a1) * i / steps),
                    c.y + r * math.sin(a1 + (a2 - a1) * i / steps)) for i in range(steps + 1)]
            sheet.polyline([(c.x, c.y), (a.x, a.y)], t["thin"], sheet.w("ext"))
            sheet.polyline([(c.x, c.y), (b.x, b.y)], t["thin"], sheet.w("ext"))
            sheet.polyline(arc, t["ink"], sheet.w("dim"))
            mid = arc[len(arc) // 2]
            out = Vector((mid[0] - c.x, mid[1] - c.y))
            out = out.normalized() if out.length > 1e-6 else Vector((0, -1))
            lw = render.text_width(d.label, fs) + fs * 0.7
            lx, ly, _ = placer.place(mid[0] + out.x * fs * 1.2 * offset,
                                     mid[1] + out.y * fs * 1.2 * offset,
                                     lw, fs * 1.15, out.x, out.y, fs * 1.1 * step)
            _dim_text(sheet, lx, ly, d.label, fs, t["ink"])
            drawn += 1
            continue

        # linear-family: dimension line parallel to the measurement, offset clear of it
        n = Vector((-v.y, v.x)).normalized()
        bx0, by0, bx1, by1 = model_bbox_px
        bc = Vector(((bx0 + bx1) / 2.0, (by0 + by1) / 2.0))
        mid = (a + b) / 2.0
        if (mid - bc).dot(n) < 0:     # always push away from the model, never across it
            n = -n

        base = fs * 1.6 * offset
        lw = render.text_width(d.label, fs) + fs * 0.7
        lx, ly, ok = placer.place(mid.x + n.x * base, mid.y + n.y * base,
                                  lw, fs * 1.15, n.x, n.y, fs * 1.35 * step)
        if not ok:
            continue
        off = Vector((lx, ly)) - mid
        da, db = a + off, b + off

        sheet.polyline([(a.x, a.y), (da.x + n.x * fs * 0.5, da.y + n.y * fs * 0.5)],
                       t["thin"], sheet.w("ext"))
        sheet.polyline([(b.x, b.y), (db.x + n.x * fs * 0.5, db.y + n.y * fs * 0.5)],
                       t["thin"], sheet.w("ext"))
        sheet.polyline([(da.x, da.y), (db.x, db.y)], t["ink"], sheet.w("dim"))
        u = (db - da).normalized()
        render.arrow(sheet, da.x, da.y, -u.x, -u.y, ah, t["ink"])
        render.arrow(sheet, db.x, db.y, u.x, u.y, ah, t["ink"])

        _dim_text(sheet, lx, ly, d.label, fs, t["ink"])

        # Reserve the dimension LINE too, not just its label. Two dimensions can have
        # labels that clear each other while their lines lie on top of one another.
        lox, hix = min(da.x, db.x), max(da.x, db.x)
        loy, hiy = min(da.y, db.y), max(da.y, db.y)
        placer.reserve((lox, loy - fs * 0.45, hix - lox, (hiy - loy) + fs * 0.9))
        drawn += 1

    return drawn
