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
THEMES = {
    "white": dict(
        label="White",
        bg="#ffffff",
        ink="#1a2b4c",        # linework, dimension lines, text
        faint="#8a97ad",      # extension lines, hidden lines, secondary text
        hidden="#8a97ad",
        frame="#1a2b4c",
        grid="#dfe4ec",       # graticule, barely there
    ),
    "blueprint": dict(
        label="Blueprint",
        bg="#12386e",
        ink="#ffffff",
        faint="#9dc4ef",
        hidden="#9dc4ef",
        frame="#ffffff",
        grid="#1d4c8a",
    ),
    "cyanotype": dict(
        label="Cyanotype",
        bg="#0b3d62",         # deeper Prussian blue, the classic print look
        ink="#f2f7fb",
        faint="#7fb2d9",
        hidden="#5d93bd",
        frame="#f2f7fb",
        grid="#154f7c",
    ),
    "sepia": dict(
        label="Sepia",
        bg="#e8dcc0",         # aged parchment
        ink="#3a2f22",
        faint="#8a7a5e",
        hidden="#a2937a",
        frame="#3a2f22",
        grid="#d6c8a6",
    ),
    "steel": dict(
        label="Steel",
        bg="#3d4348",
        ink="#eef1f3",
        faint="#9aa4ab",
        hidden="#7f8a92",
        frame="#eef1f3",
        grid="#4a5157",
    ),
    "dark": dict(
        label="Dark",
        bg="#14161a",
        ink="#e8ecf2",
        faint="#7d8794",
        hidden="#7d8794",
        frame="#e8ecf2",
        grid="#22262c",
    ),
}
THEME_ORDER = ["white", "blueprint", "cyanotype", "sepia", "steel", "dark"]


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

    def text(self, x, y, s, size, fill, anchor="start", bold=False, rotate=0):
        """anchor: start | middle | end. rotate: 0 or 90 (90 reads bottom-to-top)."""
        self.ops.append(("text", x, y, ascii_only(s), size, fill, anchor, bold, rotate))


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
                _, x, y, t, size, fill, anchor, bold, rotate = op
                self.ops.append(("text", x + dx, y + dy, t, size, fill, anchor, bold, rotate))


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


def text_width(s, size, bold=False):
    total = 0
    for ch in str(s):
        o = ord(ch)
        total += _HELV[o - 32] if 32 <= o <= 126 else 556
    w = total / 1000.0 * size
    return w * 1.06 if bold else w


def fit_text(s, size, avail, min_size, bold=False):
    """Shrink, then truncate, so a long string cannot escape its box."""
    while size > min_size and text_width(s, size, bold) > avail:
        size *= 0.94
    if text_width(s, size, bold) <= avail:
        return s, size
    out = s
    while out and text_width(out + "...", size, bold) > avail:
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
            _, x, y, s, size, fill, anchor, bold, rotate = op
            a = {"start": "start", "middle": "middle", "end": "end"}[anchor]
            attrs = ('font-family="Helvetica,Arial,sans-serif" font-size="%.1f" fill="%s" '
                     'text-anchor="%s"' % (size, fill, a))
            if bold:
                attrs += ' font-weight="700"'
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
            _, x, y, s, size, fill, anchor, bold, rotate = op
            fsize = size * sy
            w = text_width(s, fsize, bold)
            # PDF has no text anchor; shift the origin by the measured width instead
            off = 0.0 if anchor == "start" else (-w / 2.0 if anchor == "middle" else -w)
            o.append("BT")
            o.append("/%s %.3f Tf" % ("FB" if bold else "FR", fsize))
            o.append("%s rg" % _pdf_colour(fill))
            if rotate:  # reads bottom-to-top; PDF is y-up so this is +90 degrees
                o.append("0 1 -1 0 %.3f %.3f Tm" % (X(x), Y(y) + off))
            else:
                o.append("1 0 0 1 %.3f %.3f Tm" % (X(x) + off, Y(y)))
            o.append("%s Tj" % _pdf_str(s))
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
    t = sheet.theme
    sheet.rect(0, 0, sheet.px_w, sheet.px_h, fill=t["bg"])
    sheet.rect(fs, fs, sheet.px_w - 2 * fs, sheet.px_h - 2 * fs,
               fill=None, stroke=t["frame"], width=3)


def draw_grid(sheet, fs, step_mm=10.0, bold_every=5):
    """Faint graticule, the hallmark of blueprint paper. Spacing is in real millimetres."""
    t = sheet.theme
    colour = t.get("grid", t["faint"])
    px_per_mm = sheet.px_w / sheet.mm_w
    step = step_mm * px_per_mm
    if step < 6:                      # denser than this just muddies the page
        return
    inset = fs
    i = 0
    x = inset
    while x <= sheet.px_w - inset:
        sheet.line(x, inset, x, sheet.px_h - inset, colour,
                   1.6 if (i % bold_every == 0) else 0.8)
        x += step
        i += 1
    i = 0
    y = inset
    while y <= sheet.px_h - inset:
        sheet.line(inset, y, sheet.px_w - inset, y, colour,
                   1.6 if (i % bold_every == 0) else 0.8)
        y += step
        i += 1


def draw_title_block(sheet, fs, rows):
    t = sheet.theme
    bw, bh = fs * 20.0, fs * (1.6 + 1.35 * len(rows))
    x, y = sheet.px_w - bw - fs * 1.6, sheet.px_h - bh - fs * 1.6
    sheet.rect(x, y, bw, bh, fill=t["bg"], stroke=t["frame"], width=2.5)
    ty = y + fs * 1.5
    for i, (k, v) in enumerate(rows):
        if i == 0:
            shown, size = fit_text(str(v), fs * 1.05, bw - fs * 1.4, fs * 0.62, bold=True)
            sheet.text(x + fs * 0.7, ty, shown, size, t["ink"], bold=True)
            sheet.line(x, ty + fs * 0.45, x + bw, ty + fs * 0.45, t["frame"], 1.5)
            ty += fs * 1.75
            continue
        sheet.text(x + fs * 0.7, ty, k, fs * 0.8, t["faint"])
        shown, size = fit_text(str(v), fs * 0.8, bw - fs * 1.4 - text_width(k, fs * 0.8),
                               fs * 0.55)
        sheet.text(x + bw - fs * 0.7, ty, shown, size, t["ink"], anchor="end")
        ty += fs * 1.35
    return (x, y, bw, bh)


def arrow(sheet, x, y, dx, dy, size, fill):
    """Solid arrowhead at (x, y) pointing along (dx, dy)."""
    px, py = -dy, dx
    sheet.polygon([(x, y),
                   (x - dx * size * 2 + px * size * 0.5, y - dy * size * 2 + py * size * 0.5),
                   (x - dx * size * 2 - px * size * 0.5, y - dy * size * 2 - py * size * 0.5)],
                  fill)


def label_plate(sheet, cx, cy, s, size, rotate=0):
    """Punch the background behind a label so the dimension line does not run through it."""
    w = text_width(s, size) + size * 0.7
    h = size * 1.15
    if rotate:
        sheet.rect(cx - h * 0.5, cy - w * 0.5, h, w, fill=sheet.theme["bg"])
    else:
        sheet.rect(cx - w * 0.5, cy - h * 0.62, w, h, fill=sheet.theme["bg"])


def hypot(ax, ay, bx, by):
    return math.hypot(bx - ax, by - ay)
