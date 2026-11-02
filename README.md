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
- **PC retail ISO**: ISO9660 reader plus Windows Installer metadata and Microsoft LZX cabinet extraction. A background scan catalogs archive names and discovers map folders without unpacking the entire DVD; selected archives are indexed on demand.

PC retail-disc extraction currently requires Windows because the installer database and `expand.exe` are used for the original cabinets. Xbox 360 disc reading is platform-independent.

## Installation

1. Build or download the extension ZIP.
2. In Blender 5.2, open **Edit > Preferences > Get Extensions**.
3. Use **Install from Disk** and select the ZIP.
4. Enable **Bad Company 2 Frostbite Importer** if Blender does not enable it automatically.

For a source checkout, install the repository root as an extension or add `bc2_frostbite_importer` as a legacy add-on during development.

## Workflow

1. Open **File > Import > Bad Company 2 / Frostbite 1**, or use the **Frostbite** tab in the 3D View sidebar.
2. Choose a PC or Xbox 360 ISO. The add-on immediately scans it in a background
   worker. Blender remains interactive, progress appears in the status area,
   and **Cancel After Current Archive** stops at the next safe archive
   boundary. Xbox assets are indexed during that scan.
3. The default PC scan reads installer metadata only. It populates the archive
   list and an editable map picker from
   folders containing both `level-00.fbrb` and `terrain-00.fbrb`; it does not
   extract every cabinet member or build a bundled game-data manifest.
   **Index All (Slow)** remains an explicit action in the archive panel.
4. To browse objects associated with a PC map, choose a discovered level and
   click **Index Map Objects**. Only that level's `level-00` archive and the
   shared `mp_common` archive are indexed in the background. To assemble the
   complete map instead, click **Import Map**; the importer also indexes the
   terrain archive and resolves required materials and textures. Complete-map
   behavior is currently validated with `MP_001`.
5. For any other PC bundle, select it in **PC Frostbite Archives** and click
   **Index Selected Archive**. This operation also runs in the background.
6. For individual assets, combine the **Content group** filter
   (Characters, Vehicles, Weapons, Buildings, Props, Vegetation, Levels &
   Terrain, Effects, UI & Movies, Audio, or Other) with the resource-type
   filter:
   - Meshes
   - Textures
   - Terrain
   - Animation
   - Levels / DBX
   - Audio
   - UI / Video
   - Other data
   The word-search field remains active alongside both filters, so a search
   such as `Raven` within **Characters** and **Meshes** narrows the complete
   result set before pagination.
   **Collapse identical paths** is enabled by default and shows one row when
   the same virtual resource path is packaged in several archives.
   **Hide LOD variants** is also enabled by default and hides internal
   `*_lodN_data.res` mesh payloads; disable either option and refresh the list
   when the individual archive copies or LOD resources are needed.
   Rows use a compact type icon so more of the asset name remains visible.
   Enable **Sizes** beside the search field only when file sizes are needed.
7. Select an asset and click **Import Selected**, or use **Extract Raw**.

The map picker is derived locally from archive names and remains editable. It
does not prove that every discovered level is supported: the validated PC
retail sample exposes 33 level/terrain pairs, while complete assembly is
currently verified only for `MP_001`. The manual **Scan Disc** action reapplies
the lightweight catalog scan while preserving a valid source catalog and its
previously indexed assets instead of resetting them.

### Preferences and session settings

Add-on Preferences store machine-wide configuration:

- the last selected PC or Xbox 360 ISO;
- the asset cache directory and an **Open Cache Folder** action;
- default axis conversion, UV flip, scale, Z-only, terrain-resolution,
  material, composite-visibility, and related-animation options;
- whether detailed JSON diagnostics Text datablocks are created.

The ISO path is no longer stored in new `.blend` scene settings. The
**Frostbite** sidebar keeps filters, selections, map input, status, and current
import-option overrides only for the Blender session. Use **Reset to Preference
Defaults** in **Import Settings** to discard those overrides. The File > Import
asset browser displays the same settings instead of bypassing them.

Detailed diagnostics Text datablocks default to off. Importers still collect
diagnostics and report warning/error counts in Blender and in the Frostbite
status display. Enable the preference when a persistent JSON issue report is
needed.

Material mode controls dependency work:

| Mode | Behavior |
|---|---|
| **Materials and Textures** | Resolves source surfaces, shader bindings, and texture images. This preserves the previous default behavior. |
| **Materials Only** | Creates source-named materials and metadata without loading shader databases for texture bindings or converting images. |
| **Simple Placeholders** | Skips source material lookup and creates neutral subset/terrain materials. |

Directly importing a selected texture still creates an Image in every mode.
**Composite Visibility** can preserve the verified default-state hiding rules
or show every imported part while retaining the original state metadata. It
does not implement runtime destruction-state selection.

For a complete PC character, select its high-level soldier `.dbx` rather than
an individual mesh set. The importer follows `Meshes3p`, creates one shared
armature, and places every linked LOD0 body, head, and headgear module in a
separate visible collection. Selecting a vehicle entity `.dbx` follows its
`CompositeMeshAsset`, applies the mesh-set LOD0 bind transforms, and creates
named source-part collections with damaged, destroyed, and blurred
alternatives hidden by default. Geometry whose individual source, object, or
mesh name contains `Wreck` at least twice is also hidden independently of its
part collection. A single occurrence does not hide either an object or its
part collection. The **Show All Imported Parts** option leaves these variants
visible without changing their decoded state metadata. On verified PC tracked
vehicles, the same high-level import also follows each nested
`TrackComponentData`: the shared skinned track mesh is instantiated once per
source component, bound to that component's referenced track skeleton, and
placed with its exact DBX transform. Importing the track mesh set directly
remains a focused single-component import and therefore does not reconstruct
the left/right vehicle instances.
**Import related animations** defaults to enabled for direct PC
`SkinnedMeshSet` imports and modular character `.dbx` imports. The importer
searches the mesh's and referenced skeleton's exact parent folders. For a PC
soldier mesh, it also reverse-matches bounded `Objects/Soldiers` descriptors
through `Meshes3p`, then follows the character-specific static, facial,
kit-joint, and revive animation-tree links and their explicit clip lists. For
human soldiers, it also follows the explicit `Shared` link only as far as the
rifle base-set tree and selects eight verified locomotion clips: `StandIdle`,
`StandRelaxedIdle`, `CrouchIdle`, forward/backward walk and run, and
`SneakForward`. Other weapon, AI-behavior, death, and cutscene branches remain
excluded. Resource paths are deduplicated, and every tree and clip must
reference the exact skeleton. This imports Raven's six verified clips and 14
bounded Actions for each validated Beach Scout/Japanese infantry mesh without
loading the hundreds of clips that can share `ske01`.

The locomotion Actions preserve their resource timing rather than applying an
invented preview slowdown: the verified walk cycles are 29 inclusive frames
over about 0.9333 seconds at 30 Hz, while the run cycles are 19 inclusive
frames over about 0.6 seconds. The game animation graph may phase or scale
these clips from runtime movement parameters; that state-machine behavior is
not reconstructed. Quadratic curve boundaries use clamped Granny control
windows, so the inclusive loop endpoint does not skip the final control or
distort feet and body orientation.

The four facial motion resources are source-additive deltas, not complete
rest-pose Actions. The character facial tree's replacement array identifies
its authored `FacePose`; the importer bakes `FacePose @ delta` into each
standalone 46-track Blender Action. Selecting a dead-pose or blink Action
therefore retains the character-specific neutral face instead of distorting it.
The `bc2_additive` metadata records the source encoding, while
`bc2_additive_baked` and `bc2_baked_base_asset_path` record the standalone
bake.

Only indexed assets are searchable; Blender reports a warning when unindexed
archives mean discovery may be incomplete. Direct soldier reverse lookup is
limited to 256 descriptors and caches the selected compressed archive payload
once before bounded random reads. **Index All (Slow)** provides full catalog
coverage, while the session checkbox or its Add-on Preference default can
disable automatic Action import. A second default-on checkbox controls the
eight core gameplay Actions independently. A combined 64-clip safety limit
prevents unexpectedly broad discovery from locking Blender.

Direct `CompositeMeshSet` imports remain focused component imports. Importing
a selected absolute `GrannyAnimation` adds one Action to the matching
armature; if that armature is absent, the referenced skeleton is created
automatically. Direct import of a sparse source-additive clip fails explicitly
when its character tree is unavailable because applying it over the generic
rest pose would be incorrect.

The provided Xbox example indexes 630 Frostbite archives and 251,251 active assets. The provided PC example exposes 625 extractable Frostbite archives through the installer database.

## Native Blender import support

| Asset | Current behavior |
|---|---|
| `MeshData` | Creates one Blender mesh object per visible subset, including UVs, materials, and available bone-weight vertex groups. PC little-endian and Xbox 360 big-endian layouts are supported. |
| Rigid and tree mesh sets | Resolves a matching LOD `MeshData` resource when available, preferring LOD0. |
| `CompositeMeshSet` | Resolves LOD0 geometry and source materials, corrects shared/global index streams, separates verified source part IDs into named collections, applies decoded mesh-set bind transforms, and hides decoded damaged/destroyed/blurred parts plus geometry or collections with two `Wreck` occurrences in one name by default. A single `Wreck` occurrence alone remains visible. |
| PC `SkinnedMeshSet` | Resolves LOD0 geometry, the DBX skeleton reference, the default surface set, and source DX11 texture bindings; preserves declaration-specific UV layers (two for verified stride-48 assets and four for stride-64 characters), creates named vertex groups and source materials, adds Armature modifiers, and imports bounded exact-folder plus character-tree Actions when enabled. Console mesh sets remain geometry-only. |
| PC soldier entity `.dbx` | Resolves every `Meshes3p` body/head/headgear component at LOD0, applies per-component shader-set overrides, binds all modules to one verified skeleton, creates modular component collections, follows character-specific static/facial/kit/revive animation links, and optionally selects eight verified rifle locomotion clips through the explicit shared base-set link. |
| PC vehicle entity `.dbx` | Resolves the linked composite LOD0 mesh, source-backed materials, ordered named part collections, mesh-set bind transforms, and DBX-backed default/damaged/alternate states without inventing runtime pivots or parenting. Nested `TrackComponentData` instances are preserved separately: each resolves its shared skinned mesh and explicit skeleton, receives a source-backed local armature, and uses the exact component transform. Matching shader databases are selected by source/duplicate-path/material-key provenance and cached; compatible shader variants route texture bindings to physical UV offsets. Verified AH60 and MI28 families retain dedicated arithmetic while other vehicle glass uses a semantic transmissive fallback. Unknown composite UV declarations remain an explicitly marked cross-subset topology fallback rather than a vehicle-name exception. |
| PC `GrannyModel` | Creates a native Blender armature while preserving the complete source hierarchy, local transforms, source flags, and physics-joint metadata. |
| PC `GrannyAnimation` | Creates a Blender 5.2 layered/slotted Action on the matching armature, sampled at the source time step with root motion, quaternion continuity, and representable bone scale. Degrees 0-3 and the documented human/Raven codecs are implemented. |
| `WaterMesh` | Creates native water geometry from every body using platform-endian 32-bit triangle indices. |
| `ITexture` | Converts PC texture resources to DDS and creates an Image plus node material. |
| `XenonTexture` | Untiles Xbox 360 surfaces, fixes compressed-block byte order, converts to DDS, and creates an Image plus node material. DXT1, DXT3, DXT5, ARGB8888, and L8 headers are recognized. Grouped, padded mip allocations import their base level so Blender can generate lower display mips. |
| `TerrainHeightfield` | Creates a Blender terrain mesh with configurable sampling. The 1024-edge default preserves the complete `MP_001` playable heightfield; lower settings are explicit previews. |
| Other binary `.dbx` | Converts the Frostbite binary document to XML in a Blender Text datablock. |
| PC complete map | Resolves direct and compact-group placements into linked Blender objects; imports all level heightfields and water; combines mesh subsets; and applies the selected material mode. A JSON diagnostics Text datablock and diagnostic empty are created only when detailed diagnostics are enabled. |
| Other resources | Extracts the original resource to the cache without pretending that a native import succeeded. |

The axis option converts Frostbite Y-up coordinates to Blender Z-up
coordinates. DirectX V coordinates can be flipped during mesh import.

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
Tracked vehicles can add sibling `TRACK` component collections, each with a
local `Rig` collection. These are distinct source instances even when both
refer to the same mesh and skeleton; the importer does not infer mirroring or
hard-code vehicle offsets.
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
pairs at offsets 24, 28, 32, and 40. The MI28 uses another stride-48
composite declaration: its repeated part selector is at byte 32 and its four
shader-coordinate pairs are at 24, 28, 36, and 40.

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

The validated MI28 vehicle assembly creates 24 source-part collections and 44
surface fragments totaling 21,032 imported triangles. Its LOD0 MeshData uses
the byte-32 composite part selector and exposes byte 24/28/36/40 as
`UVMap`/`UV1`/`UV2`/`UV3`. Retail `MP_005` shader disassembly routes the main
normal/detail through `UVMap`, color and the 10x generic mask through `UV1`,
Russian symbols through `UV2`, and dirt through `UV3`. Main and MainMetal
receive their recovered color graphs. The textureless
`06_lod0_Windshields_Glass` surface receives a transmissive view-angle glass
graph rather than an opaque fallback.

The validated Zero Fighter import creates 24 objects totaling 8,459 triangles.
Its stride-64 LOD0 declaration uses a byte-48 composite part selector and
byte-36 primary `UVMap`; the previous byte-24 fallback was degenerate. Outer
and interior glass now receive shared transmissive vehicle-glass graphs, with
the sky texture retained as an environment binding instead of becoming Base
Color. A retail survey parses all 62 unique vehicle LOD0 MeshData resources
without failure and exercises recurring byte-32, byte-36, byte-40, byte-44,
and byte-48 selector families. Unknown declarations remain topology-driven
preview heuristics, not exporter-safe format specifications.

## Cache behavior

The cache location is configurable in Add-on Preferences, where **Open Cache
Folder** opens it without deleting or rebuilding any data. It contains:

- the persistent SQLite asset catalog;
- the extracted PC installer database;
- PC cabinets staged on demand;
- extracted `.fbrb` archives;
- decompressed map-archive payloads used for direct asset seeking;
- raw resources and converted DDS textures.

Cabinets and extracted archives can be large. **Index All (Slow)** may
stage substantial data while all PC archives are inspected. Archive indexing
runs in a background worker so Blender UI events continue to be processed.
Cancellation takes effect between archives; an active `expand.exe` cabinet
extraction is allowed to finish before the worker stops.
Cached cabinets may be deleted when Blender is closed; the add-on recreates
them from the ISO when needed.
Complete-map payload caches are also substantial: the three verified
`MP_001` payloads total about 730 MB. They are reusable, source-fingerprint
scoped derivatives and may likewise be deleted when Blender is closed.

## Current limitations

- Native Granny conversion is currently limited to validated PC little-endian
  v7 skeletons and transform animations. Xbox 360/PS3 Granny variants, vector
  tracks, time-varying or nondecomposable scale/shear, and curve codecs outside
  the documented set are rejected explicitly. Degree-0 `DaConstant32f`
  scale/shear is supported when Blender location/rotation/scale channels can
  represent it.
- PC modular character materials are validated with the complete US Assault
  body/head/headgear set. Shared character bindings use `MP_001`'s duplicated
  DX11 database; standalone AH60 bindings fall back to the matching `MP_004`
  database. AH60 and MI28 graph relationships plus Zero Fighter glass are
  verified. Other vehicle glass uses a semantic approximation, and exact
  Frostbite environment lighting, generic per-binding UV routes, and every
  alternate vehicle kit still require additional validation.
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
  and animation are not reconstructed by the static map importer. It does not
  select destructible-building health-state meshes; standalone composite
  imports do not interpret the building's part graph. See the
  [PC building destruction research](docs/formats/levels-and-maps.md#research-pc-building-destruction-assemblies).
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
bind records, modular DBX resolution, repeated skinned-track component
transforms, and vehicle state classification,
accelerated and fallback Xbox texture paths, DDS generation, terrain sampling,
DBX conversion, an end-to-end
synthetic XDVDFS -> FbRB -> MeshData path, bounded Granny skeleton/animation
fixtures including a degree-3 `D3I1K32fC32f` curve, bounded mesh/skeleton-folder
related-clip discovery, the four-channel PC character shader declaration, degenerate
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

The Raven mesh/animation integration check is:

```powershell
blender --background --factory-startup `
  --python tests\blender_raven_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

It targets only `sp_common` and `sp_003_B`, imports the stride-48 Raven with
its 17-bone armature, discovers six unique exact-skeleton clips, verifies the
authored 0.8 `exportAnimation` scale, and checks that all six Actions survive
save/reopen.

The shared-human-skeleton graph rule is checked with the Scout integration:

```powershell
blender --background --factory-startup `
  --python tests\blender_scout_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

It imports the 11-subset Scout on the 147-bone `ske01` rig. The mesh-folder
`FacePose` and `Us_Scout_SkeletonPose` become 46- and 147-track Actions; the
authored facial tree adds `DeltaBlink`, `DeltaEyeIdle`, and two dead-pose
deltas with 4, 6, 21, and 14 source tracks, baked over the 46-track
`FacePose`. The explicit shared rifle tree adds eight 39-track core locomotion
Actions. All 14 survive save/reopen.

The Japanese infantry graph and additive conversion check is:

```powershell
blender --background --factory-startup `
  --python tests\blender_jap_infantry_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

It imports eight Japanese infantry mesh subsets on the 147-bone `ske01` rig,
resolves the regular soldier entity's facial and kit-joint trees, verifies the
same six character-specific Action classes plus eight core rifle locomotion
Actions, and checks save/reopen persistence. It also compares
`dead_pose2_Delta` against the Japanese `FacePose` baseline: maximum, p99, and
mean mesh displacement must remain below 0.012, 0.005, and 0.0004 Blender
units, with explicit eye/lid vertex coverage. `WalkBackward` additionally
verifies 29 frames at 30 Hz, a final-to-first right-toe rotation mismatch below
`1e-4` radians, and a hips loop-matrix error below `2e-4`.

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

The BMD3 track-component integration check is:

```powershell
blender --background --factory-startup `
  --python tests\blender_vehicle_track_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

It verifies the repeated-track source pattern on BMD3, M1A2, and T90, then
imports BMD3 as one composite body plus two independently transformed track
instances. Each track retains 1,138 vertices, 1,096 triangles, its own
source-backed eight-bone rig, the shared mesh/skeleton references, distinct
component GUID, exact lateral placement, and save/reopen persistence.

The equivalent MI28 UV and glass integration check is:

```powershell
blender --background --factory-startup `
  --python tests\blender_mi28_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

It verifies 24 source parts, 44 surface fragments, 21,032 imported triangles,
the byte-32 part selector and 24/28/36/40 coordinate layout across all ten
LOD0 subsets, shader-verified Main/MainMetal texture routing, the 36-vertex
transmissive windshield, and save/reopen persistence.

The broader vehicle declaration/material check is:

```powershell
blender --background --factory-startup `
  --python tests\blender_vehicle_family_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

It imports and reopens Zero Fighter, Corsair, and UAV. The runner verifies
Zero Fighter's 24 objects, 8,459 triangles, byte-48 selector, byte-36 primary
UV, and semantic glass; Corsair `_CM` color, cube environment, byte-24
color/normal, byte-28 decal, and `(128, 64)` normal repeat; and UAV byte-28
color/detail with `(8, 8)` detail repeat. It also checks the recurring AH60,
AH64, MI28, M1A2, and T90 declaration families directly from retail
resources.

The structural all-vehicle audit is:

```powershell
blender --background --factory-startup `
  --python tests\blender_vehicle_catalog_audit.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_JSON>
```

It enumerates all 62 unique indexed PC vehicle LOD0 resources, parses their
geometry, resolves subset surfaces and source material keys, records database
provenance, layout/variant evidence, texture roles, physical UV routes,
fallbacks, and explicit unsupported cases. It fails on parse errors,
unexpected resolver failures, unresolved subset assignment without a source
explanation, or shader routes to absent layers. Material partitions absent
from every indexed retail database and source descriptors that omit shader
sets/references are reported separately as source limitations.
