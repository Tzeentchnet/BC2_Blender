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
- original vertex declaration and attribute encodings;
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

## External-reference constraints

The audited BF3/BF4 MaxScripts reinforce several preservation requirements for
a future mesh writer: keep the complete vertex declaration, distinguish
position encodings and coordinate semantics, retain subset-local bone palettes,
and do not collapse type-2 part data into type-1 skin weights.

The importer now decodes type-2 part membership for Blender organization, but
that does not make a safe writer. Export must preserve whether each source
subset used local or shared/global indices, the original aligned part-ID
field and palette, ordered `lodN|PartName` strings, unsplit vertex ordering,
and all still-unknown composite transform metadata. Recombining edited part
objects without those invariants would be lossy.

They do not provide a BC2 writer, a no-edit round trip, or transferable numeric
offsets. Their BF3 and BF4 structure offsets differ, their bone counts and DCC
scale are hard-coded, and their material path is filename-based. Use the
[external MaxScript audit](maxscript-reference-audit.md) as design evidence,
not as an encoder specification.

## Coordinate and material cautions

- Preserve source-space data separately from Blender's Z-up representation.
- Track whether UV V was flipped.
- Do not bake shader UV scaling into mesh UVs unless that is an explicit export
  choice.
- Preserve compressed normal channels; reconstructed Z may be derived data.
- Keep Z-only subsets and unrendered attributes even when they are hidden in
  the DCC.
