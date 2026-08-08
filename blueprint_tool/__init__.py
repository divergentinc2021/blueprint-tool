"""
Blueprint Tool — Blender add-on.

3D Viewport > sidebar (N) > Blueprint. Measures the scene and writes orthographic
blueprint drawings: hidden-line removed, dimensioned, with a title block, as SVG and PDF.

The work happens in core.py, which the command-line entry point shares, so the add-on and
the CLI cannot drift apart.
"""

bl_info = {
    "name": "Blueprint Tool",
    "author": "Laurent de Laroche Souvestre",
    "version": (1, 4, 0),
    "doc_url": "https://github.com/divergentinc2021/blueprint-tool",
    "blender": (4, 5, 0),
    "location": "3D Viewport > Sidebar (N) > Blueprint",
    "description": "Measure a model and generate dimensioned orthographic blueprint drawings",
    "category": "Object",
}

import os
import traceback

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       IntProperty, PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup, UIList

from . import annotate, core, preview, render, updates

if "bpy" in locals():  # support Reload Scripts
    import importlib
    render = importlib.reload(render)
    annotate = importlib.reload(annotate)
    core = importlib.reload(core)
    preview = importlib.reload(preview)
    updates = importlib.reload(updates)


UNIT_ITEMS = [
    ("m", "Metres", "Report dimensions in metres"),
    ("cm", "Centimetres", "Report dimensions in centimetres"),
    ("mm", "Millimetres", "Report dimensions in millimetres"),
    ("in", "Inches", "Report dimensions in inches"),
    ("ft", "Feet", "Report dimensions in feet"),
]
PAGE_ITEMS = [
    ("a4l", "A4 Landscape", "297 x 210 mm"),
    ("a4p", "A4 Portrait", "210 x 297 mm"),
    ("a3l", "A3 Landscape", "420 x 297 mm"),
    ("a3p", "A3 Portrait", "297 x 420 mm"),
    ("a2l", "A2 Landscape", "594 x 420 mm"),
]
# Built from render.THEMES so the two cannot drift: a hand-kept copy of this list already
# went stale once, leaving a theme in the menu that no longer existed.
THEME_DESCRIPTIONS = {
    "white": "Dark lines on white paper",
    "blueprint": "White lines on traditional blueprint blue",
    "cyanotype": "White lines on deep Prussian blue, the classic print look",
    "noir": "Fine light lines on near-black, technical-cover style",
    "sepia": "Dark lines on aged parchment",
    "steel": "Light lines on gunmetal grey",
}
THEME_ITEMS = [(k, render.THEMES[k]["label"], THEME_DESCRIPTIONS.get(k, ""))
               for k in render.THEME_ORDER]
LAYOUT_ITEMS = [
    ("sheet", "One Per Page", "A separate page for each view"),
    ("plate", "Single Plate", "Every view laid out on one sheet, poster style"),
    ("projection", "Projection", "One sheet with the views aligned in standard "
                                 "multiview projection"),
]
ANGLE_ITEMS = [
    ("third", "Third Angle", "US convention: each view sits on the side it is seen from"),
    ("first", "First Angle", "ISO/European convention: each view sits on the opposite side"),
]
KIND_ITEMS = [
    ("linear", "Linear", "Width, height and depth extents"),
    ("length", "Length", "Distance between two points, such as an edge"),
    ("diameter", "Diameter", "Across a circular feature"),
    ("radius", "Radius", "From the centre of a circular feature"),
    ("angle", "Angular", "Angle between two selected edges"),
    ("perimeter", "Perimeter", "Around a selected face or closed edge loop"),
]


class BlueprintField(PropertyGroup):
    """One title block row. An empty value is filled in from the drawing."""
    label: StringProperty(name="Label", default="", update=lambda s, c: preview.mark_stale())
    value: StringProperty(
        name="Value", default="",
        description="Leave blank to fill in from the drawing when the label is one of: "
                    "View, Scale, Overall, Unit, Page, Date, Projection, Sheet, Drawn",
        update=lambda s, c: preview.mark_stale())


class BlueprintDetail(PropertyGroup):
    """A detail callout: some objects, ringed on a parent view and enlarged beside it."""
    name: StringProperty(name="Tag", default="A", update=lambda s, c: preview.mark_stale())
    objects: StringProperty(name="Objects", default="",
                            update=lambda s, c: preview.mark_stale())
    on: StringProperty(name="On", default="front",
                       update=lambda s, c: preview.mark_stale())


class BlueprintObject(PropertyGroup):
    """One row of the measured-objects list."""
    name: StringProperty(name="Name")
    use: BoolProperty(name="Measure", default=True, update=lambda s, c: preview.mark_stale())


class BlueprintPrefs(bpy.types.AddonPreferences):
    bl_idname = __name__

    feed_url: StringProperty(
        name="Update Feed",
        description="URL returning the latest release. A GitHub releases API URL or a "
                    "plain JSON file with a \"version\" field both work",
        default=updates.DEFAULT_FEED)
    check_on_start: BoolProperty(
        name="Check on Startup",
        description="Check once when Blender loads the add-on",
        default=False)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "check_on_start")
        col.prop(self, "feed_url")
        if not getattr(bpy.app, "online_access", True):
            col.label(text="Allow Online Access is off in Preferences > System",
                      icon="ERROR")


def _prefs():
    try:
        return bpy.context.preferences.addons[__name__].preferences
    except (KeyError, AttributeError):
        return None


def _stale(self, context):
    """Any option change invalidates the cached preview."""
    preview.mark_stale()


# ---------------------------------------------------------------- properties
class BlueprintSettings(PropertyGroup):
    use_current_scene: BoolProperty(
        name="Use Current Scene",
        description="Draw whatever is already in the scene. Turn off to import a file first",
        default=True, update=_stale,
    )
    source: StringProperty(name="Model", description="Model to import and draw",
                           subtype="FILE_PATH")
    out_dir: StringProperty(
        name="Output", description="Folder for the drawings and dimensions.json",
        subtype="DIR_PATH", default="//blueprints/")
    title: StringProperty(name="Title",
                          description="Title block heading. Blank uses the file name",
                          update=_stale)

    unit: EnumProperty(name="Unit", items=UNIT_ITEMS, default="mm", update=_stale)
    page: EnumProperty(name="Page", items=PAGE_ITEMS, default="a3l", update=_stale)
    theme: EnumProperty(name="Theme", items=THEME_ITEMS, default="white", update=_stale)
    layout: EnumProperty(name="Layout", items=LAYOUT_ITEMS, default="sheet", update=_stale)
    angle: EnumProperty(name="Angle", items=ANGLE_ITEMS, default="third", update=_stale)
    uniform_scale: BoolProperty(
        name="Uniform Scale",
        description="One scale across every view. Framing each view to its own extents "
                    "makes the same model print at a different ratio per view",
        default=True, update=_stale)
    grid: BoolProperty(
        name="Grid", description="Faint graticule behind the drawing, like blueprint paper",
        default=False, update=_stale)

    margin: FloatProperty(
        name="Margin", description="Fraction of the page kept clear for dimensions "
                                   "and the title block",
        default=0.18, min=0.02, max=0.45, subtype="FACTOR", update=_stale)
    # plain degrees rather than subtype ANGLE, which stores radians and would need
    # converting on every read for no gain
    crease: FloatProperty(
        name="Crease",
        description="Edges whose faces meet at an angle BELOW this become lines, in "
                    "degrees. Higher values draw more interior detail",
        default=140.0, min=0.0, max=180.0, update=_stale)
    centre_marks: BoolProperty(
        name="Centres",
        description="Dash-dot cross through circular features the view sees face-on",
        default=True, update=_stale)
    silhouette: BoolProperty(
        name="Silhouette",
        description="Add the true outline of curved surfaces. Line Art alone draws no "
                    "contour for a smooth surface, so a cylinder comes out as two rings "
                    "with no sides",
        default=True, update=_stale)
    hidden: BoolProperty(
        name="Hidden",
        description="Draw occluded edges as dashed lines. Roughly doubles generation time",
        default=True, update=_stale)
    dims: BoolProperty(name="Dims", description="Draw dimension lines and values",
                       default=True, update=_stale)
    png: BoolProperty(name="PNG", description="Also render a shaded reference image per view",
                      default=False)
    pdf: BoolProperty(name="PDF", description="Also write one multi-page PDF of all views",
                      default=True)

    # -- dimension sources
    dim_overall: BoolProperty(
        name="Overall", description="The model's total extents in each view",
        default=True, update=_stale)
    dim_objects: BoolProperty(
        name="Selected Objects",
        description="Bounding box of each selected object, in Object Mode",
        default=False, update=_stale)
    dim_edges: BoolProperty(
        name="Selected Edges",
        description="Length of each selected mesh edge. Two selected edges also give the "
                    "angle between them; a selected circular loop gives its diameter",
        default=False, update=_stale)
    dim_auto: BoolProperty(
        name="Auto Circles",
        description="Find circular features automatically and dimension them",
        default=False, update=_stale)
    dim_empties: BoolProperty(
        name="Empties",
        description="Distance between Empties, paired in name order: 1st to 2nd, "
                    "3rd to 4th, and so on",
        default=False, update=_stale)
    dim_perimeter: BoolProperty(
        name="Perimeter",
        description="Perimeter around each selected face and closed loop of selected edges",
        default=False, update=_stale)
    dim_radius_not_dia: BoolProperty(
        name="Radius", description="Circular features as radius instead of diameter",
        default=False, update=_stale)
    use_object_filter: BoolProperty(
        name="Limit to Checked Objects",
        description="Measure only the objects ticked below. Off measures everything",
        default=False, update=_stale)
    objects: CollectionProperty(type=BlueprintObject)
    objects_index: IntProperty(default=0)
    fields: CollectionProperty(type=BlueprintField)
    fields_index: IntProperty(default=0)
    details: CollectionProperty(type=BlueprintDetail)
    details_index: IntProperty(default=0)

    dim_offset: FloatProperty(
        name="Offset", description="How far dimensions sit from the geometry",
        default=1.0, min=0.2, max=6.0, update=_stale)
    dim_step: FloatProperty(
        name="Spread", description="How far apart dimensions are pushed when they collide",
        default=1.0, min=0.2, max=6.0, update=_stale)
    dim_text: FloatProperty(
        name="Text Size", description="Dimension text size",
        default=1.0, min=0.4, max=3.0, update=_stale)

    preview_size: FloatProperty(
        name="Size", description="How much of the viewport the preview occupies",
        default=0.42, min=0.15, max=1.0, subtype="FACTOR",
        update=lambda s, c: preview.set_options(size=s.preview_size))
    preview_corner: EnumProperty(
        name="Corner", description="Where the preview sits in the viewport",
        items=[("BOTTOM_RIGHT", "Bottom Right", ""), ("BOTTOM_LEFT", "Bottom Left", ""),
               ("TOP_RIGHT", "Top Right", ""), ("TOP_LEFT", "Top Left", ""),
               ("CENTER", "Centre", "")],
        default="BOTTOM_RIGHT",
        update=lambda s, c: preview.set_options(corner=s.preview_corner))
    preview_opacity: FloatProperty(
        name="Opacity", description="Fade the preview back over the model",
        default=1.0, min=0.15, max=1.0, subtype="FACTOR",
        update=lambda s, c: preview.set_options(opacity=s.preview_opacity))

    preview_view: EnumProperty(
        name="Preview",
        items=[(v, v.capitalize(), "Preview the %s view" % v) for v in core.VIEW_ORDER],
        default="front", update=_stale)

    last_result: StringProperty(default="")
    last_output: StringProperty(default="")


for _v in core.VIEW_ORDER:
    BlueprintSettings.__annotations__["view_" + _v] = BoolProperty(
        name=_v.capitalize(), default=_v in ("front", "right", "top", "iso"))
for _k, _lbl, _desc in KIND_ITEMS:
    BlueprintSettings.__annotations__["kind_" + _k] = BoolProperty(
        name=_lbl, description=_desc, default=True, update=_stale)


def _opts(s, views):
    """Translate the property group into core.generate() keyword arguments."""
    kinds = [k for k, _l, _d in KIND_ITEMS if getattr(s, "kind_" + k)]
    return dict(
        use_current_scene=s.use_current_scene,
        source=(None if s.use_current_scene else bpy.path.abspath(s.source)),
        out_dir=bpy.path.abspath(s.out_dir),
        views=views, unit=s.unit, page=s.page, theme=s.theme,
        layout=s.layout, grid=s.grid,
        margin=s.margin, crease=s.crease, silhouette=s.silhouette,
        hidden=s.hidden, dims=s.dims, png=s.png, pdf=s.pdf,
        title=(s.title or None),
        angle=s.angle, uniform_scale=s.uniform_scale,
        dim_overall=s.dim_overall, dim_objects=s.dim_objects, dim_edges=s.dim_edges,
        dim_auto=s.dim_auto, dim_empties=s.dim_empties, dim_perimeter=s.dim_perimeter,
        dim_radius_not_dia=s.dim_radius_not_dia,
        dim_objects_only=([i.name for i in s.objects if i.use]
                          if s.use_object_filter else None),
        dim_offset=s.dim_offset, dim_step=s.dim_step, dim_text=s.dim_text,
        centre_marks=s.centre_marks,
        fields=([(f.label, f.value or None) for f in s.fields if f.label.strip()]
                or None),
        details=([{"name": dt.name,
                   "objects": [n.strip() for n in dt.objects.split(",") if n.strip()],
                   "on": dt.on}
                  for dt in s.details if dt.objects.strip()] or None),
        dim_kinds=(kinds if len(kinds) < len(KIND_ITEMS) else None),
    )


# ---------------------------------------------------------------- object list
class BLUEPRINT_UL_objects(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        row = layout.row(align=True)
        row.prop(item, "use", text="")
        obj = context.scene.objects.get(item.name)
        row.label(text=item.name, icon="MESH_DATA" if obj else "ERROR")
        if obj:
            row.label(text="%d" % len(obj.data.polygons))


class BLUEPRINT_OT_sync_objects(Operator):
    bl_idname = "blueprint.sync_objects"
    bl_label = "Refresh List"
    bl_description = "Match the list to the mesh objects in the scene, keeping the ticks"

    def execute(self, context):
        s = context.scene.blueprint
        # keep existing ticks so a refresh after editing the scene does not silently
        # re-enable objects the user deliberately turned off
        remembered = {i.name: i.use for i in s.objects}
        s.objects.clear()
        for o in core.mesh_objects(context.scene):
            item = s.objects.add()
            item.name = o.name
            item.use = remembered.get(o.name, True)
        preview.mark_stale()
        self.report({"INFO"}, "%d object%s" % (len(s.objects),
                                               "" if len(s.objects) == 1 else "s"))
        return {"FINISHED"}


class BLUEPRINT_OT_object_ticks(Operator):
    bl_idname = "blueprint.object_ticks"
    bl_label = "Set Ticks"
    bl_description = "Tick all, none, or only what is selected in the viewport"

    mode: StringProperty(default="all")

    def execute(self, context):
        s = context.scene.blueprint
        selected = {o.name for o in context.selected_objects}
        for item in s.objects:
            if self.mode == "all":
                item.use = True
            elif self.mode == "none":
                item.use = False
            else:
                item.use = item.name in selected
        preview.mark_stale()
        return {"FINISHED"}


class BLUEPRINT_UL_fields(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        row = layout.row(align=True)
        row.prop(item, "label", text="", emboss=False)
        sub = row.row(align=True)
        auto = item.label.strip().lower() in core.AUTO_FIELDS and not item.value
        sub.prop(item, "value", text="",
                 placeholder="auto" if auto else "")


class BLUEPRINT_UL_details(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_prop, index):
        row = layout.row(align=True)
        row.prop(item, "name", text="")
        row.prop(item, "objects", text="")


class BLUEPRINT_OT_fields(Operator):
    bl_idname = "blueprint.fields"
    bl_label = "Title Block Fields"
    bl_description = "Add, remove or reset the title block rows"

    action: StringProperty(default="add")

    def execute(self, context):
        s = context.scene.blueprint
        if self.action == "add":
            f = s.fields.add()
            f.label, f.value = "Label", ""
            s.fields_index = len(s.fields) - 1
        elif self.action == "remove" and s.fields:
            s.fields.remove(s.fields_index)
            s.fields_index = max(0, s.fields_index - 1)
        elif self.action == "reset":
            s.fields.clear()
            for label, _v in core.default_fields():
                f = s.fields.add()
                f.label, f.value = label, ""
        preview.mark_stale()
        return {"FINISHED"}


class BLUEPRINT_OT_details(Operator):
    bl_idname = "blueprint.details"
    bl_label = "Detail Callouts"
    bl_description = "Add a detail from the selected objects, or remove one"

    action: StringProperty(default="add")

    def execute(self, context):
        s = context.scene.blueprint
        if self.action == "add":
            picked = [o.name for o in context.selected_objects if o.type == "MESH"]
            if not picked:
                self.report({"ERROR"}, "Select the objects the detail should enlarge")
                return {"CANCELLED"}
            used = {d.name for d in s.details}
            tag = next(c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if c not in used)
            d = s.details.add()
            d.name, d.objects = tag, ",".join(picked)
            d.on = next((v for v in core.VIEW_ORDER if getattr(s, "view_" + v)), "front")
            s.details_index = len(s.details) - 1
            self.report({"INFO"}, "Detail %s: %d object%s"
                        % (tag, len(picked), "" if len(picked) == 1 else "s"))
        elif self.action == "remove" and s.details:
            s.details.remove(s.details_index)
            s.details_index = max(0, s.details_index - 1)
        preview.mark_stale()
        return {"FINISHED"}


# ---------------------------------------------------------------- operators
class BLUEPRINT_OT_import(Operator):
    bl_idname = "blueprint.import_model"
    bl_label = "Import Model"
    bl_description = "Import the chosen model into the current scene"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(context.scene.blueprint.source)

    def execute(self, context):
        s = context.scene.blueprint
        path = bpy.path.abspath(s.source)
        if not os.path.exists(path):
            self.report({"ERROR"}, "File not found: %s" % path)
            return {"CANCELLED"}
        try:
            kind = core.import_into_current(path)
        except Exception as e:
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        s.use_current_scene = True
        preview.mark_stale()
        self.report({"INFO"}, "Imported %s (%s)" % (os.path.basename(path), kind))
        return {"FINISHED"}


class BLUEPRINT_OT_generate(Operator):
    bl_idname = "blueprint.generate"
    bl_label = "Generate"
    bl_description = "Measure the model and write the drawings"
    bl_options = {"REGISTER"}

    def execute(self, context):
        s = context.scene.blueprint
        views = [v for v in core.VIEW_ORDER if getattr(s, "view_" + v)]
        if not views:
            self.report({"ERROR"}, "Select at least one view")
            return {"CANCELLED"}
        if not s.use_current_scene and not s.source:
            self.report({"ERROR"}, "Choose a model file, or tick Use Current Scene")
            return {"CANCELLED"}
        if s.use_current_scene and not core.mesh_objects(context.scene):
            self.report({"ERROR"}, "No mesh geometry in the scene")
            return {"CANCELLED"}

        lines = []
        try:
            report = core.generate(log=lines.append, **_opts(s, views))
        except Exception as e:
            traceback.print_exc()
            self.report({"ERROR"}, str(e))
            s.last_result = "Failed: %s" % e
            return {"CANCELLED"}

        for ln in lines:
            print(ln)
        d = report["scene"]["dimensions"]
        s.last_output = report["output_dir"]
        s.last_result = "%s x %s x %s %s" % (
            render.fmt(d["x"], s.unit), render.fmt(d["y"], s.unit),
            render.fmt(d["z"], s.unit), s.unit)
        self.report({"INFO"}, "%d view%s -> %s"
                    % (len(views), "" if len(views) == 1 else "s", report["output_dir"]))
        return {"FINISHED"}


class BLUEPRINT_OT_preview_refresh(Operator):
    bl_idname = "blueprint.preview_refresh"
    bl_label = "Refresh Preview"
    bl_description = ("Rebuild the previewed view. Not automatic: rebuilding bakes Line "
                      "Art, which is far too heavy to run on every viewport redraw")
    bl_options = {"REGISTER"}

    def execute(self, context):
        s = context.scene.blueprint
        if s.use_current_scene and not core.mesh_objects(context.scene):
            self.report({"ERROR"}, "No mesh geometry in the scene")
            return {"CANCELLED"}
        sheets = {}
        try:
            # write=False: a preview should not litter the output folder
            core.generate(sheets_out=sheets, write=False, log=lambda *a: None,
                          **_opts(s, [s.preview_view]))
        except Exception as e:
            traceback.print_exc()
            self.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        sheet = sheets.get(s.preview_view)
        if sheet is None:
            self.report({"ERROR"}, "Preview produced nothing")
            return {"CANCELLED"}
        preview.set_options(size=s.preview_size, corner=s.preview_corner,
                            opacity=s.preview_opacity)
        preview.set_sheet(sheet, s.preview_view)
        if not preview.is_enabled():
            preview.enable()
        return {"FINISHED"}


class BLUEPRINT_OT_preview_toggle(Operator):
    bl_idname = "blueprint.preview_toggle"
    bl_label = "Show Preview"
    bl_description = "Show or hide the blueprint overlay in the viewport"

    def execute(self, context):
        on = preview.toggle()
        self.report({"INFO"}, "Preview " + ("on" if on else "off"))
        return {"FINISHED"}


class BLUEPRINT_OT_check_updates(Operator):
    bl_idname = "blueprint.check_updates"
    bl_label = "Check for Updates"
    bl_description = ("Ask whether a newer release exists. Installing is still a download "
                      "and Install from Disk: Blender keeps its own copy of an add-on")

    def execute(self, context):
        p = _prefs()
        started = updates.check_async(bl_info["version"],
                                      feed_url=(p.feed_url if p else None),
                                      on_done=preview.redraw)
        if not started:
            self.report({"WARNING"}, updates.state()["message"])
        return {"FINISHED"}


class BLUEPRINT_OT_open_url(Operator):
    bl_idname = "blueprint.open_url"
    bl_label = "Open"
    bl_description = "Open this page in your browser"

    url: StringProperty(default=updates.RELEASES_PAGE)

    def execute(self, context):
        bpy.ops.wm.url_open(url=self.url)
        return {"FINISHED"}


class BLUEPRINT_OT_open_output(Operator):
    bl_idname = "blueprint.open_output"
    bl_label = "Open Output Folder"
    bl_description = "Reveal the output folder in the file browser"

    @classmethod
    def poll(cls, context):
        return bool(context.scene.blueprint.out_dir)

    def execute(self, context):
        path = bpy.path.abspath(context.scene.blueprint.out_dir)
        if not os.path.isdir(path):
            self.report({"WARNING"}, "Not generated yet: %s" % path)
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=path)  # cross-platform, no per-OS shelling out
        return {"FINISHED"}


class BLUEPRINT_OT_set_views(Operator):
    bl_idname = "blueprint.set_views"
    bl_label = "Set Views"
    bl_description = "Select a preset group of views"

    preset: StringProperty(default="standard")

    def execute(self, context):
        s = context.scene.blueprint
        groups = {"standard": ("front", "right", "top", "iso"),
                  "all": tuple(core.VIEW_ORDER), "none": ()}
        chosen = groups.get(self.preset, groups["standard"])
        for v in core.VIEW_ORDER:
            setattr(s, "view_" + v, v in chosen)
        return {"FINISHED"}


# ---------------------------------------------------------------- ui
class BLUEPRINT_PT_main(Panel):
    bl_label = "Blueprint"
    bl_idname = "BLUEPRINT_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blueprint"

    def draw(self, context):
        s = context.scene.blueprint
        layout = self.layout

        box = layout.box()
        box.prop(s, "use_current_scene")
        if not s.use_current_scene:
            box.prop(s, "source")
            row = box.row()
            row.enabled = bool(s.source)
            row.operator(BLUEPRINT_OT_import.bl_idname, icon="IMPORT")
        else:
            n = len(core.mesh_objects(context.scene))
            box.label(text="%d mesh object%s" % (n, "" if n == 1 else "s"),
                      icon="MESH_DATA" if n else "ERROR")

        col = layout.column(align=True)
        col.prop(s, "out_dir")
        col.prop(s, "title")
        # "//" is relative to the .blend; with nothing saved it lands next to whatever
        # directory Blender was launched from, which is rarely what anyone wants
        if s.out_dir.startswith("//") and not bpy.data.filepath:
            layout.label(text="Save the .blend, or set an absolute path", icon="ERROR")

        layout.separator()
        head = layout.row(align=True)
        head.label(text="Views")
        head.operator(BLUEPRINT_OT_set_views.bl_idname, text="Std").preset = "standard"
        head.operator(BLUEPRINT_OT_set_views.bl_idname, text="All").preset = "all"
        head.operator(BLUEPRINT_OT_set_views.bl_idname, text="None").preset = "none"
        grid = layout.grid_flow(row_major=True, columns=2, even_columns=True, align=True)
        for v in core.VIEW_ORDER:
            grid.prop(s, "view_" + v, toggle=True)

        layout.separator()
        col = layout.column(align=True)
        col.prop(s, "unit")
        col.prop(s, "page")
        col.prop(s, "theme")
        col.prop(s, "layout")
        if s.layout == "projection":
            col.prop(s, "angle")
        if s.layout in ("plate", "projection"):
            col.prop(s, "uniform_scale")

        col = layout.column(align=True)
        col.prop(s, "margin", slider=True)
        col.prop(s, "crease")

        row = layout.row(align=True)
        row.prop(s, "hidden", toggle=True)
        row.prop(s, "silhouette", toggle=True)
        row.prop(s, "grid", toggle=True)
        row = layout.row(align=True)
        row.prop(s, "dims", toggle=True)
        row.prop(s, "centre_marks", toggle=True)
        row = layout.row(align=True)
        row.prop(s, "pdf", toggle=True)
        row.prop(s, "png", toggle=True)

        layout.separator()
        n_views = sum(1 for v in core.VIEW_ORDER if getattr(s, "view_" + v))
        big = layout.column()
        big.scale_y = 1.5
        big.enabled = n_views > 0
        big.operator(BLUEPRINT_OT_generate.bl_idname,
                     text=("Generate Sheet (%d views)" % n_views if s.layout != "sheet"
                           else "Generate %d View%s" % (n_views, "" if n_views == 1 else "s")),
                     icon="FILE_IMAGE")

        if s.last_result:
            box = layout.box()
            box.label(text=s.last_result, icon="DRIVER_DISTANCE")
            if s.last_output:
                box.operator(BLUEPRINT_OT_open_output.bl_idname, icon="FILE_FOLDER")

        st = updates.state()
        v = bl_info["version"]
        row = layout.row(align=True)
        row.label(text="v%d.%d.%d" % v)
        row.operator(BLUEPRINT_OT_check_updates.bl_idname, text="", icon="FILE_REFRESH")
        if st["status"] == "newer":
            box = layout.box()
            box.alert = True
            box.label(text="Update: %s" % st["message"], icon="IMPORT")
            box.operator(BLUEPRINT_OT_open_url.bl_idname,
                         text="Download", icon="URL").url = st["url"]
            box.label(text="Then Preferences > Add-ons > Install from Disk")
        elif st["status"] in ("error", "offline"):
            layout.label(text=st["message"], icon="ERROR")
        elif st["status"] in ("ok", "checking"):
            layout.label(text=st["message"],
                         icon="CHECKMARK" if st["status"] == "ok" else "SORTTIME")


class BLUEPRINT_PT_dimensions(Panel):
    bl_label = "Dimensions"
    bl_idname = "BLUEPRINT_PT_dimensions"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blueprint"
    bl_parent_id = "BLUEPRINT_PT_main"

    def draw(self, context):
        s = context.scene.blueprint
        layout = self.layout
        layout.enabled = s.dims

        col = layout.column(align=True)
        col.label(text="Measure from:")
        col.prop(s, "dim_overall", toggle=True)
        col.prop(s, "dim_objects", toggle=True)
        col.prop(s, "dim_edges", toggle=True)
        col.prop(s, "dim_auto", toggle=True)
        col.prop(s, "dim_perimeter", toggle=True)
        col.prop(s, "dim_empties", toggle=True)

        obj = context.active_object
        if s.dim_edges:
            if obj and obj.mode == "EDIT":
                layout.label(text="Reading live edge selection", icon="EDITMODE_HLT")
            else:
                layout.label(text="Uses the stored edge selection", icon="INFO")
        if s.dim_auto or s.dim_edges:
            layout.prop(s, "dim_radius_not_dia")

        layout.separator()
        col = layout.column(align=True)
        col.label(text="Kinds to draw:")
        for k, lbl, _d in KIND_ITEMS:
            col.prop(s, "kind_" + k, toggle=True)

        layout.separator()
        col = layout.column(align=True)
        col.label(text="Spacing:")
        col.prop(s, "dim_offset", slider=True)
        col.prop(s, "dim_step", slider=True)
        col.prop(s, "dim_text", slider=True)


class BLUEPRINT_PT_objects(Panel):
    bl_label = "Objects"
    bl_idname = "BLUEPRINT_PT_objects"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blueprint"
    bl_parent_id = "BLUEPRINT_PT_main"

    def draw(self, context):
        s = context.scene.blueprint
        layout = self.layout
        layout.prop(s, "use_object_filter")

        box = layout.column()
        box.enabled = s.use_object_filter
        row = box.row(align=True)
        row.operator(BLUEPRINT_OT_sync_objects.bl_idname, icon="FILE_REFRESH")
        row = box.row(align=True)
        row.operator(BLUEPRINT_OT_object_ticks.bl_idname, text="All").mode = "all"
        row.operator(BLUEPRINT_OT_object_ticks.bl_idname, text="None").mode = "none"
        row.operator(BLUEPRINT_OT_object_ticks.bl_idname,
                     text="Selected").mode = "selected"

        box.template_list("BLUEPRINT_UL_objects", "", s, "objects", s, "objects_index",
                          rows=6)

        n_scene = len(core.mesh_objects(context.scene))
        n_on = sum(1 for i in s.objects if i.use)
        if not len(s.objects):
            box.label(text="Press Refresh to list the scene", icon="INFO")
        elif len(s.objects) != n_scene:
            box.label(text="Scene has %d meshes - refresh" % n_scene, icon="ERROR")
        else:
            box.label(text="Measuring %d of %d" % (n_on, n_scene), icon="CHECKMARK")


class BLUEPRINT_PT_sheet(Panel):
    bl_label = "Title Block"
    bl_idname = "BLUEPRINT_PT_sheet"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blueprint"
    bl_parent_id = "BLUEPRINT_PT_main"

    def draw(self, context):
        s = context.scene.blueprint
        layout = self.layout
        layout.prop(s, "title")
        if not len(s.fields):
            layout.operator(BLUEPRINT_OT_fields.bl_idname, text="Add Default Rows",
                            icon="PRESET").action = "reset"
            layout.label(text="Using the default rows", icon="INFO")
            return
        row = layout.row()
        row.template_list("BLUEPRINT_UL_fields", "", s, "fields", s, "fields_index", rows=5)
        col = row.column(align=True)
        col.operator(BLUEPRINT_OT_fields.bl_idname, text="", icon="ADD").action = "add"
        col.operator(BLUEPRINT_OT_fields.bl_idname, text="", icon="REMOVE").action = "remove"
        col.separator()
        col.operator(BLUEPRINT_OT_fields.bl_idname, text="", icon="LOOP_BACK").action = "reset"
        layout.label(text="Blank value = filled in from the drawing")


class BLUEPRINT_PT_details(Panel):
    bl_label = "Detail Callouts"
    bl_idname = "BLUEPRINT_PT_details"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blueprint"
    bl_parent_id = "BLUEPRINT_PT_main"

    def draw(self, context):
        s = context.scene.blueprint
        layout = self.layout
        row = layout.row()
        row.template_list("BLUEPRINT_UL_details", "", s, "details", s, "details_index",
                          rows=3)
        col = row.column(align=True)
        col.operator(BLUEPRINT_OT_details.bl_idname, text="", icon="ADD").action = "add"
        col.operator(BLUEPRINT_OT_details.bl_idname, text="", icon="REMOVE").action = "remove"
        if len(s.details):
            d = s.details[min(s.details_index, len(s.details) - 1)]
            layout.prop(d, "on", text="Ring on view")
        layout.label(text="Add rings the selected objects", icon="INFO")


class BLUEPRINT_PT_preview(Panel):
    bl_label = "Preview"
    bl_idname = "BLUEPRINT_PT_preview"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blueprint"
    bl_parent_id = "BLUEPRINT_PT_main"

    def draw(self, context):
        s = context.scene.blueprint
        layout = self.layout
        st = preview.status()

        layout.prop(s, "preview_view", text="")
        layout.operator(BLUEPRINT_OT_preview_refresh.bl_idname, icon="FILE_REFRESH")

        # A labelled button, not a bare icon: the overlay draws over the viewport and the
        # way out has to be obvious.
        on = preview.is_enabled()
        row = layout.row()
        row.scale_y = 1.3
        row.alert = on
        row.operator(BLUEPRINT_OT_preview_toggle.bl_idname,
                     text="Hide Preview" if on else "Show Preview",
                     icon="HIDE_ON" if on else "HIDE_OFF")

        col = layout.column(align=True)
        col.enabled = on
        col.prop(s, "preview_size", slider=True)
        col.prop(s, "preview_opacity", slider=True)
        col.prop(s, "preview_corner", text="")

        if not st["has"]:
            layout.label(text="Nothing previewed yet", icon="INFO")
        elif st["stale"]:
            layout.label(text="Options changed - refresh", icon="ERROR")
        else:
            layout.label(text="Showing %s" % st["view"].upper(), icon="CHECKMARK")
        layout.label(text="Hidden lines show translucent, not dashed")


CLASSES = (BlueprintPrefs, BlueprintField, BlueprintDetail, BlueprintObject,
           BlueprintSettings, BLUEPRINT_UL_objects, BLUEPRINT_UL_fields,
           BLUEPRINT_UL_details, BLUEPRINT_OT_fields, BLUEPRINT_OT_details,
           BLUEPRINT_OT_sync_objects, BLUEPRINT_OT_object_ticks,
           BLUEPRINT_OT_import, BLUEPRINT_OT_generate,
           BLUEPRINT_OT_preview_refresh, BLUEPRINT_OT_preview_toggle,
           BLUEPRINT_OT_open_output, BLUEPRINT_OT_set_views,
           BLUEPRINT_OT_check_updates, BLUEPRINT_OT_open_url,
           BLUEPRINT_PT_main, BLUEPRINT_PT_dimensions, BLUEPRINT_PT_objects,
           BLUEPRINT_PT_sheet, BLUEPRINT_PT_details, BLUEPRINT_PT_preview)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.blueprint = PointerProperty(type=BlueprintSettings)

    p = _prefs()
    if p and p.check_on_start:
        updates.check_async(bl_info["version"], feed_url=p.feed_url,
                            on_done=preview.redraw)


def unregister():
    preview.disable()      # a live draw handler would outlive the add-on and crash
    del bpy.types.Scene.blueprint
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
