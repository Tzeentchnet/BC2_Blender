# Mesh formats

## MeshData platform split

The tested MeshData structure is the same on PC and Xbox 360 except for byte
order:

| Platform | Byte order |
|---|---|
| PC | little-endian |
| Xbox 360 / PS3 convention | big-endian |

When platform metadata is unavailable, the parser scores both interpretations
using the mesh type and subset count. Platform-directed decoding is preferred.

## Header and subset records

The resource begins with:

| Offset | Type | Meaning |
|---:|---|---|
| `0x00` | `uint32` | mesh type |
| `0x04` | `uint16` | unknown |
| `0x06` | `uint8` | subset count |
| `0x07` | variable | first subset record |

Each subset begins with a NUL-terminated Latin-1 name, followed by:

| Relative field | Type | Meaning |
|---|---|---|
| `primitive_count` | `uint32` | triangle count in tested data |
| `vertex_count` | `uint32` | vertices in the subset |
| `index_offset` | `uint32` | first index, not byte offset |
| `vertex_offset` | `uint32` | byte offset into the shared vertex buffer |
| `stride` | `uint8` | bytes per vertex |
| `primitive_type` | `uint8` | `3` for supported triangle lists |
| `bones_per_vertex` | `uint8` | influence-layout hint |
| `palette_count` | `uint8` | bone palette entries |
| `palette_count_2` | `uint16` | duplicate count; must agree |
| palette | `uint16[]` | type-1 global bone indices or type-2 global part IDs |

Subset names containing `ZOnly` identify shadow/depth-only geometry in tested
assets and are skipped by default.

## Locating the geometry payload

Unknown metadata can follow the subset records. The parser searches for three
integers:

```text
vertex_buffer_size
index_buffer_size
reserved == 0
```

A candidate is accepted only when:

```text
candidate + 12 + vertex_buffer_size + index_buffer_size == resource_size
```

The search is bounded to at most 1 MiB beyond the parsed subset metadata. This
is a strong structural check but still a heuristic; a writer needs the actual
intermediate metadata schema.

The index buffer follows the vertex buffer and contains platform-endian
`uint16` indices. Supported primitives are triangle lists, so each primitive
uses three indices.

## Vertex decoding

The current importer intentionally decodes only the attributes needed for a
usable Blender preview:

- Position starts at byte zero of each vertex.
- Position is either three half-floats (6 bytes) or three floats (12 bytes).
  In automatic mode, finite range, variation, implausible magnitude, and
  extreme axis imbalance are scored over distributed sample vertices.
  Selection is performed once across all subsets sharing a stride in one
  MeshData resource, not independently per subset.
- UV0 is a half-float pair selected by declaration rules or a stride-aware
  heuristic. General candidate scanning starts at byte 16 so packed
  normal/tangent fields at earlier offsets are not misread as plausible UVs.
  Tested preferred offsets include 16, 20, 24, 28, and 32 bytes depending on
  stride. Unknown little-endian type-2 composite declarations use the more
  constrained topology resolver documented below.
- The common retail PC 48-byte static declaration stores
  `R16G16B16A16_FLOAT` inputs at bytes 24 and 32. The inspected vertex shaders
  consume their `.zw` halves as the object and repeating coordinates, while
  the `.xy` halves carry selector/atlas coordinates:

  | Shader route | Byte offset | Blender layer |
  |---|---:|---|
  | `v3.zw -> o8.xy` | 28 | `UVMap` |
  | `v4.zw -> o8.zw` | 36 | `UV1` |
  | `v4.xy -> o9.xy` | 32 | `UV2` |
  | `v3.xy -> o9.zw` | 24 | `UV3` |

  This declaration is verified on Ship System, Harbour, PileBricks, GasSilo,
  and IND_Silo_Medium. The importer uses it only when the declared object and
  repeating pairs decode structurally; other legitimate 48-byte declarations
  fall back to per-mesh detection.
- The retail PC stride-48 composite declaration whose repeated part-ID field
  is at byte 36 exposes four half-float shader-coordinate pairs:

  | Vertex input | Byte offset | Blender layer |
  |---|---:|---|
  | `v2.xy` | 24 | `UVMap` |
  | `v2.zw` | 28 | `UV1` |
  | `v3.xy` | 32 | `UV2` |
  | `v5.xy` | 40 | `UV3` |

  The importer identifies this declaration across related type-2 subsets from
  the verified byte-36 part field, including one-part subsets where the field
  offset cannot be inferred independently. Padding at byte 44 is not exposed
  as a coordinate layer. Which layer is a color UV remains shader-specific;
  for AH60 Main, byte 28 is the nondegenerate base-color channel.
- The retail PC MI28 uses a distinct stride-48 composite declaration. Its
  repeated part-ID field is at byte 32, between two packed coordinate inputs:

  | Vertex input | Byte offset | Blender layer |
  |---|---:|---|
  | `v3.xy` | 24 | `UVMap` |
  | `v3.zw` | 28 | `UV1` |
  | `v5.xy` | 36 | `UV2` |
  | `v5.zw` | 40 | `UV3` |

  This layout is **verified on PC retail MI28 LOD0**. The importer propagates
  the declaration identified by multi-part subsets to related one-part
  subsets with the same stride, including `lod0_Windshields_Glass`, where a
  repeated field cannot independently identify byte 32. The stored part
  metadata remains unset for those one-part subsets; only declaration
  selection is inherited.
- Unknown little-endian PC type-2 composite declarations use an
  **implemented heuristic**, not a vehicle-name table. Aligned half-float
  pairs are considered from byte 24 through the end of each vertex. The
  inferred repeated part-selector field is excluded. For all related subsets
  sharing the stride, each remaining field is scored by:

  - the fraction of source triangles with nonzero two-dimensional UV area;
  - the fraction of values within the approximately normalized
    `[-0.05, 1.05]` range.

  The highest deterministic score becomes `UVMap`. Adjacent and strongly
  supported physical coordinate fields are retained, up to six channels, and
  the resulting declaration is propagated to related one-part subsets where
  a repeated selector cannot be inferred independently. The subset's
  `part_index_offset` remains unset in that case; declaration propagation
  does not fabricate part metadata.

  This resolves recurring declarations without asset-specific exceptions.
  It is still preview-import evidence, not a safe writer specification:
  topology can identify useful coordinates but cannot establish every shader
  semantic or whether a constant declared field must be written back.
- Every imported subset records the implemented evidence source for its UV
  ordering: verified PC declaration, vehicle shader-route candidates,
  topology fallback, stride heuristic, or no decoded coordinate. Blender
  stores this as `bc2_uv_source`; topology-selected channels are therefore not
  indistinguishable from shader/declaration evidence.
- PC type-1 vehicle LOD0 resources use more than the bird/character
  `40/44` pair. Retail track, stationary-gun, and parachute variants route
  samples through byte 32 or 36. Vehicle imports retain
  `40/44/32/36` as physical candidates, preserving the established
  `40/44` order while allowing shader variants to map byte 32 to `UV2` and
  byte 36 to `UV3`. This category-gated retention is **implemented shader
  support**, not proof that all four pairs are authored texture coordinates
  on every type-1 resource; nonvehicle skinned imports keep their verified
  declaration behavior.
- PC type-0 stride-48 vehicle shaders can declare a fifth coordinate at byte
  40 in addition to the common `28/36/32/24` ordering. Vehicle imports retain
  byte 40 when it decodes as a bounded half-float pair. This is required by
  the verified Ka-52 wreck variant, whose normal/detail/mask samples route to
  byte 40. Ordinary nonvehicle static imports keep the four-field declaration.
- Two stride-64, byte-48-selector samples show why the resolver preserves
  multiple physical channels:

  | PC retail sample | Imported offset order | Evidence |
  |---|---|---|
  | Zero Fighter LOD0 | 36, 40, 44 | Pixel shader `401` samples normal, color, and repeated dirt/detail from byte 36. Byte 36 is therefore the verified primary coordinate; bytes 40 and 44 are retained heuristic candidates. |
  | T90 LOD0 | 36, 32, 40, 44 | Pixel shader `94` uses byte 36 for base color, dirt, and generic mask; byte 32 for normal/detail; and byte 40 for symbols. Byte 44 is retained as an additional candidate. |

  Zero Fighter vertex shader `1107` also consumes the byte-48 part selector.
  On its MainBody and glass subsets, the former byte-24 fallback is
  topologically degenerate while byte 36 is not.
- A retail PC survey parsed all 62 unique vehicle LOD0 MeshData resources in
  the indexed catalog with no failures: 21 type-0, nine type-1, and 32 type-2
  meshes. A separate shader/layout audit resolves every standard layout field
  through explicit widths, including field code `258` as an 8-byte
  two-component input. Representative recurring type-2 families include AH64 at selector
  byte 44 with primary byte 28, M1A2 at selector byte 40 with primary byte 28,
  Zero Fighter and T90 at selector byte 48 with primary byte 36, plus the
  exact AH60 and MI28 declarations above. This is broad parser evidence, not
  proof that every vehicle texture binding uses `UVMap`.
- The implemented little-endian type-1, palette-backed stride-48 skinned
  declaration exposes two half-float coordinate pairs:

  | Mesh byte offset | Blender layer |
  |---:|---|
  | 40 | `UVMap` |
  | 44 | `UV1` |

  This layout is verified structurally on the retail Raven and Parrot LOD0
  meshes and 50 inspected stride-48 character subsets. Raven's byte-40
  `UVMap` has nonzero UV area on all 1,130 triangles. Bytes 24 through 39
  contain packed tangent-frame data; applying the static stride-48
  28/36/32/24 mapping to this skinned layout produces degenerate or `-1..1`
  vector fields instead of texture coordinates.
- The tested little-endian stride-64 skinned-character declaration is
  explicit rather than heuristic. It exposes four half-float shader-coordinate
  pairs:

  | Vertex input | Byte offset | Blender layer | Representative shader use |
  |---|---:|---|---|
  | `v6.xy` | 40 | `UVMap` | primary color and primary normal |
  | `v3.xy` | 16 | `UV1` | nominal auxiliary/detail coordinate |
  | `v6.zw` | 44 | `UV2` | nondegenerate secondary UV and masks |
  | `v7.xy` | 48 | `UV3` | baked character occlusion |

  DX11 vertex shaders 424 and 920 both declare these offsets and write the
  values to pixel interpolants `o10.xy`, `o10.zw`, `o11.xy`, and `o11.zw`.
  Decoding offsets 24 and 28 as UVs is incorrect for this layout because those
  bytes are packed tangent-frame inputs. Because these channels are
  declaration-backed, a constant channel remains valid; the US head eyelash
  subset verifies that zero span must not be treated as corrupt UV data.

  On retail `US_01`, every byte-16 pair on `LegStandard`, MOLLE, and pouches
  satisfies `u == v`, so every triangle has zero 2D UV area. The pair remains
  preserved as `UV1` because it is part of the source declaration, but the
  Blender material adapter routes the affected layered bindings through
  byte-44 `UV2`. That channel is nondegenerate and removes the visible zigzag
  corruption.
- For tested type-1 skinned layouts with a palette, four byte-sized palette
  indices are stored at vertex offset `8` and four byte weights at offset
  `12`.
- `bones_per_vertex == 1` in that skinned layout still uses the first
  per-vertex palette-index byte. A subset palette can contain multiple rigid
  attachment bones; assigning every such vertex to palette entry zero is
  incorrect.
- Weights are normalized to sum to one. If no usable weights exist, the first
  palette entry receives full weight.
- Type-4 tree meshes also carry palettes with `bones_per_vertex == 1`, but
  they do not use the validated skinned vertex declaration. The parser
  preserves its conservative first-palette-entry fallback instead of treating
  packed static vertex data as a bone index.

### Type-2 composite index streams and part IDs

Retail type-2 composite meshes use two index conventions. A subset whose
maximum index is below its declared vertex count is already local. Otherwise,
the subset can address a shared cumulative vertex stream:

```text
base = min(subset indices)
max(subset indices) - base < vertex_count
vertex byte address += base * stride
Blender face index = source index - base
```

The parser rejects a range that does not fit after rebasing. AH60 begins with
seven shared-stream subsets and later switches to subsets with explicit vertex
offsets and local indices. Across the surveyed retail type-2 LOD0 resources,
24 visible subsets require this correction.

The same type-2 subset palette contains **global composite part IDs**, not
type-1 local skin slots. Part membership is stored per vertex as four repeated
bytes in an aligned declaration field. The byte offset is declaration
dependent: verified stride-32/48/64 samples use offsets 28, 32, 36, 40, 44,
or 48. The implemented inference:

1. scans four-byte-aligned fields;
2. requires all four bytes to agree and the value to belong to the subset
   palette for every vertex;
3. groups identical candidate sequences;
4. chooses the unique sequence with the greatest distinct part coverage,
   preferring exact palette coverage.

A one-entry palette assigns that one part without guessing a field offset.
Triangles must have one part ID across all three vertices; crossing triangles
are rejected rather than duplicated or silently assigned. Per-part output
compacts positions, every retained UV channel, influences, and face indices.

Ordered NUL-terminated strings in the mesh-set resource provide global names:

```text
lod0|MainFuselage
lod0|Door_Left
...
```

For records with decoded placement data, the bytes immediately after the name
terminator are 18 platform-endian `float32` values:

```text
bounds_min[3]
bounds_max[3]
linear_transform[3][3]  # row-major
translation[3]
```

PC records are little-endian; Xbox 360 and PS3 records are big-endian. Part ID
`n` maps to the `n`th `lod0|` record. Geometry remains in its authored local
coordinates and the 3x3 plus translation is applied as the Blender object
matrix. The matrix uses the same Frostbite-to-Blender basis conversion as map
placements, `(x, y, z) -> (x, -z, y)`; only the translation is multiplied by
the configured unit scale.

A survey of 156 retail type-2 LOD0 meshes decoded all 542 visible
palette-bearing subsets without an ambiguous field, and no triangle among
552,570 surveyed triangles crossed a part boundary. Ordered names covered 151
resources. Five resources without recognizable names use stable `Part_###`
fallbacks: MP09 Dam, static A10, static F18, static Su39, and the GMG QLZ
ground mount.

The decoded matrix is the mesh-set bind placement, not evidence of a runtime
pivot or hierarchy. Composite runtime pivots, parent links, state switching,
and animation remain unknown and are not inferred.

Normals, tangents, colors, morph data, complete shader semantics, and every
vertex declaration variant are not yet decoded generically. Unknown PC
composite coordinate fields are topology-resolved as described above, but
that preview heuristic is not a complete vertex-layout specification and is
not exporter-safe.

### MaxScript cross-check

The BF3 and BF4 MaxScript importers in the supplied external reference set are
for later Frostbite revisions, so their numeric layout identifiers are not
copied into the Frostbite 1 parser. BF3 and BF4 use different header offsets
and submesh record sizes, directly demonstrating that those values are
version-specific. They nevertheless confirm useful format rules:

- vertex-element offsets come from the mesh declaration, not from stride-only
  guesses;
- `Vert_Diffuse_UV` (`1570`) and `Vert_Normals_UV` (`1569`) are distinct
  half-float-pair semantics, while tangents, binormals, and unknown fields
  have separate identifiers;
- both recognized UV channels receive an explicit V flip when converted to
  the DCC coordinate convention.
- tangents and binormals are separate declared half-float vectors;
- type-1 vertices carry four local byte indices and four byte weights divided
  by 255, while a submesh palette maps local slots to global skeleton bones;
- the BF3 v4.1 revision gates skinning on object type 1, matching the current
  parser's need to isolate type-1 declarations from other palette-bearing
  meshes.

BF3 v4.1 also groups object-type-2 vertices by their four-byte index tuple and
offers to detach them as vehicle parts. That remains non-authoritative for
BC2, but the independently verified retail rules above now corroborate its
high-level distinction between type-1 skinning and type-2 part membership.
The later script still does not provide BC2 offsets, global index rebasing,
ordered part names, hierarchy, bind-transform records, or pivots.

The scripts build simple `_d`, `_s`, and `_n` standard materials and do not
decode Frostbite 1 shader-database layer arithmetic. Their scale of 39 and the
BF4-only 90-degree X rotation are DCC choices, not BC2 unit or axis evidence.
They support semantic-driven decoding but are not authoritative for BC2
layouts, character materials, or transforms. See the complete
[external MaxScript audit](../maxscript-reference-audit.md).

Retail shader declarations correct the earlier offset-24 interpretation for
`Objects/Objects/GasSilo/GasSilo` and
`Objects/Architecture/Industry/IND_Silo_Medium/IND_Silo_Medium`. Their object
maps use byte 28, while byte 36 supplies the tiled cylindrical detail ranges.
Bytes 24 and 32 remain preserved as shader selectors rather than being
discarded or mislabeled as the two color UVs.

The PC `US_01_Body` LOD0 sample verifies the skinned offsets across 10,513
vertices in 13 visible stride-64 subsets. Every four-byte weight tuple sums to
255, every nonzero palette index is in range, 61 of the 147 source skeleton
bones are used, source bone indices range from 1 through 146, and all four UV
channels persist through Blender save/reopen.

The tested MedKit results were:

| Platform | Visual stride | Position representation | Result |
|---|---:|---|---|
| PC | 48 | half-float | 3,378 vertices, 3,130 triangles |
| Xbox 360 | 32 | half-float | geometry and UVs matched PC |

The mesh-wide rule is required by real map assets. Independent subset scoring
misclassified one half-float building-block subset as float data, expanding a
roughly 10 m object to more than 8 km, and misclassified one float bridge
subset as half-floats, expanding a roughly 71 m bridge to more than 100 km.
Shared-stride selection yields coherent bounds for both complete resources.

## Mesh-set resources

`RigidMeshSet`, `SkinnedMeshSet`, `CompositeMeshSet`, and `TreeMeshSet` locate
geometry by asset naming. The catalog looks for `<base>_lod%_data.res`,
prefers LOD0, and prefers a resource in the same FbRB. Composite payloads are also scanned for ordered `lod0|PartName` records and
their 18-float bounds/bind-transform payloads used by the type-2 split path.

For robust multi-LOD or export support, continue decoding the mesh-set resource
and retain its explicit references, runtime pivots, skeleton linkage, and
material assignments. LOD0 part membership, names, bounds, and bind transforms
are retained, but they are not a decoded runtime hierarchy.

The PC native character path currently obtains the skeleton reference from the
matching DBX `Render.SkinnedMeshAsset` descriptor rather than decoding the
`SkinnedMeshSet` payload itself. The same descriptor's first `ShaderSets`
entry supplies the default surface table; normalized longest-name matching
assigns those surfaces to visible LOD0 subsets. See
[Granny characters and animation](granny-animation.md) and
[Shaders and materials](../shaders-and-materials.md).

## Vehicle track component instances

**Verified on PC retail BMD3, M1A2, and T90. Implemented for
`TrackComponentData` in high-level vehicle imports.**

Tracked vehicles do not store one already-assembled two-sided track mesh. Two
instances in the nested `VehicleEntityData.Components` graph reference the
same skinned mesh and skeleton:

```text
VehicleEntityData
  Components[] -> VehicleComponentData
    Components[] -> TrackComponentData
      Name
      Transform
      Mesh -> Render.SkinnedMeshAsset
      Skeleton -> GrannyModel
      ScrollFactor
```

The PC binary DBX numeric words are big-endian. `Transform` contains 16
`float32` words in column-major order; zero-based words 12, 13, and 14 are
translation X, Y, and Z. Placement uses the upper-left 3x3 and this
translation, converts `(x, y, z)` to Blender `(x, -z, y)`, and scales only the
translation. The unused fourth row is not treated as placement data. This is
important for T90, where the typed DBX renderer exposes non-finite marker
words as their integer fallback value.

| Vehicle | Instance | Source translation `(x, y, z)` |
|---|---|---|
| BMD3 | `Track Right` | `(-1.567699, 0.002113, -0.009618)` |
| BMD3 | `Track Left` | `(1.567699, 0.002113, -0.009618)` |
| M1A2 | unnamed instance 1 | `(-1.628634, -0.050659, -0.091767)` |
| M1A2 | unnamed instance 2 | `(1.613156, -0.063159, -0.091767)` |
| T90 | `Track Right` | `(-1.629941, 1.023512, -0.025497)` |
| T90 | `Track Left` | `(1.612939, 1.023512, -0.025497)` |

The asymmetry in the M1A2 values is direct evidence that a preview importer
must preserve both source instances rather than mirror one side or apply a
vehicle-name offset. The immediate chassis parent has an identity transform
in all three verified samples.

The importer creates one local source-backed armature per PC track instance,
binds a fresh instance of the shared skinned geometry, and applies the exact
component transform to that armature. BMD3's track resource has one LOD0
subset with 1,138 vertices and 1,096 triangles; its referenced skeleton has
eight bones. Component GUID, type, full 16-word transform, mesh path, and
skeleton path are retained as Blender metadata.

This does not reconstruct track motion. `ScrollFactor`, wheel-component
physics, runtime bone driving, and non-identity ancestor-transform composition
remain unsupported. Directly importing `BMD3Tracks_Mesh.res` correctly imports
one focused mesh component; only the high-level BMD3 entity supplies the two
vehicle instances and their placement. Export code must retain the two DBX
component records and their references rather than deriving them from the
mesh resource.

## WaterMesh

WaterMesh uses platform byte order: little-endian on PC and big-endian on Xbox
360. The implemented layout is:

| Location | Type | Meaning |
|---|---|---|
| start | `uint32` | body count |
| per body | `uint32` | vertex count |
| next | `float32[vertex_count][3]` | positions |
| next | `uint32` | index count |
| next | `uint32[index_count]` | triangle-list indices local to this body |

The index count must be divisible by three, every index must be below its
body's vertex count, and the final body must consume the resource exactly.
Multiple bodies are joined with rebased indices. Empty bodies are valid.

The verified Xbox 360 `MP_001` resource is 18,996 bytes: one body, 569
vertices, and 3,039 indices forming 1,013 triangles. The 32-bit interpretation
references all 569 vertices and consumes the payload exactly. The former
16-bit interpretation happened to produce 1,013 malformed triangles, but
referenced only vertices `0..306` and silently left 6,078 bytes unread, which
removed the harbor water from the imported topology. The same body/u32-index
layout is independently corroborated for all 36 PC terrain bundles by
[`bfbc2core/water.py`](https://github.com/mathewsbinny/bfbc2-tool-suite/blob/main/core/bfbc2core/water.py).

The current importer has no water UV or exact material decoding.

## Blender coordinate conversion

Frostbite mesh coordinates are treated as Y-up. With the default conversion:

```text
(x, y, z) -> (x, -z, y)
```

This is a rotation into Blender Z-up space. The optional unit scale is applied
at the same time. UV V is independently flipped by default to convert DirectX
texture orientation. This UV conversion is separate from placement transforms;
the importer does not reflect object matrices to change texture orientation.

## Complete-map mesh assembly

For whole-map import, visible MeshData subsets are combined into one Blender
mesh:

- subset vertex ranges are concatenated;
- triangle indices are offset into the combined vertex range;
- one material slot and polygon material index are retained per subset;
- available UV channels become Blender `UVMap`, `UV1`, `UV2`, and `UV3` in
  source declaration order;
- a MeshData resource is parsed only once;
- a Blender mesh datablock is created per geometry/shader-set variant;
- every placement of that variant links the same datablock.

The verified `MP_001` set contains 178 unique LOD0 MeshData resources and 211
geometry/material variants for 5,380 placed objects. All resources parse, with
observed strides of 32, 48, 64, 68, 72, and 76 bytes. Of their visible
subsets, 347 use the common 28/36/32/24 static declaration; other subsets
retain their distinct detected or declaration-backed layouts.

In the common static layered permutation, identified OcRGB object bindings
use `UVMap` at byte 28 and repeating details use `UV1` at byte 36. Binding
routing remains shader-specific for other declarations.

This is Blender instancing, not reconstruction of Frostbite runtime culling or
destruction systems. See [Levels and complete maps](levels-and-maps.md).

## Native writer cross-check

The supplied BC2-oriented C++ tool includes a PC-style FBX-to-MeshData and
mesh-set writer. Unlike the later-engine MaxScripts, it corroborates several
Frostbite 1 structures directly:

- four count-prefixed byte arrays between subset metadata and geometry sizes;
- 89-byte shader-database vertex-declaration records selected through mesh
  reference GUIDs;
- declaration words containing an element code and byte offset;
- little-endian `uint16` triangle indices;
- rigid, composite, and tree mesh-set records with part names, hashes, bounds,
  and transforms.

A **Verified (PC retail)** sample of 60 `MP_001` MeshData resources had all
four byte arrays end exactly at the geometry-size triplet. Five composite
resources matched the reference part hash for all 48 decoded parts. Two local
PC shader-database samples linked every surveyed mesh-reference GUID to an
89-byte record; the larger sample contained 61 generic declarations, of which
11 used element codes unknown to the external writer. Only 24 of 50 fully
recognized declarations filled the complete stride, making trailing padding
or unknown data an explicit preservation requirement.

The writer is not safe to adapt directly. It is host-little-endian, leaves
skinned generation unfinished, writes weights through an unsupported path,
does not enforce the 16-bit index limit, zeroes unknown declaration fields,
forces tangent/default signs, and replaces composite rotations with identity.
It also maps FBX control points to output vertices while selecting UVs and
normals from one polygon corner, so loop-specific seams require a separate
vertex-expansion pass. See the
[external writer audit](../external-writer-reference-audit.md).

## Writer requirements

A safe MeshData writer needs more than the current preview model:

- preserve unknown header and inter-buffer metadata;
- preserve the four count-prefixed subset-order arrays;
- preserve the raw declaration record, GUID, element offsets, and stride
  padding, or explicitly regenerate the complete declaration;
- retain all attributes and original quantization;
- preserve subset order, names, Z-only subsets, palettes, and mesh-set links;
- expand Blender loop/corner data when UVs, normals, tangents, materials, or
  weights require distinct output vertices;
- confirm alignment and offset units for every target platform;
- verify triangle winding and tangent handedness after coordinate and UV
  conversion;
- enforce the 16-bit index limit or reproduce the game's subset splitting.

Round-trip tests should compare untouched resources byte-for-byte before any
semantic editing is attempted.

See [`mesh.py`](../../bc2_frostbite_importer/core/mesh.py).
