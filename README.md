# Bad Company 2 Frostbite Importer for Blender 5.2

A Blender extension for browsing and importing assets from **Battlefield: Bad Company 2** Frostbite 1 disc images. It reads the source media directly and does not ship game files.

## Technical documentation

The reverse-engineering notes for importer and exporter authors live in
[`docs/`](docs/README.md). They cover the PC and Xbox 360 disc layouts, MSI and
cabinet indirection, FbRB archives, supported resource formats, shader-derived
material behavior, validation evidence, round-trip design constraints, and a
source-by-source audit of the external BF3/BF4 MaxScript references.

## Supported source media

- **Xbox 360 ISO / XGD2**: pure-Python XDVDFS reader. A scan indexes the active entries in all Frostbite `.fbrb` archives.
- **PC retail ISO**: ISO9660 reader plus Windows Installer metadata and Microsoft LZX cabinet extraction. The archive list is scanned immediately; individual archives are indexed on demand to avoid unpacking the entire DVD.

PC retail-disc extraction currently requires Windows because the installer database and `expand.exe` are used for the original cabinets. Xbox 360 disc reading is platform-independent.

## Installation

1. Build or download the extension ZIP.
2. In Blender 5.2, open **Edit > Preferences > Get Extensions**.
3. Use **Install from Disk** and select the ZIP.
4. Enable **Bad Company 2 Frostbite Importer** if Blender does not enable it automatically.

For a source checkout, install the repository root as an extension or add `bc2_frostbite_importer` as a legacy add-on during development.

## Workflow

1. Open **File > Import > Bad Company 2 / Frostbite 1**, or use the **Frostbite** tab in the 3D View sidebar.
2. Select a PC or Xbox 360 ISO and click **Scan Disc**.
3. For a PC ISO, filter the archive list and click **Index Selected Archive**. Cabinet files are staged in the configured cache only when needed.
4. To assemble a complete supported PC map, enter its level name (currently
   validated with `MP_001`) in **Complete PC Map** and click **Import Map**.
   The add-on automatically indexes the level, terrain, and shared multiplayer
   archives.
5. For individual assets, filter the indexed asset list by type:
   - Meshes
   - Textures
   - Terrain
   - Animation
   - Levels / DBX
   - Audio
   - UI / Video
   - Other data
6. Select an asset and click **Import Selected**, or use **Extract Raw**.

For a complete PC character, select its high-level soldier `.dbx` rather than
an individual mesh set. The importer follows `Meshes3p`, creates one shared
armature, and places every linked LOD0 body, head, and headgear module in a
separate visible collection. Selecting a vehicle entity `.dbx` follows its
`CompositeMeshAsset`, applies the mesh-set LOD0 bind transforms, and creates
named source-part collections with damaged, destroyed, and blurred
alternatives hidden by default. Geometry whose individual source, object, or
mesh name contains `Wreck` at least twice is also hidden independently of its
part collection. A single occurrence does not hide either an object or its
part collection.
Direct
`SkinnedMeshSet` and `CompositeMeshSet` imports remain focused component
imports. Importing a `GrannyAnimation` adds an Action to the matching armature;
if that armature is absent, the referenced skeleton is created automatically.

The provided Xbox example indexes 630 Frostbite archives and 251,251 active assets. The provided PC example exposes 625 extractable Frostbite archives through the installer database.

## Native Blender import support

| Asset | Current behavior |
|---|---|
| `MeshData` | Creates one Blender mesh object per visible subset, including UVs, materials, and available bone-weight vertex groups. PC little-endian and Xbox 360 big-endian layouts are supported. |
| Rigid and tree mesh sets | Resolves a matching LOD `MeshData` resource when available, preferring LOD0. |
| `CompositeMeshSet` | Resolves LOD0 geometry and source materials, corrects shared/global index streams, separates verified source part IDs into named collections, applies decoded mesh-set bind transforms, and hides decoded damaged/destroyed/blurred parts plus geometry or collections with two `Wreck` occurrences in one name by default. A single `Wreck` occurrence alone remains visible. |
| PC `SkinnedMeshSet` | Resolves LOD0 geometry, the DBX skeleton reference, the default surface set, and source DX11 texture bindings; creates all source bones, four preserved shader-coordinate layers, nondegenerate character UV routing, named vertex groups, one material per visible subset, and Armature modifiers. Console mesh sets remain geometry-only. |
| PC soldier entity `.dbx` | Resolves every `Meshes3p` body/head/headgear component at LOD0, applies per-component shader-set overrides, binds all modules to one verified skeleton, and creates modular component collections. |
| PC vehicle entity `.dbx` | Resolves the linked composite LOD0 mesh, source-backed materials, ordered named part collections, mesh-set bind transforms, and DBX-backed default/damaged/alternate states without inventing runtime pivots, parenting, or an armature. Verified AH60 Main, glass, interior, cockpit-interior, and minigun shader relationships receive dedicated Blender graphs. |
| PC `GrannyModel` | Creates a native Blender armature while preserving the complete source hierarchy, local transforms, source flags, and physics-joint metadata. |
| PC `GrannyAnimation` | Creates a Blender 5.2 layered/slotted Action on the matching armature, sampled at the source time step with root motion and quaternion continuity. |
| `WaterMesh` | Creates native water geometry from every body using platform-endian 32-bit triangle indices. |
| `ITexture` | Converts PC texture resources to DDS and creates an Image plus node material. |
| `XenonTexture` | Untiles Xbox 360 surfaces, fixes compressed-block byte order, converts to DDS, and creates an Image plus node material. DXT1, DXT3, DXT5, ARGB8888, and L8 headers are recognized. Grouped, padded mip allocations import their base level so Blender can generate lower display mips. |
| `TerrainHeightfield` | Creates a Blender terrain mesh with configurable sampling. The 1024-edge default preserves the complete `MP_001` playable heightfield; lower settings are explicit previews. |
| Other binary `.dbx` | Converts the Frostbite binary document to XML in a Blender Text datablock. |
| PC complete map | Resolves direct and compact-group placements into linked Blender objects; imports all level heightfields and water; combines mesh subsets; assigns surface materials through the level DX11 shader database; and creates a JSON diagnostics Text datablock. |
| Other resources | Extracts the original resource to the cache without pretending that a native import succeeded. |

The axis option converts Frostbite Y-up coordinates to Blender Z-up coordinates. DirectX V coordinates can be flipped during mesh import.

Blender's bundled NumPy is used when available for bulk mesh, topology, UV,
terrain, and Xbox texture operations. The importer retains pure-Python
fallbacks. Converted textures remain file-backed DDS images loaded through
Blender's native image subsystem instead of expanding pixels in Python.

### Modular character and vehicle organization

High-level assembly imports create this collection structure:

```text
BC2_<Assembly>
  Rig                         # characters only
  Components
    00_<Module>
      mesh subsets            # character component
      Parts                   # composite vehicle component
        000_<SourcePart>
        001_<SourcePart>
```

All character modules are visible by default so body, head, and headgear can
be mixed and matched at the collection level. `RandomGroup` is retained as
component metadata rather than used to hide optional modules. Vehicle parts
are split only where retail part IDs and triangle membership agree; ordered
`lod0|PartName` records provide names, local bounds, and authoritative bind
transforms. Those transforms place movable parts in the assembled vehicle.
Nested DBX component health states and low/high-RPM model fields identify the
default model; lower-health, destroyed, and blurred alternatives remain
available in collections but are hidden in the viewport and render by
default. Runtime pivots, parenting, state switching, and animation hierarchy
are not synthesized.

### Complete-map organization

The PC map importer creates a `BC2_<LEVEL>` collection with Direct, Forest,
Middlegrowth, Rigid Group, Static Group, Terrain, Water, and Diagnostics child
collections. Repeated placements share mesh datablocks. `MP_001` resolves to
5,380 placed static objects backed by 178 unique MeshData resources and 211
geometry/material variants.

Mesh materials use exact database keys and retain every recovered binding as
metadata. DXBC `RDEF` reflection preserves the pixel-shader parameter name for
each texture slot. The importer uses those names together with source texture
families to construct primary-color, layered detail/OcRGB, occlusion, cutout,
and normal graphs. The shader-validated `Nm_General_001` detail normal uses the
game's alpha/green DXT5nm decode, 8x UV repeat, and
`base + 0.4 * detail` combine. Graphs that still approximate unimplemented
shader arithmetic are labeled in material custom properties.

The common retail PC stride-48 static declaration retains object, repeating,
selector, and atlas coordinate pairs at byte offsets 28, 36, 32, and 24 as
`UVMap`, `UV1`, `UV2`, and `UV3`. Layered static shaders sample object
`OcRGB` through `UVMap` and repeating detail textures through `UV1`.
Placement matrices are reconstructed from the source column-major transform
without the former X/Z reflection, so signs and other readable texture
content are no longer mirrored. Sparse shader-set overrides are merged over
mesh defaults; this restores the complete default SmallFactory surface set
while retaining its intended metal substitutions. The distinct retail PC
stride-48 composite declaration used by AH60 retains four shader-coordinate
pairs at offsets 24, 28, 32, and 40.

The `MP_001` physical-material map is decoded to a file-backed L8 image and
drives source grass, dirt, tarmac, gravel, stone, seabed, and default-ground
textures across all terrain tiles. This is a source-backed approximation;
terrain decals and exact shader blend constants remain future work.

The validated `MP_001` scene reopens with 5,380 placements, 211 linked mesh
datablocks, 13 terrain tiles, one water mesh, 418 exact shader-key matches, 417
connected Base Color graphs, and 185 available file-backed images.
Terrain and water remain in the heightfield's already-world-space frame; a
ground-contact check measures a -0.018-unit median forest-origin offset. All 485
visible subset slots resolve by source naming. The diagnostics Text preserves
the ten distinct nonvisual-reference descriptions from 24 raw placements.

The validated PC US Assault assembly resolves 23 visible subsets across
`US_01_Body`, `US_01_Head`, and `US_01_Headgear`, binds all three modules to
one 147-bone `ske01` armature, resolves all 23 materials, and loads 56 unique
file-backed source images. The body retains its verified 10,513 vertices,
9,883 triangles, and 13 exact material keys. Its stride-64 vertex declaration
maps source UVs at byte offsets 40, 16, 44, and 48 to Blender `UVMap`, `UV1`,
`UV2`, and `UV3`. Representative retail DX11 permutations establish
red/green primary normals,
alpha/green repeated cloth-detail normals, 15x dirt sampling, camouflage and
cloth modulation, source dirt masks, and baked occlusion. The Blender graph
reproduces those high-level relationships while labeling the remaining
lighting/specular and per-surface blend differences as approximations.

The byte-16 field is preserved as `UV1`, but retail `US_01` stores collinear
`u == v` values there for `LegStandard`, MOLLE, and pouches. Treating it as a
2D Blender UV caused the reported zigzag texture corruption. Those surfaces
now route their layered detail, camouflage, and dirt-mask bindings through
the nondegenerate byte-44 `UV2` channel while retaining the original field
for diagnostics.

The validated AH60 vehicle assembly creates 36 ordered source-part
collections and 66 surface fragments totaling 27,388 triangles. Its 34
healthy/static part collections are visible by default; only the two blurred
high-RPM rotor alternatives are hidden at collection level. Single-`Wreck`
part names such as `NoseWreck` and `WreckPartTail` remain visible. The 11
geometry fragments whose source subset contains `Wreck` twice are also hidden,
including fragments split into otherwise healthy part collections. Fragments
with one `Wreck` occurrence remain visible inside single-`Wreck` part
collections. Known
doors, rotors, wheels, and wings use their decoded retail bind transforms.
The import resolves 11 source materials and 14 file-backed images using a
matching retail level DX11 database. The reported
`01_lod0_WreckPartTail_Main.001` piece is verified as authored LOD0 with 948
vertices and 722 triangles, compared with 434/251 at LOD1 and 56/34 at LOD2.
Retail shader disassembly routes AH60 Main color through byte-28 `UV1`,
symbols through byte-32 `UV2`, dirt through byte-40 `UV3`, detail through
byte-24 `UVMap`, and the generic mask through `UV1` repeated 15 times.
Dedicated graphs also drive transmissive dirty glass, the dark
interior/cockpit diffuse scales, and the minigun's symbol color, repeated
generic mask, and green-channel specular control. No vehicle rig, runtime
pivot, or parent hierarchy is fabricated.

## Cache behavior

The cache location is configurable in the add-on preferences. It contains:

- the persistent SQLite asset catalog;
- the extracted PC installer database;
- PC cabinets staged on demand;
- extracted `.fbrb` archives;
- decompressed map-archive payloads used for direct asset seeking;
- raw resources and converted DDS textures.

Cabinets can be large. They may be deleted from the cache when Blender is closed; the add-on recreates them from the ISO when needed.
Complete-map payload caches are also substantial: the three verified
`MP_001` payloads total about 730 MB. They are reusable, source-fingerprint
scoped derivatives and may likewise be deleted when Blender is closed.

## Current limitations

- Native Granny conversion is currently limited to validated PC little-endian
  v7 skeletons and transform animations. Xbox 360/PS3 Granny variants, vector
  tracks, nonidentity animated scale/shear, and curve codecs outside the
  documented set are rejected explicitly.
- PC modular character materials are validated with the complete US Assault
  body/head/headgear set. Shared character bindings use `MP_001`'s duplicated
  DX11 database; standalone AH60 bindings fall back to the matching `MP_004`
  database. AH60 Main/glass/interior/minigun source relationships are
  verified, but exact Frostbite environment lighting and every alternate
  vehicle kit still require additional validation.
- Composite part membership, ordered LOD0 records, bounds, and mesh-set bind
  transforms are imported. Initial visibility uses DBX health/model states
  plus explicit wreck/damaged/blurred names. Runtime pivots, parent hierarchy,
  state switching, rigs, and animation remain unverified and are not
  synthesized.
- Complete-map assembly is currently validated only for the PC retail
  `MP_001` graph. Xbox whole-map assembly is not yet implemented.
- Representative map color, alpha, normal, OcRGB, and detail bindings are
  reflected and connected, but general shader constants, exact UV sources,
  swizzles, blend arithmetic, and every permutation are not decoded.
  Unresolved bindings and subset matches are retained in diagnostics rather
  than silently guessed.
- Console texture formats outside the listed formats are rejected explicitly.
- Terrain physical-material indices and source detail layers are imported.
  Terrain full/group/spline decal records are detected and reported, but the
  compiled `VisualTerrain`/`MaskTileAtlas` mapping, exact repeat constants, and
  blend arithmetic are not decoded. Water geometry is complete; its Blender
  material remains an explicit approximation.
- Effects, game logic, collision, destruction states, audio, lights, weather,
  and animation are not reconstructed by the static map importer.
- Deleted Frostbite archive entries are intentionally omitted from the asset browser.
- Writing modified resources, rebuilding `.fbrb` archives, cabinets, or disc images is not implemented.

## Export roadmap

Export is intentionally secondary to reliable import. Future work can add:

1. Blender mesh-to-`MeshData` writing with strict vertex-layout preservation.
2. DDS-to-`ITexture` / `XenonTexture` encoding.
3. DBX round-tripping.
4. Lossless `.fbrb` repacking.
5. Optional mod-folder output compatible with existing BC2 mod loaders.

Original disc images should remain read-only. Any future write support should target a separate mod workspace.

## References and credits

Format behavior was studied and validated against:

- [AnirohDev/FBOneTools](https://github.com/AnirohDev/FBOneTools)
- [Powback/Bad-Company-2-Map-Editor](https://github.com/powback/Bad-Company-2-Map-Editor)
- Local Frostbite research tools supplied for this project

The extension is a Python 3 implementation designed for Blender and does not bundle code executables or copyrighted game assets from those projects.

Battlefield, Frostbite, Electronic Arts, and DICE are trademarks of their respective owners. Use the importer only with game media and assets you are legally permitted to access.

## Development and validation

Build installable extension packages into `dist\`:

```powershell
.\scripts\build_extension.ps1
```

If Blender is not on `PATH`, provide its executable explicitly:

```powershell
.\scripts\build_extension.ps1 `
  -BlenderPath "D:\Blender Foundation\Blender 5.2\blender.exe"
```

The script reads the package name and version from
`blender_manifest.toml`. Existing versions remain in `dist\`; the directory is
excluded from extension package contents and generated ZIP files are not
committed.

Run the dependency-free core tests from the repository root:

```powershell
python -m unittest discover -s tests -v
```

Run the Blender-facing smoke test with Blender 5.2:

```powershell
blender --background --factory-startup --python tests\blender_smoke.py
```

An optional reproducible performance benchmark is also available:

```powershell
blender --background --factory-startup --python tests\benchmark_blender_import.py
```

The tests cover PC/Xbox mesh byte order, shared composite index streams,
source part-ID inference, lossless splitting, little-/big-endian composite
bind records, modular DBX resolution and vehicle state classification,
accelerated and fallback Xbox texture paths, DDS generation, terrain sampling,
DBX conversion, an end-to-end
synthetic XDVDFS -> FbRB -> MeshData path, bounded Granny skeleton/animation
fixtures, the four-channel PC character shader declaration, degenerate
coordinate routing regressions, character binding roles and packed-normal
graphs, and Blender-native mesh, armature, Action, material, and image
creation.

For optional validation with legally owned PC media:

```powershell
blender --background --factory-startup `
  --python tests\blender_map_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND> --level MP_001
```

This saves and reopens the map blend, then verifies placement, linked-mesh,
terrain, and diagnostics counts. Images remain native file-backed DDS resources
unless `--pack-images` is supplied.

The equivalent PC character integration check is:

```powershell
blender --background --factory-startup `
  --python tests\blender_character_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

It imports the US Assault body, head, and headgear on `ske01`, then adds
`StandIdle` and `RunForward`; checks bind invariance, sampled pose matrices,
four source coordinate channels, corrected LegStandard/MOLLE/pouches UV
routing, all 23 component materials, source-backed DDS images, Base Color and
packed-normal graphs; then saves, reopens, and verifies the modular metadata,
armature, skinned subsets, materials, modifiers, and Actions.

The equivalent AH60 vehicle integration check is:

```powershell
blender --background --factory-startup `
  --python tests\blender_vehicle_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

It verifies 36 named source-part collections, 27,388 total triangles, the
34-visible/2-hidden part state, all 11 double-`Wreck` fragments hidden,
single-`Wreck` collections and their healthy fragments visible, known
door/rotor/wheel bind
translations, authored WingLeft LOD0 topology, its four declared coordinate
layers, verified Main/glass/interior/minigun graphs, source-backed materials
and images, assembly metadata, and save/reopen persistence without introducing
an unverified vehicle armature.
