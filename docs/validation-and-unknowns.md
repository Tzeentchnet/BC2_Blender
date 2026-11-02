# Validation and unknowns

## Audit status

This document was audited on 2026-11-02 against the current implementation,
the complete test inventory, and the named integration-runner assertions.

- `python -m compileall -q bc2_frostbite_importer` completed successfully.
- All 66 dependency-free tests in `tests/test_core.py` passed.
- The Blender smoke test and optional retail-media integrations were reviewed
  but not rerun during this documentation audit. Their results below remain
  recorded evidence from the named Blender 5.2/5.2.2 runs, not results inferred
  from the unit suite.

Synthetic regression coverage, Blender-hosted validation, recorded retail-media
observations, implemented preview policy, heuristics, and unknowns are kept
separate below. Passing a parser test does not upgrade a preview heuristic into
an exporter-safe format claim.

## Real-media observations

The implementation has been exercised read-only against supplied PC and Xbox
360 retail images. The measurements in this section are recorded outputs from
those bounded runs; the dependency-free unit suite does not recreate them.

### Xbox catalog

| Category | Active assets |
|---|---:|
| Animation | 11,749 |
| Audio | 117,739 |
| Data | 35,019 |
| Level / DBX | 69,688 |
| Mesh | 9,635 |
| Other | 2,134 |
| Terrain | 473 |
| Texture | 4,704 |
| UI / Video | 110 |
| **Total** | **251,251** |

The disc contained 630 FbRB archives and 384,241 total metadata records when
deleted entries were included.

### PC catalog

The tested MSI exposed 625 unique extractable FbRB archives. Archive indexing
was validated on demand, including the MedKit mesh and shader-related assets.
The lightweight archive scan found 33 folders containing both
`level-00.fbrb` and `terrain-00.fbrb`; these populate the map picker without
extracting FbRB files. This is an implemented archive-name heuristic. A
separate resolver/material audit now completes all 33 on the tested media;
strict Blender construction, save/reopen, and visual-foundation checks remain
verified only for `MP_001`.

### PC `MP_001` complete-map foundation

The level resolver was run against the retail PC media with the level,
terrain, and `mp_common` archives indexed:

| Measurement | Result |
|---|---:|
| Direct visual placements | 2,461 |
| Compact-group placements | 2,919 |
| Total static visual placements | 5,380 |
| Unique MeshData resources | 178 |
| Mesh/shader-set variants | 211 |
| Terrain heightfields | 13 |
| Water meshes | 1 |
| Expected nonvisual references | 24 |
| Expected unsupported terrain-decal diagnostic | 1 |
| Unexpected unresolved geometry | 0 |

Every one of the 178 LOD0 MeshData resources parsed. The unique geometry totals
378,856 decoded vertices and 247,147 triangles. All group spawn counts,
transform counts, asset counts, and serialized enumeration order agree.

The level DX11 database parser recovered 8,222 unique material keys from a
26,905,158-byte resource with no trailing bytes.

The final Blender 5.2 integration scene was saved and reopened with:

| Measurement | Result |
|---|---:|
| Linked placement objects | 5,380 |
| Linked mesh datablocks | 211 |
| Resolved shader database keys | 418 exact matches |
| Connected Base Color graphs | 417 |
| Native file-backed images available after reopen | 185 |
| Matched visible subset slots | 485 of 485 |
| Explicit unresolved subset slots | 0 |
| Missing shader database keys | 0 |
| Terrain world footprint | `-1024..1024` on Blender X and Y |
| Terrain world elevation | approximately `0..482.43` on Blender Z |
| Central heightfield vertices | 1,048,576 |
| Median forest-origin terrain offset | approximately `-1.09` Blender units |
| Water world elevation | approximately `130.5..133.25` on Blender Z |
| Water topology | 569 of 569 vertices referenced by 1,013 triangles |

The diagnostics Text has ten deduplicated nonvisual-reference descriptions
covering the 24 raw nonvisual placements, one explicit unsupported-terrain-
visual-decal description, 176 dynamic-transform descriptions, and three
routes to a physical vertex offset that cannot be exposed because it is a
verified part-selector field. There were no missing-shader, missing-texture,
texture-conversion, or unresolved-subset diagnostics.

### PC all-map structural/material audit

The Blender-hosted audit was run against all 33 discovered PC map pairs using
the same level, terrain, `sp_common` where applicable, and `mp_common`
preparation policy as the importer:

| Measurement | Result |
|---|---:|
| Completed maps / structural errors | 33 / 0 |
| Serialized placements | 192,022 |
| Mesh/material variants | 6,425 |
| Unique MeshData paths | 1,194 |
| Assigned subsets / source surfaces | 14,367 / 19,308 |
| Exact shader-key matches | 14,367 |
| Texture bindings / missing assets | 60,664 / 0 |
| Shader route-analysis errors | 0 |
| Heightfield paths / WaterMesh paths | 321 / 33 |
| Terrain classes / source-textured classes | 223 / 184 |

All 14,367 assigned subsets used suffix surface matching and exact shader
keys. Binding roles were 4,947 color, 2,933 decal, 20,681 detail, 323
environment, 5,458 mask, 1,346 noise, 12,341 normal, 11,842 occlusion, six
specular, and 787 unknown.

Route evidence was 27,717 physical, 25,597 physical with dynamic transforms,
7,005 semantic fallbacks, and 345 routes to vertex offsets not exposed by
the mesh decoder. These last two classes are retained limitations, not audit
failures. The resolver also reported 791 expected nonvisual references, 33
unsupported terrain-visual-decal diagnostics, and two missing shader
references for the same unused MP_005 industry-skyline surface in the base
and SR graphs.

The terrain prefix is derived from each resolved material-map path, which
correctly covers shared mode-variant partitions. Of the 39 classes without a
source texture, 16 are SeaBed, seven Dirt, five Tarmac, four Sand, four Water,
and three Concrete. Those classes use explicit color fallbacks; they are not
silently assigned an unrelated default texture.

## Verified resource samples

- PC and Xbox MedKit MeshData: 3,378 vertices and 3,130 triangles each, with
  matching decoded geometry and UVs.
- Xbox tiled L8 texture: untiled and decoded through DDS.
- Xbox DXT5 texture: untiled, 16-bit block-swapped, and decoded.
- PC L8 ITexture: converted and decoded through DDS.
- Xbox grouped/padded mip resources: base surface recovered without reading
  nonexistent separate mip payloads.
- Xbox 2048 x 2048 TerrainHeightfield: bounds, samples, and preview mesh parsed.
- PC little-endian and Xbox big-endian TerrainHeightfield headers; both tested
  platforms use little-endian samples beginning at byte `0x31`.
- Binary DBX: typed immutable tree plus well-formed XML inspection view.
- WaterMesh: platform-endian body records and 32-bit indices; Xbox `MP_001`
  consumes all 18,996 bytes and references all 569 vertices. A public PC reader
  independently reports exact consumption with the same layout across all 36
  terrain bundles.
- MedKit material: texture bindings and DXT5nm decode checked against compiled
  DX11 shaders.
- `MP_001`: direct and grouped placement graph, shared meshes, terrain, water,
  and shader database resolved from retail archives.
- PC building destruction research: 37 building or subcomponent DBX
  documents surveyed in the indexed `MP_001` and `mp_common` archives.
  `IND_Warehouse` matched 77 named health-state meshes to 77 visual composite
  parts and all 224 part-link endpoints to component names. A local Blender
  5.2.2 prototype was reopened and checked for two complete threshold views,
  individual named and empty state toggles, and a real map placement. Runtime
  collapse, effect timing, Xbox equivalence, and export remain unverified.
- `MP_001` map materials: pixel-shader `RDEF` names retained for each binding;
  representative background-tree, bark, foliage, and layered-prop
  permutations checked with FXC disassembly.
- PC all-map material catalog: all 33 discovered level/terrain pairs completed
  with 14,367 exact subset shader keys, 60,664 indexed texture bindings, 223
  terrain classes, and no structural, missing-texture, or route-analysis
  errors. The report preserves route limitations rather than treating them as
  exact shader recovery.
- PC direct rigid/tree material survey: 1,295 unique `RigidMeshSet` and 57
  unique `TreeMeshSet` paths with all 623 non-voice retail archives indexed;
  the two unindexed archives were `streaming_vo_ru-00.fbrb` and `vo-00.fbrb`.
  All 1,175 available rigid descriptors and all 57 tree descriptors parsed.
  Their 2,653 source surfaces produced 2,422 exact material-key matches, 121
  unique complete-partition matches, and 110 explicit misses across 35
  indexed shader databases. The 120 descriptorless rigid resources were
  undergrowth.
- PC direct prop Blender integration: BarrackInterior, Barrack Objects,
  Barrack Tables, MedKit, Barn Door, BarbWire, and Acacia created 20 assigned
  source materials and 30 per-sample file-backed image references, then
  survived save/reopen. BarrackInterior resolved six of six subset materials
  and eight images from `SP_02`; three bindings retain explicit unavailable
  physical-route diagnostics. Acacia resolved three assigned materials from
  five descriptor surfaces and retains five dynamic-transform diagnostics.
- Multi-subset map meshes: one position representation selected across related
  subsets, including half-float building blocks and the float-position bridge.
- Common stride-48 static declaration: object/repeating/selector/atlas pairs
  at bytes 28/36/32/24, verified on 347 MP_001 subsets including Ship System,
  Harbour, PileBricks, GasSilo, and IND_Silo_Medium.
- Layered static materials: OcRGB uses byte-28 `UVMap`, repeating details use
  byte-36 `UV1`, and the undecoded decal atlas remains classified separately
  instead of becoming a striped primary-color texture.
- Terrain alignment: forest origins ray-tested against imported heightfields;
  the source terrain entity translation is retained but not double-applied.
- `MP_001.Heightfield-000`: full 1024 x 1024 byte-`0x31` little-endian source
  mesh, `109.30..276.49` m local height range, game quad diagonal, plus
  targeted GasSilo, IND_Silo_Medium, and IND_SmallFactory contacts within one
  meter.
- `MP_001` TerrainMaterialMap: 4096 x 4096 indexed map, 128 tiles, seven named
  material classes, and file-backed L8 Blender sampling.
- Xbox `MP_001` terrain decals: 9 full, 2 group, and 20 spline records; 22
  level splines split into 20 Road, one River, and one Lake plane; full-decal
  source mask bounds and densities preserved in DBX; runtime
  `VisualTerrain`/format-code-3 atlas mapping still unknown.
- Placement transforms: source column-major reconstruction, positive target
  determinants, and no former X/Z reflection.
- `IND_SmallFactory`: sparse metal override merged over the complete default
  shader set with no unresolved visible subsets.
- PC `US_01_Body`: 13 visible skinned subsets, 10,513 vertices, 9,883
  triangles, and 61 used bones mapped into the complete 147-bone `ske01`
  hierarchy.
- PC `US_01_Body` materials: 13/13 exact default shader keys, 34 unique
  file-backed DDS images, four UV layers from verified offsets 40/16/44/48,
  corrected byte-44 routing for the collapsed LegStandard/MOLLE/pouches
  coordinate, and connected Base Color plus source-normal graphs where bound.
- PC Raven and Parrot LOD0: type-1 palette-backed stride-48 vertices expose
  `UVMap`/`UV1` at byte offsets 40/44. Raven's imported
  `00_RavenLeftWingShape_Raven_Body` uses those two layers and its primary UV
  is nondegenerate on all 1,130 triangles.
- PC Raven animation: six unique clips were deduplicated from ten indexed
  resource rows and verified against the exact 17-bone `skel_Raven` reference.
  Every clip has 17 matching tracks, uses degree-3 rotation/translation
  curves, and samples at 60 Hz to finite transforms. `CorpseEating` and
  `Idle04` preserve the authored uniform 0.8 `exportAnimation` scale. A Blender
  5.2.2 scene retained all six Actions after save/reopen.
- PC Beach US Scout animation: the mesh resolves the shared 147-bone `ske01`
  skeleton, while its two same-skeleton clips live beside the mesh rather than
  the skeleton. Exact-folder discovery imports `FacePose` with 46 tracks and
  `Us_Scout_SkeletonPose` with 147 tracks. The explicitly linked Scout facial
  tree adds 4-track `DeltaBlink`, 6-track `DeltaEyeIdle`, and 21/14-track
  dead-pose sources, each baked over the Scout `FacePose` into a standalone
  46-track Action. The explicit shared rifle tree adds eight 39-track core
  locomotion Actions. A Blender 5.2.2 scene retained all 14 Actions and all 11
  skinned subsets after save/reopen.
- PC Beach Japanese infantry animation: four regular soldier descriptors
  explicitly connect `Jap_Infantry_Mesh` to its 147-track static pose, a
  kit-joint tree, and `JapInfantryFacialAnimTree`. That tree lists the local
  46-track `FacePose` and the same four facial deltas used by Scout. The
  nonzero animation-wrapper tables contain 4, 6, 21, and 14 `ske01` bone
  indices whose names exactly match their transform tracks. Direct rest-pose
  application produced visibly invalid eye/face deformation. Baked
  `FacePose @ delta` conversion reduced final `dead_pose2_Delta`
  FacePose-relative maximum/p99/mean vertex displacement to about
  0.00905/0.00344/0.000160 Blender units; the maximum explicitly occurs among
  eye/lid-weighted vertices and remains below the 0.012 regression bound.
  `DeltaBlink` retains a maximum sampled pose-channel delta of about 0.1575.
  The explicit shared rifle tree adds the same eight 39-track core locomotion
  Actions used by Scout. A Blender 5.2.2 scene retained all 14 Actions, the
  147-bone armature, and all eight mesh subsets after save/reopen.
- PC shared human gameplay graph: both verified default/squad AI soldier roots
  expand through 11 trees to about 644 unique clips, including 202 clips in
  the rifle tree. Automatic import follows only the explicit shared
  `BaseSetAnimTrees` link and selects eight named rifle locomotion clips. This
  is an implemented bounded preview policy; it is not evidence that the other
  runtime branches are understood or exporter-safe. The selected walk cycles
  are authored as 29 inclusive samples over about 0.9333 seconds at 30 Hz and
  the run cycles as 19 samples over about 0.6 seconds. No fixed playback-speed
  field was observed in the rifle tree; runtime movement-driven phase/rate
  behavior remains unknown.
- PC `WalkBackward` quadratic endpoint: retail source-step and Blender 5.2.2
  validation showed that clamping the degree-2 interval index skipped the last
  control at the curve boundary. Clamping relative knot/control indices
  instead reduced the `RightToeBase` frame-29/frame-1 angle mismatch from
  about 0.833 radians to zero, reduced its frame-28/frame-29 change from about
  0.468 to 0.234 radians, and produced a hips loop-basis error of about
  `8.68e-5`. These values validate the importer correction; they do not define
  a general exporter-safe loop policy.
- PC Japanese `combat_pose`: the 46-track, one-frame pose is referenced by the
  `SP_01` level/cutscene graphs, including Soldier Ambush and CS_02 trees, but
  not by the regular soldier entity's facial/kit graph. It is intentionally
  excluded from automatic normal-character discovery rather than inferred
  from its folder name.
- PC US Assault assembly: 13 body, three head, and seven headgear visible
  subsets; all 23 materials resolved; 56 unique file-backed DDS images; every
  module bound to the same 147-bone `ske01` armature.
- PC AH60 assembly: 36 ordered source-part collections, 66 surface fragments,
  27,388 total triangles, 11 resolved source materials, and 14 file-backed
  images. Thirty-four healthy/static collections are visible by default; only
  two blurred high-RPM rotor collections are hidden at collection level. The
  four single-`Wreck` collections remain visible.
  All 11 geometry fragments containing `Wreck` twice in their source name are
  hidden even when part splitting places them in a healthy collection;
  fragments with one occurrence remain visible inside single-`Wreck`
  collections. Decoded
  mesh-set translations place the doors, rotors, wheels, wings, and other
  local parts in the retail bind pose. The reported WingLeft
  `lod0_WreckPartTail_Main` piece retains 948 vertices and 722 triangles from
  LOD0; the authored LOD1 and LOD2 equivalents contain 434/251 and 56/34
  vertices/triangles. Its stride-48 composite declaration retains offsets
  24/28/32/40 as `UVMap`/`UV1`/`UV2`/`UV3`. Main color uses the nondegenerate
  byte-28 channel, with symbols, dirt, detail, and the 15x generic mask routed
  through their shader-verified channels. Glass uses transmissive
  Fresnel/dirt-mask opacity, Interior and CockpitInterior use their verified
  dark diffuse scales, and Minigun Main uses `Symbols_Us` for color while
  `MiniGun_C` green controls specular. Save/reopen retains every transform,
  visibility state, and material graph and introduces no fabricated armature.
- PC tracked-vehicle components: BMD3, M1A2, and T90 each contain two
  `TrackComponentData` instances that share one skinned mesh/skeleton pair and
  carry distinct transforms. Their immediate chassis parent transforms are
  identity. BMD3 places `Track Right` at source translation
  `(-1.567699, 0.002113, -0.009618)` and `Track Left` at
  `(1.567699, 0.002113, -0.009618)`. M1A2 uses asymmetric X/Y values
  `(-1.628634, -0.050659)` and `(1.613156, -0.063159)`; T90 uses X values
  `-1.629941` and `1.612939`. This independently rules out a generic mirror or
  fixed vehicle offset. The BMD3 Blender regression imports one composite body
  plus two track instances. Each track retains 1,138 vertices, 1,096
  triangles, a separate source-backed eight-bone armature, distinct component
  GUID, exact transform metadata, and placement after save/reopen. The same
  regression starts from the directly selected PC retail `T90_Mesh.res`,
  validates its deterministic `T90.dbx` primary-mesh link, and imports both
  1,282-vertex/990-triangle track instances. Their Blender translations are
  `(-1.629941, 0.025497, 1.023512)` and
  `(1.612939, 0.025497, 1.023512)` after axis conversion and remain exact
  after save/reopen. `T90Tracks_Mesh.res` is separately verified not to
  promote to the main assembly.
- PC T90 import performance: Blender 5.2.2 LTS with bundled NumPy 2.3.4
  confirms that the direct full-material path creates 63 mesh objects, two
  track armatures, seven materials, and 15 file-backed images. The previous
  profiled path spent 632.25 of 633.88 seconds scanning 35 complete shader
  databases. Selective material-key/partition parsing reduced the same
  profiled import to 3.66 seconds with a populated shader-key catalog and
  29.66 seconds with only those catalog tables cleared. Unprofiled wall times
  were 1.75 and 19.38 seconds respectively. The 62-resource vehicle shader
  audit still reports zero parser failures after this change.
- PC MI28 assembly: 24 source parts, 44 surface fragments, and 21,032 imported
  triangles. All ten LOD0 subsets retain the byte-32 part-selector declaration
  and coordinate offsets 24/28/36/40. Main and MainMetal use their verified
  normal, color, symbol, dirt, detail, and 10x mask routes. The 36-vertex,
  20-triangle `lod0_Windshields_Glass` surface uses transmissive view-angle
  opacity and retains its graph after save/reopen.
- PC vehicle declaration and shader survey: all 62 unique cataloged vehicle
  LOD0 MeshData resources parse: 21 type-0, nine type-1, and 32 type-2
  resources, covering 240 subsets/surfaces. Every standard shader layout
  encountered has an implemented field width, including the 8-byte
  two-component code `258`, and no derived shader route points to an absent
  retained physical coordinate. All 35 indexed retail `Dx11_Single.res`
  databases were checked. Fifteen referenced material keys/complete shader
  partitions do not occur in those databases; M3A3Tracks has no local surface
  shader set, and Truck `Wheels_LOD` has no source shader reference. These are
  recorded as source limitations, not replaced with unrelated same-name
  materials. The absent partitions are AH64 `asd_main`/`tailrotorblurred`,
  Russian helicopter HUD glass/frame, AA-BMD3 tracks, Cav
  tyres/kits/mirrors, Cobra metal, Humvee tyres, M3A3 tracks, T90 kits,
  Vodnik camonet, LCRS wood, and Ka-52 sensors. The representative family
  regression confirms AH60
  24/28/32/40 with selector byte 36, MI28 24/28/36/40 with selector byte 32,
  AH64 primary byte 28 with selector byte 44, M1A2 primary byte 28 with
  selector byte 40, and Zero Fighter/T90 primary byte 36 with selector byte
  48. These last four families exercise the topology-driven composite
  declaration resolver rather than path-specific exceptions.
  The final audit reports zero implementation failures, 18 source-limit
  records, eight opaque constant/control-style surfaces without a
  texture-driven color graph, and 76 binding records without one provable
  physical route. The latter include alternate-pass duplicates whose same
  texture has another routed binding, semantic glass masks, animated
  rotor/track variants, and HUD/control shaders. They remain explicitly
  labeled fallbacks; they are not exporter-safe declarations.
- PC Zero Fighter direct mesh-set import: four LOD0 subsets split into 24
  objects and 8,459 triangles with no material diagnostics. Every subset,
  including the one-part blurred rotor, retains the 36/40/44 coordinate
  declaration; the three multi-part subsets retain the byte-48 selector.
  MainBody, outer glass, and inner glass use nondegenerate byte-36 `UVMap`
  data. Outer glass retains the sky binding as `ENVIRONMENT`, uses
  transmission with Fresnel/mask opacity, and inner glass retains the
  shader-observed `0.1` opacity scale. Geometry, UV metadata, roles, and both
  glass graphs survive save/reopen.
- PC Corsair direct mesh-set import: 24 objects and 7,198 triangles survive
  save/reopen. Its source-specific SP_01 material maps `_CM` to Base Color,
  retains the reflected cube binding as `ENVIRONMENT`, routes color and
  normals through byte 24, decals through byte 28, and applies the
  shader-derived `(128, 64)` repeat to the general normal.
- PC UAV direct mesh-set import: eight objects and 3,107 triangles survive
  save/reopen. Its compatible shader variant routes both `UAV_D` and
  `Dt_Dirt_002` through physical byte 28 (`UV1` for this declaration) and
  preserves the detail texture's `(8, 8)` repeat. Validation accepts any
  provenance-ranked exact database copy rather than requiring one hard-coded
  level.
- PC `ske01`: local transforms composed against every transposed stored
  inverse-world matrix with a maximum identity residual below `7.5e-7`.
- PC rifle third-person `StandIdle` and `RunForward`: 39 named transform
  tracks each, all matching `ske01`; source durations of approximately 3.0 and
  0.6 seconds; 91 and 19 inclusive samples at 30 Hz.
- Saved/reopened animated character scene: bind-pose deformation below
  `2.5e-7` Blender units and maximum sampled source-to-Blender pose-matrix
  error below `3.8e-6`.

A separate manual Blender 5.2.2 LTS check packed the images used by the earlier
individual-asset examples, removed their extraction caches, and reopened the
scenes successfully. The current integration runners verify save/reopen with
file-backed images; they do not repeat that cache-removal check. The
complete-map workflow intentionally keeps its much larger DDS set file-backed
for performance unless `--pack-images` is requested.

## Automated regression coverage

Run:

```powershell
python -m compileall -q bc2_frostbite_importer
python -m unittest discover -s tests -v
```

The 2026-11-02 audit ran both commands successfully; `unittest` reported 66
passing tests.

Current tests cover:

- little- and big-endian MeshData, including mesh-wide half/float selection;
- one-influence palette-index handling for rigid skinned subsets;
- composite/static palettes remain isolated from the skinned index and UV
  declarations;
- type-2 shared/global subset indices, aligned repeated part-ID inference,
  ordered LOD0 part-record extraction, little-/big-endian bounds and bind
  transforms, and compact lossless per-part splitting;
- character `Meshes3p` and vehicle composite-mesh DBX assembly resolution,
  including health-state and low-/high-RPM model classification;
- explicit character animation-tree clip lists, separation of
  character-specific and shared trees, and the bounded shared rifle base-set
  selection;
- deterministic same-name vehicle-entity promotion and preservation of
  repeated `TrackComponentData` instances with distinct transforms;
- object-level default hiding for geometry with at least two `Wreck`
  occurrences in one name while preserving single-`Wreck` geometry and
  collections;
- the declared four-channel little-endian stride-48 composite layout when the
  verified part-ID field is at byte 36;
- the declared four-channel little-endian stride-48 MI28 composite layout when
  the verified part-ID field is at byte 32, including propagation to
  one-part subsets and preservation of finite declared UV values above the
  heuristic magnitude limit;
- category-gated type-1 vehicle byte-32/36 candidates and the type-0
  stride-48 byte-40 candidate without changing ordinary skinned/static
  declarations;
- stride-keyed shader-layout UV candidates appended to existing map mesh
  declarations while excluding verified composite part-selector fields;
- topology-ranked little-endian stride-64 composite coordinates when bytes
  24/28 are degenerate, including exclusion of a byte-48 part selector and
  declaration propagation to a one-part subset;
- verified AH60 Main/glass/interior/cockpit/minigun texture roles, UV channels,
  shader constants, and Blender graph construction;
- verified MI28 Main/MainMetal coordinate routes and color constants plus its
  textureless transmissive glass graph;
- semantic vehicle outer/interior glass graphs, including environment-role
  preservation, transmission, linked opacity, window/windshield recognition,
  and the interior scale;
- synthetic block-5 shader layouts, RDEF dimensions, upper-bit binding slots,
  and bounded DXBC physical-coordinate provenance through packed components,
  swizzles, affine arithmetic, source modifiers, conditional zero branches,
  and conservative nonlinear operations;
- source-fingerprinted shader material-key indexing, foreign-key invalidation,
  exact/cheap/unique-full-partition matching, and declaration-aware material
  cache separation;
- selective shader parsing and exclusion of already indexed nonmatching
  databases;
- the declared four-channel little-endian stride-64 skinned coordinate layout
  including constant declared channels, plus nondegenerate routing for
  LegStandard, MOLLE, and pouches;
- bounded PC Granny wrapper, section, relocation, skeleton, additive bone-index,
  constant-curve, quadratic-boundary, and degree-3 `D3I1K32fC32f` fixtures;
- PC L8-to-DDS conversion;
- generated L8 DDS output for decoded material-index images;
- Xbox tiled L8 conversion;
- NumPy and pure-Python untile parity for 1-, 4-, 8-, and 16-byte pitches;
- chunked NumPy untile and 16-bit block-swap parity;
- grouped Xbox mip allocation;
- PC/Xbox TerrainHeightfield header byte order, sample byte order, and
  downsampling;
- TerrainMaterialMap tile RLE, nibble order, names, and bounds validation;
- a minimal synthetic binary DBX partition/container and its typed-tree/XML
  parity; semantic DBX tests mostly construct the typed tree directly, so this
  is not exhaustive binary value-type coverage;
- Frostbite placement translation and basis conversion;
- case-insensitive virtual-path deduplication for heightfields indexed from
  more than one archive copy;
- complete PC level-pair discovery and targeted map-object archive selection
  without terrain;
- compact-group spawn/Havok enumeration validation;
- synthetic DX11 shader material, texture binding, and `RDEF` resource-name
  parsing;
- exact case-insensitive catalog lookup and path-based reads;
- combined content-group, resource-type, and text filtering, duplicate-path
  collapsing, and LOD-variant hiding before pagination;
- full and targeted archive indexing, per-archive failure recording, and
  cancellation between archives;
- one-time materialization and direct reuse of compressed FbRB payloads;
- synthetic XDVDFS -> FbRB -> MeshData end-to-end import.

See [`tests/test_core.py`](../tests/test_core.py).

Blender-facing validation runs separately:

```powershell
blender --background --factory-startup --python tests\blender_smoke.py
```

It checks bulk topology transfer, coordinate conversion, modern UV attribute
assignment, smooth flags, named and numbered vertex weights, native armature
construction, Armature modifiers, Blender 5.2 layered/slotted Actions, terrain
creation, shared terrain-partition shader lookup, evidence-backed terrain
layer families, explicit unknown-layer fallback, the pure-Python mesh
fallback, native file-backed DDS loading,
combined subset material indices and secondary UV layers (including Blender's
degenerate-face cleanup), linked map instances, placement matrices, map
texture roles, layered OcRGB/detail construction, and the verified DXT5nm node
graph. It also checks character texture-role selection, primary red/green
normal decode, alpha/green cloth-detail decode, and the combined character
normal graph. Composite smoke coverage verifies `Components/Parts` collection
creation, source part names/metadata, compact triangle geometry, verified
default-state hiding, and the visibility-only Show All override. It also
checks WindowManager session registration, preference-default copying, full,
materials-only, and placeholder material policies, and diagnostics Text output
enabled and disabled. It also checks compact asset-type icon classification
and that the optional file-size column defaults to hidden. Scan-policy coverage
verifies that a new source is
cataloged while an already-valid catalog is reused without deleting its
indexed assets. It also verifies that the default PC policy does not index
unrelated archives, that map-object preparation targets only the selected
level and `mp_common`, that explicit full indexing remains available, and that
worker results repopulate Blender's map, archive, and asset collections. Core
coverage verifies cancellation between archives and per-archive failure
recording for both full and targeted indexing.

Direct mesh-set dispatch coverage verifies that PC rigid/tree resources with a
same-name descriptor use the shared source-material path, direct composites
retain their modular path, descriptorless PC rigid resources retain neutral
geometry with a warning, console rigid resources remain geometry-only with a
PC-support warning, and direct `MeshData` does not guess an owner.

Related-animation smoke coverage verifies exact-folder discovery, explicit
animation-tree clip lists, additive-base composition metadata, deduplication,
the separate shared-gameplay option, and warnings when unindexed archives make
discovery incomplete.

An optional real-media integration runner performs a complete import, saves the
blend, reopens it, and checks placement count, linked mesh count, exact child
collection counts, terrain contact and water bounds, plausible placement
extents, Base Color graph coverage, file-backed image availability, surface
match methods, shader-key aliases, and diagnostics persistence:

```powershell
blender --background --factory-startup `
  --python tests\blender_map_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND> --level MP_001
```

The retail-wide material audit produces a detailed local JSON report and
fails on structural map errors, unresolved assigned materials, non-exact
shader keys, missing indexed textures, shader-route analysis errors,
unclassified bindings, or a map with no source-backed terrain class:

```powershell
blender --background --factory-startup `
  --python tests\blender_map_material_catalog_audit.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_JSON>
```

Known source limitations and route classifications remain in the report.
Generated reports can exceed 70 MB and are local derivatives; they are not
committed.

The PC character integration runner imports and binds the full US Assault
body/head/headgear assembly, resolves all 23 visible materials, loads native
DDS images, adds `StandIdle` and `RunForward`, validates sampled pose matrices,
and verifies the saved/reopened scene:

```powershell
blender --background --factory-startup `
  --python tests\blender_character_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

The Raven runner targets `sp_common` and `sp_003_B`, imports the 17-bone
Raven, verifies its two retained UV layers, checks all six exact-skeleton
Actions and authored scale metadata, and reopens the saved scene:

```powershell
blender --background --factory-startup `
  --python tests\blender_raven_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

The Scout runner verifies the 147-bone `ske01` armature, all 11 skinned
subsets, two exact-folder Actions, four facial deltas baked over the authored
`FacePose`, eight explicitly selected shared rifle Actions, and persistence of
all 14 Actions:

```powershell
blender --background --factory-startup `
  --python tests\blender_scout_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

The Japanese infantry runner verifies the equivalent 14-Action explicit graph,
eight skinned subsets, additive facial composition against the Japanese
`FacePose`, eye/lid displacement bounds, the corrected `WalkBackward`
quadratic endpoint, and save/reopen persistence:

```powershell
blender --background --factory-startup `
  --python tests\blender_jap_infantry_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

The PC direct prop-material runner imports and reopens seven rigid/tree mesh
sets across cutscene, building, weapon, wire, and vegetation families. It
checks source-surface assignment, shader-database provenance, file-backed
images, diagnostics, and persistence:

```powershell
blender --background --factory-startup `
  --python tests\blender_prop_material_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

The corresponding catalog audit fills any unscanned DX11 key inventories
without tracing unrequested variants, resolves every indexed PC rigid/tree
descriptor, and writes the source paths and explicit limitations to JSON:

```powershell
python tests\prop_material_catalog_audit.py `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_JSON>
```

The PC vehicle runner imports AH60, checks its 36 named source parts, 27,388
triangles, 34-visible/2-hidden part state, 11 hidden double-`Wreck` fragments,
visible single-`Wreck` collections and healthy fragments, known part
translations, verified
WingLeft LOD0 topology and four-channel UV declaration,
Main/glass/interior/minigun graphs, materials and file-backed images, then
validates save/reopen persistence:

```powershell
blender --background --factory-startup `
  --python tests\blender_vehicle_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

The tracked-vehicle runner verifies repeated source components on BMD3, M1A2,
and T90; imports and reopens independently transformed BMD3 and T90 track
instances with source-backed local armatures; checks direct `T90_Mesh.res`
promotion; and confirms that `T90Tracks_Mesh.res` remains focused:

```powershell
blender --background --factory-startup `
  --python tests\blender_vehicle_track_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

The PC MI28 runner checks the distinct byte-32 composite declaration,
24/28/36/40 UV layers across all ten LOD0 subsets, 24 parts, 44 surface
fragments, 21,032 imported triangles, Main/MainMetal material routes, and the
transmissive windshield before and after save/reopen:

```powershell
blender --background --factory-startup `
  --python tests\blender_mi28_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

The PC vehicle-family runner imports and reopens Zero Fighter, Corsair, and
UAV. It verifies Zero Fighter's 24 objects, 8,459 triangles, byte-48 selector,
36/40/44 coordinate declaration, and outer/inner glass; Corsair's 24 objects,
7,198 triangles, color/cube/decal routes, and repeated normal; and UAV's eight
objects, 3,107 triangles, byte-28 color/detail route, and repeated detail. It
also parses representative AH60, AH64, MI28, M1A2, and T90 LOD0 resources to
lock the recurring declaration families:

```powershell
blender --background --factory-startup `
  --python tests\blender_vehicle_family_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND>
```

The all-vehicle structural/material audit enumerates the 62 unique indexed
retail resources. Its JSON report preserves per-subset declarations and
evidence, surface matches, database provenance, layouts/variants, binding
paths/roles/dimensions, physical routes and transforms, explicit fallbacks,
and source limitations:

```powershell
blender --background --factory-startup `
  --python tests\blender_vehicle_catalog_audit.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_JSON>
```

## Cross-version evidence boundary

Import-only references from later Frostbite revisions corroborate general
principles: declaration-driven attributes, independent coordinate and
tangent-frame fields, one explicit V conversion, subset-local skin palettes,
and four byte indices/weights. These are secondary design constraints, not BC2
numeric-layout evidence.

They provide no BC2 retail fixture, binary round trip, or validation for disc
containers, FbRB, Xbox texture storage, terrain, binary DBX level graphs,
shader arithmetic, Granny animation, or export. BC2 retail correlation
independently established global type-2 part IDs, shared-index rebasing,
ordered LOD0 names, and the 18-float bounds/bind-transform record. It still
does not establish runtime pivots, hierarchy, or state animation.

The reusable cross-version principles are recorded with their owning topics in
[Meshes](formats/meshes.md#declaration-and-dcc-interoperability-principles),
[Granny characters and animation](formats/granny-animation.md#skeleton-and-dcc-interoperability-principles),
[Shaders and materials](shaders-and-materials.md#material-interoperability-boundary),
and [Import performance](import-performance.md#dcc-conversion-performance-boundary).

The separate Frostbite 1 archive, DBX, and PC mesh writer review produced
synthetic no-edit failure evidence and preservation requirements, not supported
export behavior. Those findings now live with the phased writer design in
[Import and export design](import-export-design.md#external-writer-evidence).

## Known heuristics

These behaviors are useful but need broader format research:

- MeshData payload-header search.
- Half-float versus float position scoring across all related subsets sharing
  a vertex stride.
- Topology- and normalized-range-based coordinate selection for unknown PC
  composite declarations. The resolver is surveyed across 62 vehicle LOD0
  resources, but coordinate meaning remains shader-specific.
- Skinned vertex influence offsets.
- PC Granny field locations and curve codecs outside the documented human and
  Raven v7 samples.
- PC additive animation wrapper semantics outside the four verified
  `ske01` facial-delta samples.
- Related-animation discovery outside exact folders and explicitly linked
  character/shared trees. The current bounded selection and 64-clip safety
  limit are implemented preview policy, not reconstruction of the runtime
  state machine.
- Mesh-set-to-MeshData resolution by path.
- Composite runtime pivots, general parent hierarchy, automatic state
  switching, and animation. Type-2 membership, ordered LOD0 names, bounds, and
  mesh-set bind transforms are verified. Direct PC track-component mesh,
  skeleton, and local transforms are additionally verified on BMD3, M1A2, and
  T90, whose immediate chassis parents are identity; non-identity ancestor
  composition and runtime track/wheel driving remain unsupported.
  Wreck/damaged name classification remains a fallback where DBX component
  state data is incomplete.
- TerrainHeightfield trailing offset/LOD table after the height samples.
- Small FbRB declared-payload discrepancy tolerance.
- Binary DBX 32-bit integer-versus-float rendering.
- Grouped Xbox mip handling currently recovers only the base surface.
- Non-MedKit DXT5nm decode and normal strength.
- Cutout-family classification and Blender dithered-alpha approximation.
- Index-based subset-to-surface fallback when normalized names do not match.
- Terrain detail-repeat scale and semantic filename-family selection. The
  terrain partition itself is source-derived from the material-map path, but
  mapping a class such as Snow or SeaBed to one candidate color texture is a
  preview heuristic and is not exporter-safe.

Tests for a heuristic should include at least one sample that would make a
naive alternative choose incorrectly.

## Unsupported or incomplete areas

- Full vertex declarations, normals, tangents, colors, and exporter-safe
  semantics for topology-resolved composite coordinate layouts.
- Complete shader arithmetic, constants, swizzles, UV sources, and all texture
  semantics.
- Xbox whole-map assembly and source-backed console mesh-set materials,
  skeletons, and Actions. Current console rigid, tree, and skinned mesh-set
  imports are geometry-only.
- PS3 disc/source support and texture validation. Recognizing the
  `Ps3Texture` type name is not verified native support.
- Xbox 360/PS3 Granny skeleton/model and animation conversion.
- Granny arrays, objects, track groups, or transform tracks outside section 0;
  vector tracks; time-varying or nondecomposable scale/shear; and unimplemented
  curve codecs.
- Complete animation state-machine semantics, runtime clip phase/rate control,
  and automatic import of the hundreds of clips reachable from shared human
  graphs.
- Full DBX type coverage and lossless round trip.
- Nonstatic level systems such as effects, game logic, lights, sound,
  animation, destruction states, and collision.
- Exact terrain shader blending, decals, atlas masks, and lossless terrain
  writing.
- Exact WaterMesh shader reproduction.
- Audio, SWF, Bink, physics, and most other resource decoders.
- FbRB writing and repacking.
- Cabinet, MSI, ISO9660, XDVDFS, and XGD2 writing.

## How to record new findings

When adding knowledge:

1. State the platform and resource type.
2. Separate byte-level observation from interpretation.
3. Record offsets relative to a named structure.
4. State byte order per field.
5. Say how the claim was validated: parser invariant, synthetic fixture,
   cross-platform comparison, decoded output, or shader inspection.
6. Mark unresolved fields and preserve their bytes.
7. Update this document's coverage or unknowns list.

Do not include personal disc paths, copyrighted payloads, credentials, or
decompiled source. Small independently authored structural diagrams and numeric
test fixtures are preferred.
