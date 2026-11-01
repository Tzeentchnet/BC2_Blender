# Terrain and DBX formats

## TerrainHeightfield

The tested TerrainHeightfield starts with a platform-endian spatial and
metadata header followed by little-endian height samples:

| Offset | Type | Meaning |
|---:|---|---|
| `0x00` | `float32[6]` | min X, min Y, min Z, max X, max Y, max Z |
| `0x18` | `uint32` | square resolution |
| `0x1C` | `uint32[5]` | LOD metadata; partially understood |
| `0x30` | `uint8` | zero in tested resources |
| `0x31` | little-endian `uint16[resolution * resolution]` | height samples |

Verified header byte order:

| Platform | Bounds and resolution |
|---|---|
| PC | little-endian |
| Xbox 360 | big-endian |

The tested Xbox resource has a 2048 x 2048 grid and little-endian height
samples even though its bounds, resolution, and metadata are big-endian.
Tested PC resources also use little-endian samples. Header byte order is
selected from the known platform and validated against finite ordered bounds,
a resolution from 2 through 8192, and the available byte length. When platform
metadata is unavailable, both header interpretations are scored. Sample byte
order is fixed little-endian; it is not selected by a smoothness heuristic.

Sample conversion is:

```text
x = min_x + column * (max_x - min_x) / resolution
z = min_z + row    * (max_z - min_z) / resolution
y = min_y + raw_u16 * (max_y - min_y) / 65535
```

The core parser returns positions as `(x, z, height)`. Blender's default axis
conversion creates `(x, -z, height)`. Each grid quad is split along its
top-left to bottom-right diagonal, matching the retail terrain query.

The Blender setting limits the number of vertices per edge to 1024 and now
defaults to 1024. If downsampling is requested, the parser distributes exactly
the requested number of sample indices evenly across the source edge and
always includes its first and final serialized rows and columns. Because the
bounds describe the complete grid domain, the last serialized sample lies one
source cell short of the maximum bound. NumPy-backed position and topology
generation keeps a full 1024 x 1024 tile practical; a pure-Python fallback
remains available.

An exporter must retain the original resolution and raw samples. A downsampled
preview mesh is not a reversible terrain representation.

The PC `MP_001.Heightfield-000` resource has 129,818 bytes after its samples.
The suffix begins with a table of little-endian offsets `0, 2, 4, ...` and is
probably LOD data; it remains undecoded.

### `MP_001` terrain composition

The PC map uses 13 heightfields:

| Resource range | Resolution | Role |
|---|---:|---|
| `MP_001.Heightfield-000` | 1024 x 1024 | high-resolution central terrain |
| `Heightfield-001` through `-012` | 64 x 64 each | twelve low-resolution outer cells |

The center spans approximately `(-512, -512)` through `(512, 512)` in the X/Z
plane. The outer cells extend the verified terrain footprint to approximately
`(-1024, -1024)` through `(1024, 1024)`.

The serialized terrain entity translation is approximately
`(0, -89.2776, 0)` in Frostbite coordinates. Heightfield and WaterMesh
coordinates already match placement world heights, so applying this
translation would move them down a second time. The importer retains it as
metadata and deliberately does not apply it.

The former fixed-step 256 setting selected only 206 vertices from this
1024-wide source. More importantly, triangulating a coarse sample can bridge
sharp depressions: comparison against the full source measured local
overestimation of approximately 55 m, with about 6.8% of source samples more
than 1 m below the coarse triangles. `MP_001.Heightfield-000` therefore
defaults to its complete 1,048,576-vertex source grid. This fixes local high
terrain shelves; it is not a global Z-offset correction.

The former byte-`0x2C` parser started five bytes before the sample grid and
then selected big-endian words by apparent smoothness. On PC MP_001 that
produced a false height range of approximately `0.12..482.43` m and adjacent
jumps up to `482.31` m. The verified byte-`0x31` little-endian grid is
approximately `109.30..276.49` m, with a maximum adjacent edge of `39.93` m
and maximum isolated four-neighbor deviation of `8.16` m. Correcting the
header boundary, byte order, spacing, and quad diagonal removes the imported
terrain spikes rather than filtering authored samples.

The associated WaterMesh has 569 vertices and 1,013 triangles. Its unplaced
height is approximately 130.5 through 133.25. Its triangle list uses 32-bit
indices; the corrected topology references every vertex and restores the
previously missing harbor surface.

## TerrainMaterialMap

The PC terrain DBX resolves `Terrains/mp_001/MP_001-MaterialMap.res` as a
`TerrainMaterialMapAsset`. This is a custom indexed-material resource, not an
`ITexture`. The implemented parser selects platform byte order and validates
the following structure:

| Field | Type | Meaning |
|---|---|---|
| map side | `uint32` | square output dimension in pixels |
| tile count | `uint16` | number of encoded 512 x 256 tiles |
| tile body size | `uint32` | compressed byte count for one tile |
| tile body | `byte[]` | row payloads concatenated in row order |
| row sizes | `uint16[256]` | compressed byte count for each tile row |
| name table size | `uint32` | byte count after the final tile |
| name table | `byte[]` | NUL-separated ASCII material names |

Each tile row expands to 256 bytes. A normal byte is literal. Two equal bytes
followed by a count byte encode `count + 1` copies of that value. Each expanded
byte stores two four-bit material indices: the low nibble is the left pixel
and the high nibble is the right pixel. Tiles are placed left-to-right, then
top-to-bottom.

The verified `MP_001` resource is little-endian, has a 4096 x 4096 output and
128 tiles, and names these indices:

```text
defaultmaterial
Grass
Dirt
Tarmac
Gravel
Stone
SeaBed
```

The importer writes the decoded indices to a file-backed L8 DDS, samples it
with nearest filtering over global `-1024..1024` terrain UVs, and selects
source detail textures recovered from the level shader database. Current
source-backed layer choices cover jungle ground, grass, soil, asphalt, gravel,
cliff/stone, and rubble-sand families.

This graph is an implemented physical-material approximation. Its 16 m detail
repeat is estimated. Material indices and names are preserved as custom
metadata so later work can replace the graph without re-decoding the resource.

## Terrain visual decals and blending

The physical-material index map above is not the complete visual blend model.
The retail Xbox 360 `MP_001` terrain DBX provides verified structural evidence
for a separate decal hierarchy:

| Instance type | Count | Observed role |
|---|---:|---|
| `Terrain.TerrainFullDecal` | 9 | terrain-wide material layer with optional tiled masks |
| `Terrain.TerrainGroupDecal` | 2 | ordered child-decal group |
| `Terrain.TerrainSplineDecal` | 20 | material layer linked to a level spline |

A full decal retains its terrain-material reference, enable flag,
`MaskTextureType`, pixels-per-meter value, grid-cell size/resolution, and
authoring mask entries. Each mask entry has `PosMin`, `PosMax`, a source PNG
name, and an enable flag. The runtime archive does not contain those PNGs.
Instead it contains `MP_001.MaskTileAtlas.res` and `MP_001.Visual.res`.

The Xbox atlas header identifies a 2048 x 1024, one-mip texture with format
code `3` and a 1,048,576-byte block payload. That format code is not yet
implemented by the texture decoder. `VisualTerrain` retains compiled terrain
shader paths and the runtime tile/material records, but their record
boundaries and mapping back to the DBX mask regions are not decoded.

The level DBX separately contains 22 `TerrainSplineData` records: 20 Road, one
River, and one Lake plane. Spline control points use a four-component
`Position` value whose first three components are the Frostbite position.
Plane-point records reference the affected control point and retain segment
count, left/right edges, and user-mask values. The spline decals link the Road
splines to road, road-line, and dirt-road materials.

The public
[`Bad-Company-2-Map-Editor`](https://github.com/Powback/Bad-Company-2-Map-Editor)
corroborates the spline relationships, but only triangulates a plane when
`PlaneType` is `Lake`; it does not implement Road/River rendering, full decals,
the mask atlas, or blend arithmetic. The importer now reports the full/group/
spline record counts in map diagnostics rather than silently implying that
the physical-material approximation includes them.

Still unknown are format-code-3 texture decoding, `VisualTerrain` tile records,
the exact mask-atlas region mapping, layer composition order at runtime, and
the shader arithmetic/constants. These remain blockers for faithful terrain
decal rendering.

See [`terrain.py`](../../bc2_frostbite_importer/core/terrain.py).

## Binary DBX

The implemented reader decodes a tested binary DBX subset into an immutable
typed tree:

```text
DbxDocument
  DbxElement(tag, attributes, value, children, container)
```

`DbxDocument` indexes instances by GUID and exposes the primary instance.
Level resolution operates on this tree directly. XML is serialized from the
same model for human inspection. Empty containers retain their container
identity so the XML view remains compatible with the earlier reader.

The model is typed structurally, but it is not yet a lossless binary model.

### Header and strings

The resource begins with the eight-byte ASCII marker:

```text
{binary}
```

The next 16 bytes contain four big-endian `uint32` values. The parser currently
uses the third as the relocation/string-table boundary and the fourth as the
string count; the first two remain unknown.

Next are `string_count` big-endian string offsets followed by the UTF-8 string
table. The implemented table length is:

```text
relocation_offset - string_count * 4
```

Strings may end in NUL. Later references are unsigned LEB128 string indices.

### Element stream

Each element starts with a LEB128 prefix string index:

- zero closes the most recently opened container;
- nonzero names the next element.

The following byte stores the value type in its high nibble and the attribute
count in its low nibble. Each attribute is a pair of LEB128 string indices for
name and value.

Implemented value types:

| High nibble | Interpretation |
|---:|---|
| `0xA` | container; consumes one marker byte and remains open |
| `0x2` | string value referenced by LEB128 index |
| `0x7` | numeric array: LEB128 count, LEB128 element size, then values |
| other | scalar marker byte plus one-byte boolean/integer value |

Numeric arrays currently support 2-, 4-, and 8-byte elements. Four-byte values
are ambiguous between integer/hash and float. Known hash-like field names and
integer bit patterns are preserved as integers; finite alternatives can be
rendered as floats.

### Lossiness and export warning

The typed tree and XML view are useful for paths, GUIDs, shader references,
placement graphs, and object metadata. They are not sufficient for DBX writing
because the current decoder loses or guesses:

- the first two header words;
- exact scalar type information;
- marker bytes;
- original string-table layout and indices;
- unknown value types;
- integer-versus-float intent in some fields.

A DBX exporter should extend the intermediate representation to preserve raw
encodings, exact scalar types, and unknown nodes. Do not attempt XML-to-DBX
conversion from the current output.

## External-reference coverage

The audited MaxScripts contain no TerrainHeightfield, TerrainMaterialMap, or
binary DBX parser. Their bone tools consume an already generated text dump and
search for labels such as `LinearTransform`; that confirms only human-readable
field naming in the dump, not DBX byte layout, numeric-array encoding, or level
graph semantics. No terrain or DBX behavior changed from this evidence. See
the [external MaxScript audit](../maxscript-reference-audit.md).

See [`dbx.py`](../../bc2_frostbite_importer/core/dbx.py) and
[Levels and complete maps](levels-and-maps.md).
