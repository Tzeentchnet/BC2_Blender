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
- For skinned layouts with a palette, four byte-sized palette indices are
  assumed at vertex offset `8` and four byte weights at offset `12`.
- Weights are normalized to sum to one. If no usable weights exist, the first
  palette entry receives full weight.

Normals, tangents, colors, arbitrary additional UV sets, morph data, and every
vertex declaration variant are not yet decoded generically. Do not treat the
preview decoder as a complete vertex-layout specification.

The offset-24 rule is verified on the reported
`Objects/Objects/GasSilo/GasSilo` and
`Objects/Architecture/Industry/IND_Silo_Medium/IND_Silo_Medium` MeshData.
Interpreting packed fields at byte 8 as UVs produced degenerate layouts;
offset 24 yields the expected tiled cylindrical ranges, while offset 28
provides the secondary object-map coordinates.

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
- UV0 and an available secondary channel become Blender `UVMap` and `UV1`;
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
