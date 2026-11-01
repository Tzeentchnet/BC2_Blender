# External Frostbite writer reference audit

## Scope and status

This audit reviews the supplied external `BFTools` writer and conversion
sources for ideas that could support a future Bad Company 2 exporter. The
sources are research inputs and are not distributed with this repository.
No export behavior was implemented as a result of the audit.

Claims use the project confidence labels with an evidence qualifier:

- **Verified (synthetic)**: exercised with generated, non-game fixtures and
  checked with this repository's independent readers.
- **Verified (PC retail)**: checked read-only against a bounded Bad Company 2
  PC retail sample.
- **Heuristic/source-reviewed**: visible in external source, but not validated
  as a game-accepted BC2 invariant.
- **Unknown**: not established well enough for writing.

The separate [MaxScript audit](maxscript-reference-audit.md) remains
authoritative for the eight BF3/BF4 import and bone scripts. Those scripts are
not native BC2 writers.

## Audited sources

| External source | SHA-256 | Observed purpose |
|---|---|---|
| `fbrb.pyw` | `26A274A23744A057277F1BC784EB2D7E466DF138A6ED1E6C8C6DE0D69DECB4AE` | Extracts and rebuilds Frostbite 1 FbRB archives |
| `dbx.pyw` | `3401FF3CB7DC35643AD7C746EE3777911386353EFD029A30C3E03D008F65F96D` | Converts binary DBX to an XML view and XML back to DBX |
| `assetc_meshdata_funcs.cpp` | `F4A4F20697B906B8BDB9920467A0A5FF356512CF492F48514A2112C64AA7A727` | Reads MeshData and generates PC-style MeshData from FBX |
| `assetc_meshset_funcs.cpp` | `C55AF4B25422B5D8B09051998C81DA367767B6B3E5E9B20AF6DEE38C8F6007C7` | Reads and writes rigid, composite, tree, and partial skinned mesh sets |
| `assetc_vbuf_actuators.cpp` | `BF0950F9B2185770A5F1206999A92773B9EF912B88964E65DCD8FE4FD27EC000` | Encodes and decodes selected vertex element representations |
| `fb_mesh_skeleton_exporter.py` | `AB1FFFD3B4CD245346FD72E92BA3483B4A78AA25AEA1BE5312EEEAFABD658106` | Converts Frostbite meshes, skeletons, and animation outward to GLB |
| `bf_mesh_exporter.py` | `A647C87A60D49648B0F8F7F17B88D89FEE28CF3FED17903A1F43FE97E94FE2B7` | Converts MeshData outward to OBJ |
| BF2 Blender `core/mesh.py` | `B80DC8FA0FFA30A2A0EFC1A2675201B67333D1D6358FF2E1D8D7B92DE4EB0600` | Refractor 2 Blender import/export reference |

The two outward converters do not write Frostbite resources. The BF2 add-on
writes Refractor 2 formats, not Frostbite formats, and is useful only for DCC
conversion patterns.

## Applicability summary

| Area | Evidence | Adaptation decision |
|---|---|---|
| FbRB rebuilding | **Verified (synthetic)** output can be read in raw- and gzip-payload modes | Reuse the high-level two-stream algorithm, not the metadata reconstruction |
| Binary DBX writing | **Verified (synthetic)** converter loses information and has a broken unedited round trip | Do not adapt the XML round-trip; design raw-preserving typed edits |
| PC rigid MeshData | **Heuristic/source-reviewed**, with selected structures **Verified (PC retail)** | Useful design reference for a future constrained original-layout writer |
| PC composite/tree MeshData and mesh sets | Partial **Verified (PC retail)** corroboration for part records and hashes | Preserve original mesh-set data; do not regenerate generically yet |
| Skinned MeshData and mesh sets | Writer paths are visibly incomplete | Do not adapt as an exporter |
| Xbox 360 output | No endian-aware native writer was found | Unsupported by this evidence |
| Blender conversion | BF2 exporter demonstrates robust loop/corner handling | Adapt the architecture, never the Refractor binary layout |
| Game acceptance | No generated resource was validated in BC2 | **Unknown** |

## FbRB packer findings

### Source behavior

The supplied packer writes the documented big-endian FbRB outer header and
metadata records, then appends either a raw payload or one gzip stream. It:

- hard-codes metadata word zero to `2`;
- creates a new string table in filesystem traversal order;
- writes `0x10000` for every nonempty file and zero for every empty file;
- sets `alternate_size` equal to `size`;
- packs resources consecutively without retained padding;
- derives each resource type from a filename-extension dictionary;
- uses gzip without a fixed timestamp.

This is a reconstruction algorithm, not an archive editor.

### Synthetic verification

A generated archive used metadata word `7`, flag `0x1234`, offset `4`,
size `5`, alternate size `9`, and four bytes of payload padding. Repacking
through the external tool produced archives that this repository could read
in both raw- and gzip-payload modes, and the five payload bytes survived.

The same no-edit path changed:

| Field | Original | Repacked |
|---|---:|---:|
| Metadata word zero | `7` | `2` |
| Entry flag | `0x1234` | `0x10000` |
| Payload-relative offset | `4` | `0` |
| Alternate size | `9` | `5` |

A zero-byte input became a deleted entry, and a file whose extension was not
in the dictionary was omitted. These are **Verified (synthetic)** loss modes.

### PC retail context

A read-only survey of the PC `MP_001` level archive found:

- 5,977 metadata records: 2,188 active and 3,789 deleted;
- metadata word zero equal to `2`;
- a gzip-compressed payload;
- flag `0x10000` on every active record;
- `alternate_size == size` on every active record;
- no gaps between sorted active payload ranges;
- zero-byte, `*deleted*` records for every deleted entry;
- no output-name collision or unsupported type in this one archive.

The packer happens to reproduce several values used by that archive, but this
does not validate changed ordering, string-table reconstruction, gzip bytes,
other archives, other builds, or Xbox 360 output. Deleted records and their
positions still need explicit preservation rather than filesystem
reconstruction.

## DBX converter findings

The external converter understands the same broad string-table, LEB128, and
numeric-array concepts as the current reader. Its representation is not
lossless:

- it decides integer versus float from field names and bit-pattern heuristics;
- it formats floats with the default six significant digits of Python `g`;
- it rebuilds string order and indices from XML encounter order;
- it maps XML syntax back to value types using field-name lists;
- it does not preserve unknown type bytes or original header words;
- it emits XML names, attributes, and values without XML escaping.

Synthetic checks established three concrete failures:

1. XML emitted by `toxml` is silently rejected by `todbx` because text-mode
   newline normalization makes its exact header comparison fail.
2. After bypassing only that header check, an unedited binary `float32` value
   `1.2345677614212036` returned as `1.234570026397705`.
3. A `1e-40` numeric field was omitted without an error, and a string
   containing `&` produced invalid XML.

The useful adaptation is a typed binary editor that retains original raw
encodings for every untouched node. XML remains an inspection view, not an
export source.

## Native PC mesh-writer findings

### MeshData and mesh-set structure

The C++ source generates MeshData with:

- a seven-byte header containing mesh type and subset count;
- NUL-terminated subset names and 22-byte subset records;
- 16-bit subset palettes;
- four count-prefixed byte arrays between subset records and geometry sizes;
- little-endian vertex and `uint16` triangle-index buffers.

A read-only check of 60 eligible MeshData resources from the PC `MP_001`
level archive found that all 60 had four count-plus-byte arrays ending exactly
at the geometry-size triplet, with every byte value below the subset count.
This verifies the structure for that sample, not the arrays' runtime meaning.
The external source calls them render order.

The mesh-set writer emits rigid, composite, and tree variants, LOD shader
paths, bounds, mesh byte sizes, composite names, part hashes, 18-float
bounds/transforms, and per-subset unknown blocks. However, it creates only one
shader set per LOD, writes identity part rotation with translation from FBX,
and regenerates unknown per-subset blocks mostly as zeroes with a leading
`0xFFFFFFFF`.

The part hash routine is a 32-bit DJB2-style XOR hash over a lower-cased part
name. On five sampled PC composite resources, the word immediately before
each of 48 decoded `lod0|PartName` records matched that routine. The sample
contained no uppercase `Z`; the reference routine lower-cases `A` through `Y`
but not `Z`, so its exact case rule remains **Unknown**.

### Shader declaration records

The writer selects a vertex declaration by GUID from 89-byte records in the
PC DX11 shader database. Mesh-reference strings carry matching 16-byte GUIDs.
In two supplied PC shader-database samples, every third string-group mesh
reference matched one of those records: 50 of 50 in the small sample and
6,611 of 6,611 in the merged sample.

In the merged sample:

- 120 total declaration records were present;
- 61 used the generic declaration tag `0xFFFFFF00`;
- 50 of those 61 used only element codes recognized by the writer;
- all 50 stored the next element's byte offset in the second `uint16` of each
  four-byte declaration word;
- only 24 of the 50 recognized layouts had element widths that summed to the
  declared stride;
- 11 generic declarations contained element codes unknown to the writer.

The difference between the final element end and stride must be retained as
padding or unknown data. A writer cannot rebuild all BC2 declarations from the
recognized code list.

The external encoder handles these source-reviewed element codes:

| Code | External interpretation | Encoded width |
|---:|---|---:|
| `263` | half-float position XYZ | 6 |
| `259` | float32 position XYZ | 12 |
| `1032` | half-float normal XYZW | 8 |
| `1288` | half-float tangent XYZW | 8 |
| `1797` | half-float constant | 2 |
| `6` | half-float coordinate pair | 4 |
| `524` | four byte indices | 4 |
| `780` | four byte weights | 4 |
| `264` | half-float position XYZW | 8 |
| `8` | eight-byte null/default field | 8 |
| `4` | unknown 16-byte field | 16 |
| `12` | unknown four-byte field | 4 |

These numbers are BC2 PC shader-database evidence, but the external writer's
interpretations are not automatically complete semantic names.

### Writer blockers

The native writer is not a safe basis for unrestricted Blender export:

- output is host-little-endian and has no platform byte-order selection;
- the skinned generation branch is empty, and the weight encoder is marked
  unsupported;
- `uint16` indices are written without enforcing the 65,535 limit;
- FBX control points become source vertices, while UVs and normals use the
  first polygon corner found for that control point; UV seams and split
  normals therefore require explicit loop-to-vertex expansion first;
- polygon count is multiplied by three without a triangulation check;
- tangent `W` and another half-float sign/default field are forced to `+1`;
- unknown 16-byte and four-byte declaration fields are zero-filled;
- composite part rotations are replaced by identity, with only translation
  retained;
- the original subset order, original shared/local index convention, hidden
  data, padding, and raw declaration bytes are not round-tripped;
- fixed-size work buffers and weak bounds checks are not suitable for the
  importer's defensive core.

No no-edit byte comparison or in-game validation accompanied the supplied
writer.

## DCC-side patterns worth adapting

The BF2 Blender exporter targets an unrelated engine, but its conversion
architecture demonstrates safeguards that a future BC2 writer should use:

- triangulate or reject non-triangles explicitly;
- expand Blender loop/corner data into unique output vertices when UVs,
  normals, tangents, material assignments, or weights differ;
- flip UV V exactly once and update tangent handedness consistently;
- validate influence counts, normalization, palette membership, and format
  limits before writing;
- retain explicit errors instead of producing partially valid output.

The outward BC2 OBJ/GLB scripts do not add native writer support. They skip
Z-only subsets by default, use heuristic declarations and texture filenames,
and convert source data into DCC representations that cannot reproduce unknown
attributes.

## Deferred adaptation plan

Exporter implementation is intentionally deferred. If resumed, the evidence
supports this order:

1. Decode and retain raw 89-byte PC declaration records, mesh-reference GUIDs,
   the four count-prefixed MeshData arrays, and all unknown bytes.
2. Add an original-layout, PC-only rigid MeshData no-edit round trip.
3. Require byte identity before enabling constrained position, index, or
   selected-coordinate edits.
4. Add loop-to-vertex expansion, winding, tangent-sign, quantization, and
   16-bit-limit validation at the Blender boundary.
5. Preserve composite mesh-set records byte-for-byte before permitting part
   edits; do not regenerate rotations or unknown blocks from the external
   writer.
6. Implement a metadata-preserving FbRB builder into a separate mod workspace.
7. Treat DBX, skinned meshes, Xbox resources, cabinets, MSI data, and disc
   images as separate later phases.

Game or mod-loader validation remains mandatory before any generated resource
is described as supported.

## Provenance constraint

No license or notice file was found alongside the supplied FbRB, DBX, native
shader/mesh-tool, OBJ, or GLB sources during this review. Their algorithms and
observed behavior may guide an independent implementation, but source should
not be copied without confirming provenance and license terms. The separate
BF2 Blender add-on includes an MIT license, but its binary formats remain
non-transferable to Frostbite.
