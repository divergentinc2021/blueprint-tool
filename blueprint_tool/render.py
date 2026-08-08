"""
Sheet drawing and output backends.

A drawing is built once as a `Sheet` — an ordered list of primitives in page-pixel space
(origin top-left, y down) — and then written by a backend. SVG, PDF and the viewport
overlay all consume the same Sheet, so they cannot disagree about what the drawing
contains; adding a primitive means teaching each backend one more case, not maintaining
three parallel renderers.

All text is ASCII. PDF base-14 fonts are WinAnsi-encoded and the SVG has to survive the
same strings, so a stray "x" or ellipsis is not worth the encoding trouble.
"""

import math
import zlib

# ---------------------------------------------------------------- themes
# Every theme names the same roles, so a drawing keeps its meaning when the paper changes:
# ink carries the object, hidden what is behind it, thin the measuring apparatus, and grid
# the paper itself. accent is used once per sheet, on the detail callouts, so an enlargement
# reads as a reference rather than as more geometry.
THEMES = {
    "white": dict(
        label="White",
        bg="#ffffff", ink="#12203a", hidden="#7d8ba3", thin="#5b6982",
        frame="#12203a", grid="#dbe2ed", accent="#a8442a", faint="#8a97ad",
    ),
    "blueprint": dict(
        label="Blueprint",
        bg="#0f3a72", ink="#ffffff", hidden="#8fb6e4", thin="#bcd6f2",
        frame="#ffffff", grid="#1d5493", accent="#ffc65c", faint="#9dc4ef",
    ),
    "cyanotype": dict(
        label="Cyanotype",
        bg="#08324f", ink="#eaf4fb", hidden="#6fa3c9", thin="#a9cde6",
        frame="#eaf4fb", grid="#124d78", accent="#f0b455", faint="#7fb2d9",
    ),
    "noir": dict(
        label="Noir",
        bg="#08090b", ink="#f4f6f8", hidden="#5f6873", thin="#9aa4b2",
        frame="#f4f6f8", grid="#20262e", accent="#5ad1e6", faint="#7d8794",
    ),
    "sepia": dict(
        label="Sepia",
        bg="#ece0c6", ink="#2f2618", hidden="#9b8a6c", thin="#6d5f47",
        frame="#2f2618", grid="#ddceac", accent="#8c3a1e", faint="#8a7a5e",
    ),
    "steel": dict(
        label="Steel",
        bg="#363c42", ink="#f2f5f7", hidden="#7d8892", thin="#aeb7c0",
        frame="#f2f5f7", grid="#4d555e", accent="#ffb648", faint="#9aa4ab",
    ),
}
THEME_ORDER = ["white", "blueprint", "cyanotype", "noir", "sepia", "steel"]

# Line weights in REAL MILLIMETRES, following the drafting convention that weight encodes
# meaning: the object outline is heaviest, hidden detail lighter, the measuring apparatus
# lighter still, and the paper grid barely there. Pixel constants cannot do this, because
# the same value means a different physical line on A4 and A2.
WEIGHTS_MM = dict(
    frame=0.70, frame_inner=0.25,
    outline=0.50, hidden=0.30, centre=0.18,
    dim=0.20, ext=0.13, leader=0.18,
    grid=0.09, grid_major=0.13,
    block=0.50, block_rule=0.13,
    detail=0.35,
)


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


# ---------------------------------------------------------------- sheet
class Sheet:
    """An ordered list of drawing primitives in page-pixel space (y down)."""

    def __init__(self, px_w, px_h, mm_w, mm_h, theme="white"):
        self.px_w, self.px_h = px_w, px_h
        self.mm_w, self.mm_h = mm_w, mm_h
        self.theme = THEMES[theme]
        self.theme_name = theme
        self.ops = []

    @property
    def px_per_mm(self):
        return self.px_w / float(self.mm_w)

    def w(self, role):
        """Line weight for a drafting role, in pixels for this sheet's real page size."""
        return max(WEIGHTS_MM[role] * self.px_per_mm, 0.6)

    # -- primitives
    def rect(self, x, y, w, h, fill=None, stroke=None, width=2.0):
        self.ops.append(("rect", x, y, w, h, fill, stroke, width))

    def polyline(self, pts, stroke, width=2.0, dash=None):
        if len(pts) >= 2:
            self.ops.append(("polyline", list(pts), stroke, width, dash))

    def line(self, x1, y1, x2, y2, stroke, width=2.0, dash=None):
        self.polyline([(x1, y1), (x2, y2)], stroke, width, dash)

    def polygon(self, pts, fill):
        if len(pts) >= 3:
            self.ops.append(("polygon", list(pts), fill))

    def text(self, x, y, s, size, fill, anchor="start", bold=False, rotate=0, track=0.0):
        """anchor: start | middle | end. rotate: 0 or 90 (90 reads bottom-to-top)."""
        self.ops.append(("text", x, y, ascii_only(s), size, fill, anchor, bold, rotate,
                         track))


    def blit(self, other, dx, dy):
        """Append another sheet's primitives, translated. Used by the plate layout."""
        for op in other.ops:
            k = op[0]
            if k == "rect":
                _, x, y, w, h, fill, stroke, width = op
                self.ops.append(("rect", x + dx, y + dy, w, h, fill, stroke, width))
            elif k == "polyline":
                _, pts, stroke, width, dash = op
                self.ops.append(("polyline", [(p[0] + dx, p[1] + dy) for p in pts],
                                 stroke, width, dash))
            elif k == "polygon":
                _, pts, fill = op
                self.ops.append(("polygon", [(p[0] + dx, p[1] + dy) for p in pts], fill))
            elif k == "text":
                _, x, y, t, size, fill, anchor, bold, rotate, track = op
                self.ops.append(("text", x + dx, y + dy, t, size, fill, anchor, bold,
                                 rotate, track))


def clip_polyline(pts, x0, y0, x1, y1):
    """
    Liang-Barsky clip of a polyline to a rectangle, returning the surviving runs.

    An enlarged detail frames a region, but Line Art still hands back every stroke in the
    scene, so without this the geometry outside the region runs across the whole sheet.
    """
    out, run = [], []
    for i in range(len(pts) - 1):
        seg = _clip_segment(pts[i], pts[i + 1], x0, y0, x1, y1)
        if seg is None:
            if len(run) > 1:
                out.append(run)
            run = []
            continue
        a, b = seg
        if run and abs(run[-1][0] - a[0]) < 1e-6 and abs(run[-1][1] - a[1]) < 1e-6:
            run.append(b)
        else:
            if len(run) > 1:
                out.append(run)
            run = [a, b]
    if len(run) > 1:
        out.append(run)
    return out


def _clip_segment(a, b, x0, y0, x1, y1):
    """Canonical Liang-Barsky. Returns the clipped endpoints, or None if fully outside."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for pk, qk in ((-dx, a[0] - x0), (dx, x1 - a[0]),
                   (-dy, a[1] - y0), (dy, y1 - a[1])):
        if pk == 0:
            if qk < 0:
                return None          # parallel to this edge and outside it
            continue
        r = qk / pk
        if pk < 0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    if t0 > t1:
        return None
    return ((a[0] + dx * t0, a[1] + dy * t0), (a[0] + dx * t1, a[1] + dy * t1))


def ascii_only(s):
    return (str(s).replace("…", "...").replace("×", "x")
            .replace("—", "-").replace("–", "-")
            .replace("°", " deg").replace("ø", "dia ").replace("⌀", "dia ")
            .encode("ascii", "replace").decode("ascii"))


# ---------------------------------------------------------------- text metrics
# Helvetica advance widths per 1000 em, ASCII 32..126. Used to centre and right-align
# text in the PDF backend, which has no notion of a text anchor, and to size the plates
# punched behind dimension labels in both backends.
_HELV = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]


def fmt(value, unit):
    """Numeric formatting for dimension text: fewer decimals on coarse units."""
    if unit in ("mm", "cm"):
        return ("%.1f" % value).rstrip("0").rstrip(".")
    return "%.3f" % value


def text_width(s, size, bold=False, track=0.0):
    total = 0
    for ch in str(s):
        o = ord(ch)
        total += _HELV[o - 32] if 32 <= o <= 126 else 556
    w = total / 1000.0 * size
    if bold:
        w *= 1.06
    return w + track * max(len(str(s)) - 1, 0)


def fit_text(s, size, avail, min_size, bold=False, track=0.0):
    """Shrink, then truncate, so a long string cannot escape its box."""
    while size > min_size and text_width(s, size, bold, track) > avail:
        size *= 0.94
    if text_width(s, size, bold, track) <= avail:
        return s, size
    out = s
    while out and text_width(out + "...", size, bold, track) > avail:
        out = out[:-1]
    return (out + "...") if out else s[:1], size


# ---------------------------------------------------------------- svg backend
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def to_svg(sheet):
    t = sheet.theme
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
           'width="%gmm" height="%gmm" viewBox="0 0 %d %d">'
           % (sheet.mm_w, sheet.mm_h, sheet.px_w, sheet.px_h)]
    for op in sheet.ops:
        k = op[0]
        if k == "rect":
            _, x, y, w, h, fill, stroke, width = op
            attrs = 'x="%.1f" y="%.1f" width="%.1f" height="%.1f"' % (x, y, w, h)
            attrs += ' fill="%s"' % (fill if fill else "none")
            if stroke:
                attrs += ' stroke="%s" stroke-width="%.2f"' % (stroke, width)
            out.append("<rect %s/>" % attrs)
        elif k == "polyline":
            _, pts, stroke, width, dash = op
            d = "M%.2f,%.2f" % pts[0] + "".join("L%.2f,%.2f" % p for p in pts[1:])
            attrs = 'd="%s" fill="none" stroke="%s" stroke-width="%.2f"' % (d, stroke, width)
            attrs += ' stroke-linecap="round" stroke-linejoin="round"'
            if dash:
                attrs += ' stroke-dasharray="%s"' % " ".join("%g" % v for v in dash)
            out.append("<path %s/>" % attrs)
        elif k == "polygon":
            _, pts, fill = op
            out.append('<polygon points="%s" fill="%s" stroke="none"/>'
                       % (" ".join("%.1f,%.1f" % p for p in pts), fill))
        elif k == "text":
            _, x, y, s, size, fill, anchor, bold, rotate, track = op
            a = {"start": "start", "middle": "middle", "end": "end"}[anchor]
            attrs = ('font-family="Helvetica,Arial,sans-serif" font-size="%.1f" fill="%s" '
                     'text-anchor="%s"' % (size, fill, a))
            if bold:
                attrs += ' font-weight="700"'
            if track:
                attrs += ' letter-spacing="%.2f"' % track
            if rotate:
                out.append('<text x="0" y="0" %s transform="translate(%.1f,%.1f) '
                           'rotate(-90)">%s</text>' % (attrs, x, y, _esc(s)))
            else:
                out.append('<text x="%.1f" y="%.1f" %s>%s</text>' % (x, y, attrs, _esc(s)))
    out.append("</svg>")
    return "\n".join(out)


def write_svg(path, sheet):
    with open(path, "w") as f:
        f.write(to_svg(sheet))


# ---------------------------------------------------------------- pdf backend
# A PDF is a small enough format to emit directly, which keeps this dependency-free —
# Blender ships no PDF writer and reportlab is not available inside it.
def _pdf_str(s):
    return "(%s)" % (str(s).replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)"))


def _pdf_colour(h):
    return "%.4f %.4f %.4f" % hex_rgb(h)


def _sheet_content(sheet):
    """One PDF content stream for one sheet. PDF user space is y-up, ours is y-down."""
    pt_w = sheet.mm_w * 72.0 / 25.4
    pt_h = sheet.mm_h * 72.0 / 25.4
    sx, sy = pt_w / sheet.px_w, pt_h / sheet.px_h

    def X(v):
        return v * sx

    def Y(v):
        return pt_h - v * sy

    o = ["q", "1 J", "1 j"]  # round caps and joins, matching the SVG
    for op in sheet.ops:
        k = op[0]
        if k == "rect":
            _, x, y, w, h, fill, stroke, width = op
            if fill:
                o.append("%s rg" % _pdf_colour(fill))
            if stroke:
                o.append("%s RG" % _pdf_colour(stroke))
                o.append("%.3f w" % (width * sx))
            o.append("%.3f %.3f %.3f %.3f re" % (X(x), Y(y + h), w * sx, h * sy))
            o.append("B" if (fill and stroke) else ("f" if fill else "S"))
        elif k == "polyline":
            _, pts, stroke, width, dash = op
            o.append("%s RG" % _pdf_colour(stroke))
            o.append("%.3f w" % max(width * sx, 0.05))
            o.append("[%s] 0 d" % (" ".join("%.2f" % (v * sx) for v in dash) if dash else ""))
            o.append("%.3f %.3f m" % (X(pts[0][0]), Y(pts[0][1])))
            for p in pts[1:]:
                o.append("%.3f %.3f l" % (X(p[0]), Y(p[1])))
            o.append("S")
            o.append("[] 0 d")
        elif k == "polygon":
            _, pts, fill = op
            o.append("%s rg" % _pdf_colour(fill))
            o.append("%.3f %.3f m" % (X(pts[0][0]), Y(pts[0][1])))
            for p in pts[1:]:
                o.append("%.3f %.3f l" % (X(p[0]), Y(p[1])))
            o.append("h f")
        elif k == "text":
            _, x, y, s, size, fill, anchor, bold, rotate, track = op
            fsize = size * sy
            tr = track * sx
            w = text_width(s, fsize, bold, tr)
            # PDF has no text anchor; shift the origin by the measured width instead
            off = 0.0 if anchor == "start" else (-w / 2.0 if anchor == "middle" else -w)
            o.append("BT")
            o.append("/%s %.3f Tf" % ("FB" if bold else "FR", fsize))
            o.append("%.3f Tc" % tr)
            o.append("%s rg" % _pdf_colour(fill))
            if rotate:  # reads bottom-to-top; PDF is y-up so this is +90 degrees
                o.append("0 1 -1 0 %.3f %.3f Tm" % (X(x), Y(y) + off))
            else:
                o.append("1 0 0 1 %.3f %.3f Tm" % (X(x) + off, Y(y)))
            o.append("%s Tj" % _pdf_str(s))
            o.append("0 Tc")
            o.append("ET")
    o.append("Q")
    return "\n".join(o).encode("latin-1", "replace")


def write_pdf(path, sheets, title="Blueprint", compress=True):
    """Write all sheets into one multi-page PDF."""
    if not sheets:
        raise ValueError("no sheets to write")

    objects = []      # 1-indexed body; objects[i] is object number i+1

    def add(body):
        objects.append(body)
        return len(objects)

    font_r = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                 b"/Encoding /WinAnsiEncoding >>")
    font_b = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                 b"/Encoding /WinAnsiEncoding >>")

    pages_id = add(b"")  # placeholder, filled once the page ids are known
    page_ids = []
    for sh in sheets:
        raw = _sheet_content(sh)
        if compress:
            data = zlib.compress(raw)
            extra = b"/Filter /FlateDecode "
        else:
            data, extra = raw, b""
        content_id = add(b"<< " + extra + ("/Length %d >>" % len(data)).encode() +
                         b"\nstream\n" + data + b"\nendstream")
        pt_w = sh.mm_w * 72.0 / 25.4
        pt_h = sh.mm_h * 72.0 / 25.4
        page_id = add(("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.3f %.3f] "
                       "/Resources << /Font << /FR %d 0 R /FB %d 0 R >> >> "
                       "/Contents %d 0 R >>"
                       % (pages_id, pt_w, pt_h, font_r, font_b, content_id)).encode())
        page_ids.append(page_id)

    objects[pages_id - 1] = ("<< /Type /Pages /Count %d /Kids [%s] >>"
                             % (len(page_ids),
                                " ".join("%d 0 R" % p for p in page_ids))).encode()
    catalog = add(("<< /Type /Catalog /Pages %d 0 R >>" % pages_id).encode())
    info = add(("<< /Title %s /Producer (blueprint-tool) >>" % _pdf_str(title)).encode())

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += ("%d 0 obj\n" % i).encode() + body + b"\nendobj\n"

    xref_at = len(out)
    n = len(objects) + 1
    out += ("xref\n0 %d\n" % n).encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += ("%010d 00000 n \n" % off).encode()
    out += ("trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (n, catalog, info, xref_at)).encode()

    with open(path, "wb") as f:
        f.write(bytes(out))
    return path


# ---------------------------------------------------------------- shared furniture
def draw_frame(sheet, fs):
    """
    Sheet border: a light trim rule at the paper edge and a heavy drawing border inside it.

    A single rule reads as a box drawn on a page. The pair reads as a sheet with a drawing
    area, which is what it is, and it gives the title block something to sit against.
    """
    t = sheet.theme
    mm = sheet.px_per_mm
    sheet.rect(0, 0, sheet.px_w, sheet.px_h, fill=t["bg"])
    trim = 4.0 * mm
    border = 9.0 * mm
    sheet.rect(trim, trim, sheet.px_w - 2 * trim, sheet.px_h - 2 * trim,
               fill=None, stroke=t["grid"], width=sheet.w("frame_inner"))
    sheet.rect(border, border, sheet.px_w - 2 * border, sheet.px_h - 2 * border,
               fill=None, stroke=t["frame"], width=sheet.w("frame"))
    return border


def draw_grid(sheet, fs, step_mm=10.0, bold_every=5):
    """Faint graticule, the hallmark of blueprint paper. Spacing is in real millimetres."""
    t = sheet.theme
    mm = sheet.px_per_mm
    step = step_mm * mm
    if step < 6:                      # denser than this just muddies the page
        return
    inset = 9.0 * mm
    for axis in (0, 1):
        limit = (sheet.px_w if axis == 0 else sheet.px_h) - inset
        i, v = 0, inset
        while v <= limit:
            major = (i % bold_every == 0)
            colour = t["grid"]
            width = sheet.w("grid_major" if major else "grid")
            if axis == 0:
                sheet.line(v, inset, v, sheet.px_h - inset, colour, width)
            else:
                sheet.line(inset, v, sheet.px_w - inset, v, colour, width)
            v += step
            i += 1


def caption(sheet, cx, y, text, fs, sub=None):
    """View name under a drawing, in the drafting idiom: spaced caps with a short rule."""
    t = sheet.theme
    size = fs * 0.82
    track = size * 0.16
    w = text_width(text.upper(), size, True, track)
    sheet.text(cx, y, text.upper(), size, t["ink"], anchor="middle", bold=True, track=track)
    sheet.line(cx - w / 2.0, y + size * 0.42, cx + w / 2.0, y + size * 0.42,
               t["ink"], sheet.w("centre"))
    if sub:
        sheet.text(cx, y + size * 1.6, sub, size * 0.82, t["thin"], anchor="middle",
                   track=size * 0.1)


def centre_mark(sheet, cx, cy, radius, colour=None):
    """Dash-dot cross through a circular feature, as a drawing would carry."""
    t = sheet.theme
    r = radius * 1.22
    gap, dash, dot = r * 0.10, r * 0.42, r * 0.06
    w = sheet.w("centre")
    c = colour or t["thin"]
    for dx, dy in ((1, 0), (0, 1)):
        for sgn in (1, -1):
            x0 = cx + dx * sgn * gap
            y0 = cy + dy * sgn * gap
            sheet.polyline([(x0, y0), (x0 + dx * sgn * dash, y0 + dy * sgn * dash)], c, w)
            s0 = gap + dash + gap
            sheet.polyline([(cx + dx * sgn * s0, cy + dy * sgn * s0),
                            (cx + dx * sgn * (s0 + dot), cy + dy * sgn * (s0 + dot))], c, w)


def detail_callout(sheet, cx, cy, r, tag, fs):
    """Ring the enlarged region and letter it, so the inset reads as a reference."""
    t = sheet.theme
    steps = 48
    ring = [(cx + r * math.cos(2 * math.pi * i / steps),
             cy + r * math.sin(2 * math.pi * i / steps)) for i in range(steps + 1)]
    sheet.polyline(ring, t["accent"], sheet.w("detail"), dash=(r * 0.16, r * 0.10))
    # leader out to the upper right, then a short horizontal shoulder to carry the letter
    a = math.radians(-38)
    ex, ey = cx + r * math.cos(a), cy + r * math.sin(a)
    lx, ly = ex + fs * 1.5, ey - fs * 1.1
    sheet.polyline([(ex, ey), (lx, ly), (lx + fs * 0.8, ly)], t["accent"], sheet.w("leader"))
    sheet.text(lx + fs * 1.05, ly + fs * 0.34, tag.upper(), fs * 1.0, t["accent"],
               bold=True, track=fs * 0.06)


def title_block_height(fs, n_fields):
    """Height the block will occupy, so a layout can reserve exactly that and no more."""
    return fs * 2.6 + fs * 2.0 * ((max(n_fields, 1) + 1) // 2)


def draw_title_block(sheet, fs, rows, title=None):
    """
    Title block: a heading band over a two-column field grid.

    Fields are (label, value) pairs supplied by the caller, so the sheet can carry whatever
    the drawing actually needs — drawing number, revision, material, client — rather than a
    fixed set baked in here.
    """
    t = sheet.theme
    mm = sheet.px_per_mm
    border = 9.0 * mm
    pairs = [(k, v) for k, v in rows if k]
    heading = title if title is not None else next((v for k, v in rows if not k), "")

    bw = fs * 22.0
    head_h = fs * 2.6
    # the label sits above its value inside one cell, so the row has to clear both
    # baselines plus the value's descender - 1.5 put them on top of each other
    row_h = fs * 2.0
    n_rows = (len(pairs) + 1) // 2
    bh = head_h + row_h * n_rows
    x = sheet.px_w - border - bw
    y = sheet.px_h - border - bh

    sheet.rect(x, y, bw, bh, fill=t["bg"], stroke=t["frame"], width=sheet.w("block"))
    sheet.line(x, y + head_h, x + bw, y + head_h, t["frame"], sheet.w("block"))

    shown, size = fit_text(str(heading).upper(), fs * 1.32, bw - fs * 1.6, fs * 0.72,
                           bold=True, track=fs * 0.05)
    sheet.text(x + fs * 0.8, y + head_h * 0.66, shown, size, t["ink"], bold=True,
               track=fs * 0.05)

    half = bw / 2.0
    for i, (k, v) in enumerate(pairs):
        r, c = divmod(i, 2)
        cx = x + c * half
        cy = y + head_h + r * row_h
        if c == 1:
            sheet.line(cx, cy, cx, cy + row_h, t["frame"], sheet.w("block_rule"))
        if r:
            sheet.line(x, cy, x + bw, cy, t["frame"], sheet.w("block_rule"))
        sheet.text(cx + fs * 0.5, cy + row_h * 0.38, str(k).upper(), fs * 0.58, t["thin"],
                   track=fs * 0.08)
        val, vsize = fit_text(str(v), fs * 0.84, half - fs * 1.0, fs * 0.5)
        sheet.text(cx + fs * 0.5, cy + row_h * 0.84, val, vsize, t["ink"])
    return (x, y, bw, bh)


def arrow(sheet, x, y, dx, dy, size, fill):
    """Slender filled arrowhead at (x, y) pointing along (dx, dy)."""
    px, py = -dy, dx
    sheet.polygon([(x, y),
                   (x - dx * size * 2.6 + px * size * 0.42,
                    y - dy * size * 2.6 + py * size * 0.42),
                   (x - dx * size * 2.6 - px * size * 0.42,
                    y - dy * size * 2.6 - py * size * 0.42)],
                  fill)


def label_plate(sheet, cx, cy, s, size, rotate=0, track=0.0):
    """Punch the background behind a label so the dimension line does not run through it."""
    w = text_width(s, size, False, track) + size * 0.8
    h = size * 1.2
    if rotate:
        sheet.rect(cx - h * 0.5, cy - w * 0.5, h, w, fill=sheet.theme["bg"])
    else:
        sheet.rect(cx - w * 0.5, cy - h * 0.64, w, h, fill=sheet.theme["bg"])


def hypot(ax, ay, bx, by):
    return math.hypot(bx - ax, by - ay)
