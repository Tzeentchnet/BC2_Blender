# Import and export design

## Recommended importer layers

Keep the layers independent so each can be tested without Blender:

1. **Disc reader**: ISO9660 or XDVDFS, returning bounded file ranges.
2. **Platform resolver**: direct Xbox archive locators or PC MSI/CAB mapping.
3. **FbRB reader**: archive metadata, active entries, and payload extraction.
4. **Catalog**: persistent paths, types, offsets, platform, and errors.
5. **Resource decoder**: mesh, texture, terrain, Granny skeleton/animation,
   typed DBX, shader database, level graph, and future types.
6. **DCC adapter**: axes, units, UV convention, linked instances, materials,
   diagnostics, and native objects.

The current repository follows this design through
[`core/`](../bc2_frostbite_importer/core/) and
[`blender_addon.py`](../bc2_frostbite_importer/blender_addon.py).

## Defensive parsing checklist

- Open disc files and archives through bounded readers.
- Validate magic, counts, dimensions, offsets, and multiplication results
  before allocation.
- Put explicit limits on decompression and in-memory resource reads.
- Use platform metadata for byte order and report ambiguous recovery.
- Reject unsupported primitive and texture formats explicitly.
- Write cache outputs to a temporary name and replace atomically.
- Record per-archive errors while retaining usable scan results.
- Keep original asset path, archive path, and platform on imported objects.
- Preserve level blueprint, shader-set, enumeration, and source transform
  metadata on map instances.

Never return a success-shaped placeholder for a failed native conversion.
Unsupported resources should be extracted raw and identified as unsupported.

## Optional acceleration boundary

NumPy acceleration is confined to computations where output can be compared
exactly with a pure-Python path: Blender bulk array transfer, Xbox untile
addressing, and block byte swaps. NumPy is not required to scan discs, index
archives, or decode supported resources.

Blender images use native file-backed DDS loading. Converting compressed
textures to Python float pixels would increase memory use and duplicate
Blender's native decoder. See [Import performance](import-performance.md).

## Round-trip metadata

A future exporter should attach a sidecar to every imported resource containing
at least:

- source platform and game/build identity;
- archive path, resource path, type, flags, and both size fields;
- byte order and original header bytes;
- unknown metadata blocks and padding;
- original vertex declaration, its raw shader-database record and GUID,
  attribute encodings, and trailing stride padding;
- subset order, shared/local index convention, and the four count-prefixed
  MeshData byte arrays;
- texture header, mip allocations, tiling mode, and channel semantics;
- Granny wrapper strings, section/relocation tables, source bone transforms,
  inverse bind matrices, curve codecs, knots, controls, and original timing;
- DBX typed nodes and raw encodings;
- direct/group placement order, enumerations, and source transforms;
- shader/database references and material constants;
- hashes of the original resource and decoded representation.

Blender geometry, images, and XML views alone are not lossless source models.

## Suggested exporter phases

### Phase 1: mod workspace

Write edited resources to a separate directory with a manifest mapping original
asset paths to replacements. Integrate with an existing mod loader where
possible. Do not alter source media.

### Phase 2: constrained resource writers

Implement one known layout at a time:

- unchanged-layout MeshData vertex/index replacement;
- PC texture replacement preserving the original header and mip structure;
- typed DBX edits that retain untouched nodes byte-for-byte.

Require a target platform and reject edits that exceed established constraints.

### Phase 3: FbRB rebuilding

Only after unknown fields and alignment are understood:

- preserve entry order, flags, alternate sizes, and string-table behavior;
- regenerate offsets from the decompressed payload;
- support both raw and gzip payload modes;
- verify archives with an independent reader and the game/mod loader.

### Phase 4: platform packaging

PC cabinet/MSI and Xbox XDVDFS/XGD2 rewriting are separate, high-risk projects.
They should not be prerequisites for useful mod output.

## Validation gates for writers

For every supported resource variant:

1. Decode original.
2. Encode without semantic edits.
3. Require byte-identical output, or document every intentional byte
   difference.
4. Decode the result independently.
5. Compare geometry, indices, UVs, weights, mip pixels/blocks, and references.
6. Test in a disposable mod workspace.

Cross-platform visual similarity is useful evidence but does not prove that
container bytes or platform allocations are interchangeable.

## External writer evidence

Supplied Frostbite 1 writer and conversion sources were reviewed as research
inputs for this design. They are not distributed with this repository, and no
export behavior was implemented from them.

Claims in this section use the project confidence labels with an evidence
qualifier:

- **Verified (synthetic)**: exercised with generated, non-game fixtures and
  checked with this repository's independent readers.
- **Verified (PC retail)**: checked read-only against a bounded Bad Company 2
  PC retail sample.
- **Heuristic/source-reviewed**: visible in external source, but not validated
  as a game-accepted BC2 invariant.
- **Unknown**: not established well enough for writing.

No generated resource or archive has been accepted by Bad Company 2. Game or
mod-loader acceptance therefore remains **Unknown**.

### Applicability boundary

| Area | Evidence | Adaptation decision |
|---|---|---|
| FbRB rebuilding | **Verified (synthetic)** output is readable in raw- and gzip-payload modes | Reuse the two-stream data flow, not reconstructed metadata |
| Binary DBX writing | **Verified (synthetic)** conversion loses information and fails an unedited round trip | Use typed, raw-preserving edits instead of XML reconstruction |
| PC rigid MeshData | **Heuristic/source-reviewed**, with selected structures **Verified (PC retail)** | Use only as evidence for a constrained original-layout writer |
| PC composite and tree resources | Part records and hashes have partial **Verified (PC retail)** corroboration | Preserve original mesh-set data; do not regenerate it generically |
| Skinned resources | Writer branches are visibly incomplete | Do not adapt them as an exporter |
| Xbox 360 output | No endian-aware native writer was found | Unsupported by this evidence |
| Blender conversion | An unrelated BF2 exporter demonstrates robust loop/corner handling | Adapt conversion safeguards, never its Refractor binary layout |

Cross-version import-only references reinforce preservation requirements but
provide no BC2 writer, no no-edit round trip, and no transferable numeric
offsets. Versioned structure layouts, hard-coded bone counts, DCC scale/axis
choices, and filename-based materials are not encoder specifications. Reuse
only declaration, palette, loop/corner, and validation principles that are
independently established by BC2 evidence.

### FbRB rebuilding evidence

The reviewed Frostbite 1 packer writes the documented big-endian FbRB outer
header and metadata records, followed by either a raw payload or one gzip
stream. It reconstructs rather than preserves the archive:

- metadata word zero is hard-coded to `2`;
- strings and entries follow filesystem traversal order;
- every nonempty file receives flag `0x10000`, while empty files become
  deleted records;
- `alternate_size` is replaced with `size`;
- resources are packed consecutively without retained padding;
- resource types come from a filename-extension dictionary, so unknown
  extensions are omitted;
- gzip output has no fixed timestamp.

A synthetic archive started with metadata word `7`, flag `0x1234`, payload
offset `4`, size `5`, alternate size `9`, and four bytes of padding. The
repacked payload remained readable in raw and gzip modes, but the no-edit path
changed these fields:

| Field | Original | Repacked |
|---|---:|---:|
| Metadata word zero | `7` | `2` |
| Entry flag | `0x1234` | `0x10000` |
| Payload-relative offset | `4` | `0` |
| Alternate size | `9` | `5` |

A zero-byte input became a deleted entry, and a file with an unrecognized
extension disappeared. These are **Verified (synthetic)** loss modes.

One read-only **Verified (PC retail)** survey of the `MP_001` level archive
found 5,977 records: 2,188 active and 3,789 deleted. That archive used
metadata word `2`, one gzip payload, flag `0x10000` and equal size fields for
every active record, contiguous active payload ranges, and zero-byte deleted
records. This agreement does not validate changed ordering, rebuilt strings,
gzip bytes, other archives, other builds, or Xbox 360 output.

A safe builder must preserve original record and string order, flags, both size
fields, deleted records, unknown metadata, and padding. It should regenerate
only the payload-relative offsets required by an edited decompressed stream,
use deterministic compression, and write to a separate mod workspace. See
[FbRB archives](formats/fbrb.md).

### DBX writing evidence

The reviewed converter recognizes the same broad string-table, LEB128, and
numeric-array concepts as the current reader, but its XML representation is
not lossless:

- integer-versus-float decisions depend on field names and bit-pattern
  heuristics;
- floats use six-significant-digit formatting;
- string order and indices are rebuilt from XML encounter order;
- XML syntax is mapped back to binary types through field-name lists;
- unknown type bytes and original header words are not preserved;
- XML names, attributes, and values are emitted without escaping.

Synthetic checks established three concrete failures:

1. XML emitted by the converter was rejected on the return path because
   text-mode newline normalization broke an exact header comparison.
2. After bypassing only that check, an unedited binary `float32` value of
   `1.2345677614212036` returned as `1.234570026397705`.
3. A `1e-40` numeric field was omitted without an error, while a string
   containing `&` produced invalid XML.

These are **Verified (synthetic)** round-trip blockers. A DBX writer should
edit a typed binary tree while retaining the original raw encoding of every
untouched header, type, value, string, and ordering decision. XML remains an
inspection view, not export source. See
[Terrain and DBX](formats/terrain-and-dbx.md).

### PC mesh writer evidence

The reviewed native source generates PC-style MeshData with a seven-byte
header, NUL-terminated subset names, 22-byte subset records, 16-bit subset
palettes, four count-prefixed byte arrays, little-endian vertices, and
`uint16` triangle indices.

A **Verified (PC retail)** sample of 60 eligible `MP_001` resources placed all
four arrays immediately before the geometry-size triplet, and every array byte
was below the subset count. This verifies the sampled structure, not the
arrays' runtime meaning.

The mesh-set writer emits rigid, composite, and tree variants, LOD shader
paths, bounds, byte sizes, composite names, part hashes, 18-float
bounds/transforms, and per-subset unknown blocks. It also creates only one
shader set per LOD, replaces part rotations with identity, retains only FBX
translation, and regenerates unknown blocks mostly as zeroes.

Its part hash is a 32-bit DJB2-style XOR over a lower-cased part name. The word
before all 48 decoded `lod0|PartName` records in five sampled PC composite
resources matched that routine. The sample contained no uppercase `Z`, and
the reference lower-cases `A` through `Y` but not `Z`, so the exact case rule
remains **Unknown**.

#### Shader declaration records

The writer selects declarations by GUID from 89-byte PC DX11 shader-database
records. In two supplied PC shader databases, every surveyed mesh-reference
GUID matched one of those records: 50 of 50 in the smaller sample and 6,611 of
6,611 in the merged sample.

The merged sample contained 120 records. Of 61 records tagged with generic
declaration value `0xFFFFFF00`, 50 used only element codes recognized by the
external writer and all 50 stored the next element's byte offset in the second
`uint16` of each four-byte declaration word. Only 24 of those layouts filled
the declared stride exactly; 11 generic declarations included unrecognized
codes. Trailing stride bytes and unknown declarations must therefore be
preserved rather than regenerated from the known-code list.

The external encoder uses these source-reviewed interpretations:

| Code | External interpretation | Encoded width |
|---:|---|---:|
| `263` | Half-float position XYZ | 6 |
| `259` | Float32 position XYZ | 12 |
| `1032` | Half-float normal XYZW | 8 |
| `1288` | Half-float tangent XYZW | 8 |
| `1797` | Half-float constant | 2 |
| `6` | Half-float coordinate pair | 4 |
| `524` | Four byte indices | 4 |
| `780` | Four byte weights | 4 |
| `264` | Half-float position XYZW | 8 |
| `8` | Eight-byte null/default field | 8 |
| `4` | Unknown 16-byte field | 16 |
| `12` | Unknown four-byte field | 4 |

These codes are PC shader-database evidence. The external interpretations are
not complete semantic names or an exporter-safe declaration specification.

#### Native writer blockers

The native writer is not a safe basis for unrestricted Blender export:

- output is host-little-endian with no platform byte-order selection;
- skinned generation is empty and weight encoding is marked unsupported;
- `uint16` indices are emitted without enforcing the 65,535 limit;
- FBX control points become source vertices while UVs and normals come from the
  first matching polygon corner, collapsing seams and split normals;
- triangle counts assume triangulated input without checking it;
- tangent `W` and another sign/default field are forced to `+1`;
- unknown declaration fields are zero-filled;
- composite rotations are replaced with identity;
- subset order, shared/local index conventions, hidden data, padding, and raw
  declaration bytes are not round-tripped;
- fixed buffers and weak bounds checks do not meet this project's defensive
  parsing requirements.

No no-edit byte comparison or in-game validation accompanied that writer. See
[Mesh formats](formats/meshes.md).

### DCC conversion requirements

An unrelated BF2 Blender exporter demonstrates useful conversion safeguards,
although its Refractor 2 binary layout does not apply to Frostbite:

- triangulate or reject non-triangles explicitly;
- expand Blender loop/corner data into unique output vertices when UVs,
  normals, tangents, materials, or weights differ;
- flip UV V exactly once and update tangent handedness consistently;
- validate influence counts, normalization, palette membership, index limits,
  and other format bounds before writing;
- fail explicitly instead of producing partially valid output.

The reviewed outward OBJ and GLB converters do not write Frostbite resources.
They omit some subsets, use heuristic declarations and texture names, and
discard unknown attributes during DCC conversion.

### Evidence-backed implementation order

If exporter work resumes, use this order within the broader phases above:

1. Retain raw 89-byte PC declaration records, reference GUIDs, the four
   count-prefixed MeshData arrays, original ordering, padding, and unknown
   bytes.
2. Implement an original-layout, PC-only rigid MeshData no-edit round trip.
3. Require byte identity before enabling constrained position, index, or
   selected-coordinate edits.
4. Add loop-to-vertex expansion, winding, tangent-sign, quantization, and
   16-bit-limit validation at the Blender boundary.
5. Preserve composite mesh-set records byte-for-byte before allowing part
   edits; do not regenerate rotations or unknown blocks.
6. Add a metadata-preserving FbRB builder that targets a separate mod
   workspace.
7. Keep DBX, skinned meshes, Xbox resources, cabinets, MSI data, and disc
   images as separate later phases.

Every generated resource still requires independent decode, no-edit
comparison, and game or mod-loader validation before it can be described as
supported.

### Provenance constraint

No license or notice file accompanied the reviewed Frostbite archive, DBX,
native mesh, OBJ, or GLB sources. Their behavior may guide an independent
implementation, but source should not be copied without confirming provenance
and license terms. The separate BF2 Blender add-on includes an MIT license,
but its binary formats are not transferable to Frostbite.

## Coordinate and material cautions

- Preserve source-space data separately from Blender's Z-up representation.
- Track whether UV V was flipped.
- Do not bake shader UV scaling into mesh UVs unless that is an explicit export
  choice.
- Preserve compressed normal channels; reconstructed Z may be derived data.
- Keep Z-only subsets and unrendered attributes even when they are hidden in
  the DCC.
