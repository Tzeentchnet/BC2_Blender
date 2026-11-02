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

### Scope: binary syntax versus Blender semantics

The implementation has three distinct DBX layers:

1. [`core/dbx.py`](../../bc2_frostbite_importer/core/dbx.py) decodes an
   implemented binary subset into an immutable element tree.
2. [`core/assembly.py`](../../bc2_frostbite_importer/core/assembly.py),
   [`core/level.py`](../../bc2_frostbite_importer/core/level.py), and selected
   Blender helpers interpret a small, named set of fields from that tree.
3. When a selected DBX is not a recognized character or vehicle assembly, the
   add-on serializes the generic tree to a Blender XML Text datablock for
   inspection.

These layers must not be conflated. An element visible in the XML view is
**syntactically decoded**, not necessarily **semantically imported**. Likewise,
fields ignored by a recognized assembly do not automatically become Blender
objects or metadata.

The core tree is:

```text
DbxDocument
  DbxElement(tag, attributes, value, children, container)
```

It preserves element and attribute order, tag text, attribute text, rendered
values, child order, and whether a node was encoded as a container. It is not
a lossless binary model.

### Container header and offset bases

The implemented layout uses fixed big-endian header, offset, and numeric
storage; `parse_dbx` has no platform byte-order argument.
The generic reader has been used on the cited PC retail documents and for
Xbox 360 terrain-DBX inspection. Native complete-map and modular
character/track behavior remains PC-scoped where stated elsewhere; PS3 DBX
coverage is unknown.

| File offset | Width | Implemented interpretation |
|---:|---:|---|
| `0x00` | 8 bytes | ASCII marker `{binary}` |
| `0x08` | `uint32` | unknown header word 0 |
| `0x0C` | `uint32` | unknown header word 1 |
| `0x10` | `uint32` | combined string-offset/table span, called `relocation_offset` in the parser |
| `0x14` | `uint32` | string count |
| `0x18` | `uint32[string_count]` | string offsets |
| `0x18 + 4 * string_count` | variable | UTF-8 string table |
| `0x18 + relocation_offset` | variable | element stream |

The third header word is not established as a general Frostbite relocation
table definition. The current reader uses it only to derive the string-table
length:

```text
string_table_size = relocation_offset - string_count * 4
```

Each string offset is relative to the start of the string table, not the file
or element stream. The parser accepts at most 1,000,000 strings, requires
nondecreasing in-range spans, removes one trailing NUL when present, and
decodes UTF-8 with replacement for invalid bytes. That replacement behavior
is useful for inspection but is lossy.

String zero is normally empty and is reserved by the element grammar as the
container-close code. All later string references use unsigned LEB128
indices.

### Element stream

An element record begins with:

```text
prefix_index       unsigned LEB128
type_and_count     uint8
attributes         attribute_count pairs of unsigned LEB128 indices
payload            depends on value_type
```

`type_and_count` stores the value type in its high nibble and the attribute
count in its low nibble. Each attribute pair is `(name_string_index,
value_string_index)`, so one record can encode at most 15 attributes.

A zero `prefix_index` is a one-field close record: it closes the most recently
opened container and has no type byte or payload. A nonzero prefix resolves
the element tag through the string table. Names such as `partition`,
`instance`, `field`, `array`, `item`, and `complex` are therefore data strings,
not separate binary opcodes.

There is no decoded element count or separate element-stream length. The
parser treats every byte after `0x18 + relocation_offset` through end of file
as an element or close record.

Implemented payload handling is:

| High nibble | Consumed payload | Tree representation | Status |
|---:|---|---|---|
| `0xA` | one marker byte | open container with children | **Implemented**; marker value is discarded |
| `0x2` | one unsigned LEB128 string index | string, or `None` for an empty string | **Implemented**, but the original index is discarded |
| `0x7` | unsigned LEB128 count, unsigned LEB128 width, raw values | slash-delimited rendered numbers | **Implemented** only for widths 2, 4, and 8 |
| any other nibble | one discarded marker byte and one payload byte | `false`, `true`, or a decimal byte value | **Fallback only**; the actual scalar type is not identified |

The final row is not evidence that every other DBX value type is a two-byte
scalar encoding. An unobserved type with a different payload length could
desynchronize the remainder of the document. Such nibbles are not format
support and are not exporter-safe.

Numeric-array count is bounded to 10,000,000. Values are fixed big-endian:

| Element width | Current rendering |
|---:|---|
| 2 bytes | unsigned `uint16` decimal |
| 4 bytes | heuristic signed `int32` or `float32` |
| 8 bytes | `float64` |

The 4-byte heuristic interprets each word both as `>i` and `>f`. It chooses
integer text when the field name is one of:

```text
CompositeMeshPartNames
Hash
HashedName
HashedWaveName
Id
OnRoadMaterialNameHashes
OriginalHashedWaveName
```

It also chooses integer text for selected integer-looking bit patterns or when
the float interpretation is non-finite; otherwise it emits the finite float.
The exact implemented choice is:

```text
integer = int32_be(word)
floating = float32_be(word)
use_integer =
    field_name in known_hash_fields
    or (integer >> 24) == 0
    or (integer >> 23) in {255, -1}
    or not finite(floating)
```

Here `field_name` is the value of the record's first attribute, not a
type-checked lookup of an attribute literally named `name`.
This is an **implemented rendering heuristic**, not preserved source type
information. It is why unused transform words can appear as large decimal
integers even when the meaningful 3x3 and translation values decode as
floats.

The parser rejects unmatched close records, unclosed containers, documents
with zero or multiple roots, and roots whose tag is not `partition`.
`DbxDocument` indexes direct child `instance` elements by case-insensitive
`guid` and resolves the root's `primaryInstance` attribute. Generic parsing
does not validate `ref` targets, instance types, required fields, or external
partition paths. Duplicate instance GUIDs are not rejected; lookup retains the
last direct child with that GUID.

### XML inspection view

`dbx_to_xml` serializes the same tree, escaping attribute and text content.
Containers retain paired start/end tags even when empty; non-container nodes
with no rendered value become self-closing tags. The view preserves useful
names, references, and ordering, but omits binary value types, widths,
markers, string indices, element offsets, and raw bytes.

The Blender importer reads selected DBX files with a 256 MiB resource limit.
XML inspection is available only when the complete binary stream survives the
implemented decoder; it is not a raw-byte fallback for an unsupported value
encoding.
An unrecognized selected DBX becomes an XML Text datablock. A recognized
character or vehicle DBX becomes an assembly instead; unsupported sibling
fields in that recognized document are ignored rather than copied into a
second XML Text datablock.

### DBX fields interpreted by the Blender importer

The following matrix is the implemented semantic surface. Direct field names
are case-sensitive because `DbxElement.child` compares the stored `name`
attribute exactly. The bounded animation-list scanner is the exception: it
normalizes its supported array names with `casefold`.

| Area | Recognized structure and fields | Implemented Blender behavior |
|---|---|---|
| Mesh descriptors | primary instance `guid`, `Name`, `Skeleton`, `ShaderSets[]`; each selected shader set's `SurfaceShaders[]` entries use `Name` and `Shader` | Resolves LOD geometry, a PC Granny skeleton, subset surface order, shader references, and optional `MeshShaderSet` overrides |
| Character entities | primary `id`/`guid` attributes and `Meshes3p[]`; each complex entry uses `Mesh`, optional `MeshShaderSet`, and `RandomGroup` | Imports body/head/headgear components and binds them to one verified skeleton |
| Character animation links | `AnimTree3p`; linked fields `StaticPose`, `KitJointsAnimTree`, `ReviveAnimTree`, `FacialAnimations`, nested local `AnimTree`, and `Shared` | Finds the bounded character-specific animation trees and the explicitly linked shared tree |
| Animation-tree descriptors | primary `Skeleton`; references nested under arrays named `AnimationList`, `AimAnimationList`, misspelled `ReplacmentAnimations`, corrected `ReplacementAnimations`, and `BaseSetAnimTrees` | Resolves explicit clips and replacement poses; shared gameplay import remains a bounded named-clip policy |
| Vehicle entities | primary `id`/`guid`/`type` attributes with type ending in `VehicleEntityData`; `Mesh`, optional `MeshShaderSet`, and `Components[]` | Imports the main composite mesh and traverses the component graph |
| Vehicle state components | `HealthStates[]` entries with `Health` and `MeshName`; direct complex children containing `MeshName`; nested `Components[]` | Classifies default, alternate, damaged, and destroyed source part names |
| Vehicle tracks | component `guid`/`type` attributes with type ending in `TrackComponentData`; `Name`, 16-word `Transform`, `Mesh`, `Skeleton`, and nested `Components[]` | Creates one source-backed skinned track instance and local armature per component |
| Direct PC map placements | level primary `Objects[]`; `Entity.ReferenceObjectData` `guid`/`type` attributes and fields `Transform`, `ReferencedObject`, optional `MeshShaderSet`, and `Enumeration` | Creates linked static mesh placements |
| Blueprint and mesh references | blueprint `Object`; target `Name`, `Mesh`, and optional `MeshShaderSet`; mesh `ShaderSets[]` and surface fields above | Resolves the visual mesh and material variant for a placement |
| Compact PC map groups | four implemented group instance types; `PhysicsData`, nested `Asset`, `MemberDatas[]`, `MeshEntityType`, `Mesh`, `MeshShaderSet`, `SpawnDatas[]`, `Enumeration`; companion DBX `Name`, `Transforms`, `Enums`, and `Assets` | Reconstructs Forest, Middlegrowth, Rigid, and Static grouped visual placements and validates source order |
| Terrain links | `TerrainEntityData` fields `TerrainAsset` and `Transform`; terrain primary `MaterialMap` | Resolves heightfield/material-map/water resources, largely by resource naming after the DBX link |
| Terrain decal inventory | instance types `TerrainFullDecal`, `TerrainGroupDecal`, and `TerrainSplineDecal` | Counts records for diagnostics only; does not render or semantically decode them |

The four compact-group types are
`GameSharedResources.ForestEntityData`,
`GameSharedResources.MiddlegrowthModelGroupEntityData`,
`GameSharedResources.RigidModelGroupEntityData`, and
`GameSharedResources.StaticModelGroupEntityData`.

This table is deliberately narrower than the set of tags that the generic
reader can display.

### Syntactically visible but not semantically imported

The following major families are not interpreted into native Blender data at
this time. The list is categorical rather than an exhaustive inventory of all
retail instance type names.

| DBX family | Current behavior |
|---|---|
| Arbitrary instances and fields outside the matrix above | Preserved only in the generic tree/XML view when that view is created; ignored by known assembly and map consumers |
| Vehicle wheels, `TrackWheelComponentData`, engines, gearboxes, steering, suspension, weapons, cameras, seats/entry/exit points, radar, interaction, and component enable/enumeration rules | No runtime vehicle rig or behavior; track `ScrollFactor` and wheel/bone driving are not applied |
| General vehicle child transforms, pivots, constraints, health switching, detachable/loose-part behavior, and destruction sequencing | Not reconstructed; only verified mesh-set bind transforms, named state visibility, and direct track transforms are applied |
| Havok shapes, collision meshes, rigid bodies, constraints, fake hinges/springs, and physics materials | Not imported; the group path consumes only the companion placement arrays |
| Soldier gameplay, AI, kit/weapon logic, damage, movement state machines, and arbitrary animation-graph nodes, conditions, blends, transitions, or playback-rate logic | Not imported; only the explicit mesh and bounded clip-reference links above are followed |
| Lights, particles, effects, sounds, weather, triggers, objectives, spawn/game-mode logic, dynamic culling, and other non-visual level systems | Ignored by complete-map visual assembly; references without a supported static mesh are reported or skipped |
| Terrain full/group/spline decal contents, spline geometry, authoring mask entries, layer order, and `VisualTerrain` runtime records | Counts and source evidence may be reported, but no faithful Blender terrain-decal graph is produced |
| Unknown value-type nibbles, exact scalar types, and unsupported numeric widths | No reliable binary decode; the generic fallback must not be treated as proof of correctness |
| DBX editing, binary writing, or XML-to-DBX conversion | Unsupported |

### Lossiness and export warning

The typed tree and XML view are useful for paths, GUIDs, shader references,
placement graphs, and object metadata. They are not sufficient for DBX writing
because the current decoder loses or guesses:

- both unknown header words and the original consumed boundary/count words;
- the element type/attribute-count byte and original attribute indices;
- exact scalar value types and unrecognized type-specific payload layouts;
- container/scalar marker bytes;
- numeric-array source widths once values become slash-delimited text;
- original string bytes, NUL layout, table offsets, and indices;
- string-value indices and the distinction between empty text and no value;
- integer-versus-float intent and non-finite 4-byte words;
- element byte offsets and original raw encodings.

A DBX exporter should extend the intermediate representation to preserve raw
encodings, exact scalar types, and unknown nodes. Do not attempt XML-to-DBX
conversion from the current output.

The supplied Frostbite 1 DBX converter reinforces this warning. In synthetic
testing, its own generated XML was silently rejected by its XML-to-DBX path
because of newline normalization. After bypassing only that check, an
unedited `float32` changed value, a very small float field was omitted without
an error, and an ampersand produced invalid XML. This is **Verified
(synthetic)** evidence that the converter is an inspection aid, not a
round-trip implementation.

### Validation scope

The synthetic binary regression verifies the marker/header/string-table path,
one attributed `partition` container, balanced close handling, immutable-tree
construction, and XML parity for that fixture. Most semantic unit tests build
`DbxElement` trees directly so they test field traversal rather than every
binary value encoding. Retail PC integrations exercise real character,
vehicle, animation-tree, mesh-descriptor, and level DBX documents; the terrain
decal inventory also includes Xbox evidence. This is broad semantic evidence,
not full DBX type coverage or a cross-game binary specification.

## External-reference coverage

The audited MaxScripts contain no TerrainHeightfield, TerrainMaterialMap, or
binary DBX parser. Their bone tools consume an already generated text dump and
search for labels such as `LinearTransform`; that confirms only human-readable
field naming in the dump, not DBX byte layout, numeric-array encoding, or level
graph semantics. No terrain or DBX behavior changed from this evidence. See
the [external MaxScript audit](../maxscript-reference-audit.md).

The external DBX converter understands similar string-table, LEB128, and
numeric-array concepts, but rebuilds scalar types and strings heuristically.
Its failures and the raw-preserving adaptation requirements are recorded in
the [external writer audit](../external-writer-reference-audit.md).

See [`dbx.py`](../../bc2_frostbite_importer/core/dbx.py) and
[Levels and complete maps](levels-and-maps.md).
