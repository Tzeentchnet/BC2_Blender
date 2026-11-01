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
- UV0 is a half-float pair selected by a stride-aware heuristic. Candidate
  scanning starts at byte 16 so packed normal/tangent fields at earlier
  offsets are not misread as plausible UVs. Tested preferred offsets include
  16, 20, 24, 28, and 32 bytes depending on stride.
- For tested 48-byte static map layouts, UV0 is at byte 24 and UV1 is at byte
  28. UV0 can legitimately tile beyond `0..1`; UV1 is usually constrained to
  that range. The parser retains UV1 when the immediately following
  half-float pair passes structural scoring.
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

Normals, tangents, colors, morph data, arbitrary UV declarations outside the
verified static and PC skinned layouts, and every vertex declaration variant
are not yet decoded generically. Do not treat the preview decoder as a
complete vertex-layout specification.

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

The offset-24 rule is verified on the reported
`Objects/Objects/GasSilo/GasSilo` and
`Objects/Architecture/Industry/IND_Silo_Medium/IND_Silo_Medium` MeshData.
Interpreting packed fields at byte 8 as UVs produced degenerate layouts;
offset 24 yields the expected tiled cylindrical ranges, while offset 28
provides the secondary object-map coordinates.

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

## WaterMesh

WaterMesh has a simpler tested layout using platform byte order:

| Offset | Type | Meaning |
|---:|---|---|
| `0x00` | `uint32` | unknown |
| `0x04` | `uint32` | vertex count |
| `0x08` | `float32[vertex_count][3]` | positions |
| after vertices | `uint32` | index count |
| next | `uint16[index_count]` | triangle-list indices |

The index count must be divisible by three. The current importer has no water
UV or material decoding.

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
observed strides of 32, 48, 64, 68, 72, and 76 bytes.

Identified OcRGB object-occlusion bindings use `UV1`; other recovered bindings
continue to use `UVMap` unless shader evidence establishes another source.

This is Blender instancing, not reconstruction of Frostbite runtime culling or
destruction systems. See [Levels and complete maps](levels-and-maps.md).

## Writer requirements

A safe MeshData writer needs more than the current preview model:

- preserve unknown header and inter-buffer metadata;
- preserve or explicitly regenerate the complete vertex declaration;
- retain all attributes and original quantization;
- preserve subset order, names, Z-only subsets, palettes, and mesh-set links;
- confirm alignment and offset units for every target platform;
- verify triangle winding after coordinate conversion;
- enforce the 16-bit index limit or reproduce the game's subset splitting.

Round-trip tests should compare untouched resources byte-for-byte before any
semantic editing is attempted.

See [`mesh.py`](../../bc2_frostbite_importer/core/mesh.py).
