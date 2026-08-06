"""
Blueprint Tool — Blender add-on.

3D Viewport > sidebar (N) > Blueprint tab. Measures the scene and writes orthographic
blueprint drawings: hidden-line removed, dimensioned, with a title block.

The work happens in core.py, which the command-line entry point shares, so the add-on
and the CLI cannot drift apart.
"""

bl_info = {
    "name": "Blueprint Tool",
    "author": "Laurent de Laroche Souvestre",
    "version": (1, 0, 0),
    "blender": (4, 5, 0),
    "location": "3D Viewport > Sidebar (N) > Blueprint",
    "description": "Measure a model and generate dimensioned orthographic blueprint drawings",
    "category": "Object",
}

import os
import traceback

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, PointerProperty,
                       StringProperty)
from bpy.types import Operator, Panel, PropertyGroup

from . import core

if "bpy" in locals():  # support Reload Scripts
    import importlib
    core = importlib.reload(core)


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


# ---------------------------------------------------------------- properties
class BlueprintSettings(PropertyGroup):
    use_current_scene: BoolProperty(
        name="Use Current Scene",
        description="Draw whatever is already in the scene. Turn off to import a file first",
        default=True,
    )
    source: StringProperty(
        name="Model",
        description="Model to import and draw",
        subtype="FILE_PATH",
    )
    out_dir: StringProperty(
        name="Output",
        description="Folder to write the drawings and dimensions.json into",
        subtype="DIR_PATH",
        default="//blueprints/",
    )
    title: StringProperty(
        name="Title",
        description="Title block heading. Leave blank to use the file name",
    )

    unit: EnumProperty(name="Unit", items=UNIT_ITEMS, default="mm")
    page: EnumProperty(name="Page", items=PAGE_ITEMS, default="a3l")

    margin: FloatProperty(
        name="Margin",
        description="Fraction of the page kept clear for dimensions and title block",
        default=0.18, min=0.02, max=0.45, subtype="FACTOR",
    )
    # plain degrees rather than subtype ANGLE, which stores radians and would need
    # converting on every read for no gain
    crease: FloatProperty(
        name="Crease",
        description="Edges meeting at a sharper angle than this become lines, in degrees. "
                    "Lower values draw more interior detail",
        default=140.0, min=0.0, max=180.0,
    )
    hidden: BoolProperty(
        name="Hidden",
        description="Draw occluded edges as dashed lines. Roughly doubles generation time",
        default=True,
    )
    dims: BoolProperty(
        name="Dims",
        description="Draw dimension lines and values. The isometric view never gets them",
        default=True,
    )
    png: BoolProperty(
        name="PNG",
        description="Also render a shaded reference image per view",
        default=False,
    )

    last_result: StringProperty(default="")
    last_output: StringProperty(default="")


def _view_prop(name, default):
    return BoolProperty(name=name.capitalize(), default=default)


for _v in core.VIEW_ORDER:
    setattr(BlueprintSettings, "__annotations__", getattr(BlueprintSettings, "__annotations__", {}))
    BlueprintSettings.__annotations__["view_" + _v] = _view_prop(
        _v, _v in ("front", "right", "top", "iso"))


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
        self.report({"INFO"}, "Imported %s (%s)" % (os.path.basename(path), kind))
        return {"FINISHED"}


class BLUEPRINT_OT_generate(Operator):
    bl_idname = "blueprint.generate"
    bl_label = "Generate Blueprints"
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
            report = core.generate(
                use_current_scene=s.use_current_scene,
                source=(None if s.use_current_scene else bpy.path.abspath(s.source)),
                out_dir=bpy.path.abspath(s.out_dir),
                views=views, unit=s.unit, page=s.page,
                margin=s.margin, crease=s.crease,
                hidden=s.hidden, dims=s.dims, png=s.png,
                title=(s.title or None),
                log=lines.append,
            )
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
            core.fmt(d["x"], s.unit), core.fmt(d["y"], s.unit),
            core.fmt(d["z"], s.unit), s.unit)
        self.report({"INFO"}, "%d views -> %s" % (len(views), report["output_dir"]))
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
        groups = {
            "standard": ("front", "right", "top", "iso"),
            "all": tuple(core.VIEW_ORDER),
            "none": (),
        }
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

        col = layout.column(align=True)
        col.prop(s, "margin", slider=True)
        col.prop(s, "crease")

        row = layout.row(align=True)
        row.prop(s, "hidden", toggle=True)
        row.prop(s, "dims", toggle=True)
        row.prop(s, "png", toggle=True)

        layout.separator()
        n_views = sum(1 for v in core.VIEW_ORDER if getattr(s, "view_" + v))
        big = layout.column()
        big.scale_y = 1.5
        big.enabled = n_views > 0
        big.operator(BLUEPRINT_OT_generate.bl_idname,
                     text="Generate %d View%s" % (n_views, "" if n_views == 1 else "s"),
                     icon="FILE_IMAGE")

        if s.last_result:
            box = layout.box()
            box.label(text=s.last_result, icon="DRIVER_DISTANCE")
            if s.last_output:
                box.operator(BLUEPRINT_OT_open_output.bl_idname, icon="FILE_FOLDER")


CLASSES = (BlueprintSettings, BLUEPRINT_OT_import, BLUEPRINT_OT_generate,
           BLUEPRINT_OT_open_output, BLUEPRINT_OT_set_views, BLUEPRINT_PT_main)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.blueprint = PointerProperty(type=BlueprintSettings)


def unregister():
    del bpy.types.Scene.blueprint
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
