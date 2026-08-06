# blueprint-tool

3D model in → dimensions + orthographic blueprint drawings out. Headless Blender, no GUI,
no extra dependencies beyond a Blender install.

```bash
./bp model.fbx --out ./out --views front,right,top,iso --unit mm --page a3l --hidden
```

Accepts `.fbx .blend .obj .glb .gltf .stl .ply .usd*`.

## Output

Per run, into `--out`:

| File | What |
|---|---|
| `dimensions.json` | overall + per-object dimensions, units, and the exact world→pixel mapping per view |
| `<view>.svg` | print-scaled vector drawing: hidden-line-removed linework, dimensions, title block |
| `<view>.png` | optional shaded reference render (`--png`) |

Views: `front back left right top bottom iso`. The `iso` view is pictorial — it gets no
dimension annotations.

See [`examples/`](examples/) for real output: a dimensioned side elevation, an isometric,
and the `dimensions.json` they came with.

## Options

| Flag | Default | Notes |
|---|---|---|
| `--unit` | `m` | `m cm mm in ft` |
| `--page` | `a4l` | `a4l a4p a3l a3p a2l` — sets both physical page size and pixel resolution (300 dpi) |
| `--margin` | `0.18` | fraction of the frame kept clear for dimensions and title block |
| `--crease` | `140` | crease angle threshold in degrees; lower = more interior lines |
| `--hidden` | off | draw occluded edges as dashed lines |
| `--no-dims` | off | omit dimension annotations |
| `--png` | off | also render a shaded PNG per view |
| `--title` | filename | title block heading |

## How it works

1. Import, then strip cameras/lights so only geometry remains.
2. Measure world axis-aligned bounding boxes, overall and per object.
3. Per view, build an orthographic camera framed on the bbox. `sensor_fit=HORIZONTAL`
   fixes `ortho_scale` to mean "world units across the image width" whatever the aspect.
4. A Grease Pencil **Line Art** modifier produces the linework. At occlusion level 0 this
   is true hidden-line removal, not just edge extraction.
5. Read the baked strokes, project each point with `world_to_camera_view`, and write the
   SVG here rather than using Blender's exporter (see below).

Because the projection is ours, `dimensions.json.views.<view>` carries everything needed
to place further annotations without re-deriving anything:

```
x_px = world_to_camera_view(scene, cam, P).x * res_x
y_px = (1 - world_to_camera_view(scene, cam, P).y) * res_y
```

## Blender 5.0 notes (found the hard way)

- **`wm.grease_pencil_export_svg` is unusable in 5.0.** It emits degenerate coordinates —
  stroke points collapse to a single value around `1e25`, with a `0 0 0 0` viewBox, on
  every combination of bake/live and clip-camera. Hence the local SVG writer.
- **`lineart_bake_strokes` bakes the whole scene frame range.** A 250-frame source file
  bakes 250 times per view. Pin `frame_start = frame_end = frame_current = 1` first —
  drops a bake from ~2.5 s to ~0.04 s.
- **A bake leaves an empty keyframe at frame 0** next to the real one, so
  `layer.frames[0]` is the wrong frame and yields zero strokes. Select by
  `frame_number == scene.frame_current`.
- **`cam.matrix_world` is stale until the depsgraph runs.** The `iso` view derives its
  axes from that matrix; reading it straight after setting `rotation_euler` gives the
  identity matrix and an empty drawing. Call `bpy.context.view_layer.update()` first.
- **Workbench `light='FLAT'` + white `single_color` on a white world renders white on
  white** — a 100% blank PNG that looks like a failed render. Uses `STUDIO` + grey.
- GPv3 stores strokes as flat point attributes plus `curve_offsets`, not `stroke.points`.
- Modifier property names: `use_material` (not `use_material_boundary`), `radius` (not
  `thickness`).

## Accuracy

FBX unit handling is correct: the same model exported to FBX with `apply_unit_scale` both
on and off imports back to the same size, matching the source `.blend` exactly
(`633.9 × 1348.3 × 640.6 mm`). Blender's 4.5+ C++ FBX importer applies the file's
`UnitScaleFactor` — three.js `FBXLoader` does not, which is the usual source of
100× dimension errors in browser-based tools.

Caveats, also emitted into `dimensions.json`:

- Dimensions are **axis-aligned** bounding boxes. A part rotated off-axis reports a larger
  box than its true size — use that object's `local_dimensions` instead.
- Line Art follows the mesh, so a tessellated curve reads as facets.
- The stated scale assumes the SVG is printed at its declared physical page size.
