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
| palette | `uint16[]` | global bone indices |

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
  bytes are packed tangent-frame inputs.

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
- Type-2 composite and type-4 tree meshes also carry palettes with
  `bones_per_vertex == 1`, but they do not use the validated skinned vertex
  declaration. Their part/hierarchy semantics remain undecoded, so the parser
  preserves its conservative first-palette-entry fallback instead of treating
  packed static vertex data as a bone index.

Normals, tangents, colors, morph data, arbitrary UV declarations outside the
verified static and PC skinned layouts, and every vertex declaration variant
are not yet decoded generically. Do not treat the preview decoder as a
complete vertex-layout specification.

### MaxScript cross-check

The BF3 and BF4 MaxScript importers under `E:\BFTools\Battlefield` are for
later Frostbite revisions, so their numeric layout identifiers are not copied
into the Frostbite 1 parser. They nevertheless confirm two useful format
rules:

- vertex-element offsets come from the mesh declaration, not from stride-only
  guesses;
- `Vert_Diffuse_UV` (`1570`) and `Vert_Normals_UV` (`1569`) are distinct
  half-float-pair semantics, while tangents, binormals, and unknown fields
  have separate identifiers;
- both recognized UV channels receive an explicit V flip when converted to
  the DCC coordinate convention.

The scripts build simple `_d`, `_s`, and `_n` standard materials and do not
decode Frostbite 1 shader-database layer arithmetic. They are supporting
evidence for semantic-driven vertex decoding, not an authoritative source for
the BC2 character material graph.

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

`RigidMeshSet`, `SkinnedMeshSet`, `CompositeMeshSet`, and `TreeMeshSet` are
currently resolved by asset naming rather than fully parsed. The catalog looks
for `<base>_lod%_data.res`, prefers LOD0, and prefers a resource in the same
FbRB.

For robust multi-LOD or export support, decode the mesh-set resource itself and
retain its explicit references, bounds, part hierarchy, skeleton linkage, and
material assignments.

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
