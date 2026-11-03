# Xbox 360 platform

## Supported source containers

Two read-only Xbox source paths are implemented:

- **BC2 retail XGD2** images through XDVDFS.
- **Xbox LIVE/PIRS STFS** packages. The verified sample is the extensionless
  Battlefield 1943 Xbox LIVE Arcade package with title ID `0x5841097E`.

Both paths expose bounded file views and converge at the same FbRB parser.
Either source may also be the single identifiable member of a ZIP archive.
Console-signed `CON ` packages, secured-volume/SVOD packages, and fragmented
STFS files are rejected explicitly.

## ZIP source wrapper

**Implemented:** a ZIP is an outer transport only. The importer identifies
exactly one source member, writes that member once to the source-fingerprinted
cache, and then runs the unchanged XDVDFS, STFS, or ISO9660 detection against
the cached file. This full-member materialization is required because the
inner filesystems perform bounded random reads; seeking repeatedly through a
compressed `ZipExtFile` would restart decompression and is not a practical
disc-access path.

Members ending in `.iso` and members beginning with `LIVE`, `PIRS`, or `CON `
are identifiable candidates. If none match, accepting the only member at
least 1 MiB in size, or the only regular member, is an **implemented
heuristic**; inner source detection must still succeed. Multiple candidates
are rejected rather than guessed.

The implementation:

- accepts stored and compressed methods supported by Python's `zipfile`;
- rejects encrypted members, nested/multiple source choices, more than
  100,000 entries, and source members larger than 16 GiB;
- checks available cache space against the declared uncompressed size;
- copies in 4 MiB chunks, reports byte progress, honors cancellation between
  chunks, verifies the ZIP CRC while reading, and removes partial temporary
  files after failure or cancellation;
- writes to a generated cache name rather than the member path, so ZIP path
  traversal names cannot select the destination;
- keys the reusable copy by the outer source fingerprint plus member name,
  CRC, and uncompressed size.

The original ZIP remains read-only. The cache must have room for the complete
uncompressed ISO or STFS package. Synthetic end-to-end tests cover a deflated
XGD2 image, a deflated extensionless STFS package, cache reuse, and ambiguous
member rejection. No real-media ZIP is claimed as validation evidence.

See
[`zip_source.py`](../../bc2_frostbite_importer/core/zip_source.py).

## XGD2 and source detection

The tested Xbox 360 image is 7,838,695,424 bytes and contains both an ISO9660
video partition and an XDVDFS game partition. A generic ISO9660 probe can
therefore misclassify it.

Detection order matters:

1. Check common XDVDFS descriptor offsets.
2. Only then accept a PC ISO9660 image with `AUTORUN.DAT` and cabinets.
3. If neither fast probe succeeds, scan the first 512 MiB for the XDVDFS magic.

The tested image has:

- XDVDFS descriptor at `0x0FDA0000`
- game partition base at `0x0FD90000`
- descriptor delta from partition base of `0x10000`

Other known descriptor offsets are also probed; do not hard-code only the
tested value.

## XDVDFS volume descriptor

All fields described here are little-endian.

| Descriptor offset | Size | Meaning |
|---:|---:|---|
| `0x00` | 20 | `MICROSOFT*XBOX*MEDIA` |
| `0x14` | 4 | root-directory sector |
| `0x18` | 4 | root-directory byte size |
| final 20 bytes | 20 | repeated volume magic |

File and directory sectors are relative to the game partition base, not the
start of the full image:

```text
absolute_offset = partition_offset + sector * 2048
```

## Directory tree nodes

An XDVDFS directory is a binary search tree stored in one directory extent.
Child pointers are measured in four-byte units from the start of that extent.

| Node offset | Type | Meaning |
|---:|---|---|
| `0x00` | `uint16` | left child offset / 4, zero if absent |
| `0x02` | `uint16` | right child offset / 4, zero if absent |
| `0x04` | `uint32` | starting sector |
| `0x08` | `uint32` | file size or child-directory extent size |
| `0x0C` | `uint8` | attributes; bit `0x10` marks a directory |
| `0x0D` | `uint8` | name length |
| `0x0E` | bytes | Latin-1 name |

Robust readers should reject cycles, pointers outside the directory extent,
zero-length names, and file ranges outside the image.

See [`xdvdfs.py`](../../bc2_frostbite_importer/core/xdvdfs.py).

## Xbox LIVE STFS packages

### Detection and metadata

**Implemented:** source detection reads the four-byte package magic at
structure-relative offset `0x00`. `LIVE` and `PIRS` packages use the STFS
reader. A `CON ` magic is recognized so it can produce an explicit unsupported
error rather than being misclassified as an ISO.

The metadata fields used by the reader are big-endian unless the table says
otherwise:

| Package offset | Size | Byte order | Meaning |
|---:|---:|---|---|
| `0x000` | 4 | ASCII | `LIVE`, `PIRS`, or `CON ` |
| `0x340` | 4 | big | unaligned header size |
| `0x344` | 4 | big | content type |
| `0x348` | 4 | big | metadata version |
| `0x34C` | 8 | big | physical content size |
| `0x360` | 4 | big | title ID |
| `0x379` | `0x24` | mixed; see below | STFS volume descriptor |
| `0x3A9` | 4 | big | descriptor type; `0` is STFS, `1` is SVOD |

The logical-data base is the header size rounded up to a `0x1000` boundary.
The verified BF1943 package is 586,448,896 bytes, declares header size
`0xAD0E`, and therefore starts its hash/data region at `0xB000`. Its content
type is `0x000D0000` (Xbox LIVE Arcade), metadata version is `2`, and
descriptor type is `0`.

### STFS volume descriptor

The descriptor begins at package offset `0x379`:

| Descriptor offset | Size | Byte order | Meaning |
|---:|---:|---|---|
| `0x00` | 1 | - | descriptor size; must be `0x24` |
| `0x01` | 1 | - | reserved |
| `0x02` | 1 | - | block-separation flag |
| `0x03` | 2 | little | file-table block count |
| `0x05` | 3 | little | first file-table logical block |
| `0x08` | 20 | - | top hash-table SHA-1 |
| `0x1C` | 4 | big | allocated logical block count |
| `0x20` | 4 | big | unallocated logical block count |

The BF1943 sample has two file-table blocks beginning at logical block zero
and 142,321 allocated blocks.

### Logical-to-physical block mapping

STFS logical blocks are `0x1000` bytes. Hash tables are inserted into the
physical package, so a logically consecutive file is not necessarily one
physical byte range. For the implemented LIVE/PIRS layout, logical block `b`
maps to physical block index:

```text
p = b + floor((b + 0xAA) / 0xAA)
if b > 0xAA:
    p += floor((b + 0x70E4) / 0x70E4)
if b > 0x70E4:
    p += floor((b + 0x4AF768) / 0x4AF768)

absolute_offset = align_up(header_size, 0x1000) + p * 0x1000
```

The first hash block is at the aligned data base; logical block zero follows
it. The importer applies this mapping on every bounded read, including reads
that cross level-zero and higher hash-table boundaries. It does not extract an
intermediate package filesystem.

### File table

Each file-table block contains 64 fixed `0x40`-byte entries. Entries end at
the first all-zero record.

| Entry offset | Size | Byte order | Meaning |
|---:|---:|---|---|
| `0x00` | `0x28` | ASCII | NUL-padded leaf name |
| `0x28` | 1 | - | low six bits: name length; bit `0x40`: consecutive blocks; bit `0x80`: directory |
| `0x29` | 3 | little | allocated block count |
| `0x2C` | 3 | little | duplicate allocated block count |
| `0x2F` | 3 | little | first logical block |
| `0x32` | 2 | big | parent entry index; `0xFFFF` means root |
| `0x34` | 4 | big | file size in bytes |
| `0x38` | 4 | big | FAT update timestamp |
| `0x3C` | 4 | big | FAT access timestamp |

The reader validates duplicate block counts, parent directories, cycles,
duplicate paths, allocations, names, and package bounds. Multi-block files
without the consecutive flag require an allocation-chain implementation and
currently fail explicitly.

See [`stfs.py`](../../bc2_frostbite_importer/core/stfs.py).

## Archive access

Xbox FbRB files are directly addressable in both inner source containers.
XDVDFS catalog locators store an absolute image offset and size. STFS locators
store the first logical block, size, and consecutive-block flag. Direct
sources require no intermediate extraction. A ZIP source first materializes
its one complete inner source file, but individual FbRB archives are still
addressed in place rather than extracted for indexing.

The tested BC2 disc contains:

- 630 FbRB archives
- 384,241 metadata records including deleted records
- 251,251 active catalog entries

The full-disc scan indexes every archive. A corrupt archive records an error
without discarding successfully indexed archives.

### Verified BF1943 package

The read-only BF1943 package scan found:

- 114 STFS entries: 90 files and 24 directories;
- 85 FbRB archives, all consecutive in logical STFS space;
- 29,850 FbRB metadata records, all active;
- 81 archives with gzip-compressed payloads;
- zero STFS, FbRB, or catalog failures.

Package-wide resource validation produced:

| Resource evidence | Result |
|---|---:|
| MeshData | 1,209 of 1,209 parsed |
| Decoded mesh vertices | 2,492,266 |
| Nondegenerate mesh triangles | 1,851,115 |
| XenonTexture | 1,505 of 1,510 converted |
| TerrainHeightfield | 5 of 5 sampled map resources parsed |
| TerrainMaterialMap | 5 of 5 parsed |
| WaterMesh | 5 of 5 parsed |
| Binary DBX | 10 cross-archive samples parsed |
| Complete level graphs | 5 of 5 resolved |
| GrannyModel | 31 of 31 parsed |
| GrannyAnimation | 787 of 787 parsed |

The five texture exceptions use format code `5` and remain explicit raw-only
resources. These observations are verified on one BF1943 Xbox LIVE package;
PIRS parsing is implemented but has no game-package sample in this project.

### BF1943 complete maps

The five discovered maps are `CORAL_SEA`, `GUADAL_CANAL`, `IWO_JIMA_S`,
`WAKE_ISLAND_S`, and `WAKE_ISLAND_TUTORIAL`. Each stages:

```text
Dist/xenon/levels/<level>/reduced-level-00.fbrb
Dist/xenon/levels/<level>/terrain-00.fbrb
Dist/xenon/levels/levels-common-00.fbrb
```

All five root `GameSharedResources.LevelData` graphs resolve through the
shared semantic map path. Their placement counts are 2,889, 5,265, 3,672,
4,328, and 4,419 respectively. Coral Sea and the 13-tile Iwo Jima map both
pass Blender construction and save/reopen checks with reduced-resolution
terrain previews.

Each map contains `Levels/<Name>/Shaders/Single.res` as a
`Dx9ShaderDatabase`. **Implemented for the verified BF1943 package:** all five
level databases and both UI databases use a version-117 stream. Structural
counts, sizes, indices, program records, name/group maps, and compiled-shader
sizes are big-endian. Parameter-block binding values and the internal
89-byte vertex-layout blob retain mixed/little-endian fields. The file header
is a big-endian `uint32` version at structure-relative offset `0x00`, a
one-byte zero flag at `0x04`, and the first unaligned big-endian section count
at `0x05`.

All seven databases parse to exact EOF with every cross-reference in range.
Complete-map import uses their verified entry names and texture paths to
create source-backed materials. Compiled Xenos shader arithmetic, reflected
parameter names/dimensions, and physical UV routing remain unsupported, so
each map records `unsupported_xbox_shader_bytecode` and uses documented
Blender role/UV heuristics rather than claiming original-renderer parity.
See [Shaders and materials](../shaders-and-materials.md#bf1943-xbox-shader-database)
for the eight sections, field widths, database counts, and material coverage.
Xbox BC2 XGD2 complete maps remain unverified and disabled.

### BF1943 Granny

The BF1943 Granny wrapper and container are big-endian version 10/version 7.
The Granny file uses this distinct 32-byte magic:

```text
b5 95 11 0e 4b b5 a5 6a 50 28 28 eb 04 b3 78 25
00 00 01 c8 00 00 00 00 00 00 00 00 00 00 00 00
```

Observed files have eight uncompressed sections and 32-bit big-endian
relocations. Skeleton bone count/pointer fields are at section-0 offsets
168/172, followed by the same 152-byte records used by the PC implementation.
Animation wrapper records contain a bone index, a component count, then
component-count lower values and component-count upper values. Counts 1, 2,
and 6 are observed; the runtime meaning of the retained ranges is unknown.

All 31 GrannyModel and 787 GrannyAnimation catalog records parse. This includes
uniformly sampled `DaKeyframes32f` rotation, translation, and scale/shear
curves whose knots are derived from the animation time step. A representative
nine-bone, 61-sample stationary-HMG Action resolves its referenced skeleton,
matches source pose matrices, and survives Blender save/reopen. These results
do not establish Xbox BC2 or PS3 Granny compatibility. See
[Granny characters and animation](../formats/granny-animation.md).

STFS RSA signatures and SHA-1 hash tables are not authenticated by the
importer. Structural bounds and downstream FbRB/resource validation detect
many corruptions, but they are not a substitute for package authenticity
verification.

## Xbox resource conventions

Observed BC2 conventions, with BF1943 differences called out:

- MeshData and WaterMesh numeric fields are big-endian.
- BF1943 MeshData additionally uses primitive type `5` triangle strips,
  32-bit index buffers selected by payload flag `1`, and full-float skinned
  declarations whose palette indices and weights follow the 12-byte position.
- Xenon texture headers are big-endian.
- Texture payloads use Xbox 360 tiled addressing.
- BC/DXT blocks require a 16-bit byte swap after untile.
- Some resources declare many mips but store one padded allocation in the
  first size entry; see [Textures](../formats/textures.md).
- The tested BF1943 2048 x 2048 terrain header and its `uint16` height samples
  beginning at byte `0x31` are both big-endian. Little-endian interpretation
  produces severe adjacent-height discontinuities. Xbox BC2 height-sample
  order remains unverified.
- BF1943 Granny wrappers, container headers, section descriptors, relocation
  records, skeletons, and animation curve records are big-endian. Its file
  magic and skeleton file-info offsets differ from PC.
- BF1943 version-117 shader database stream structure is big-endian, but the
  four-byte parameter binding value and fields inside the raw 89-byte vertex
  layout record remain mixed/little-endian. Byte order must be selected per
  field rather than applied to the whole resource.

## Export implications

A correct Xbox texture writer must reverse every read-side transform:

1. Encode pixels or BC blocks.
2. Apply the console block byte order.
3. Tile each surface using Xbox 360 addressing and allocation alignment.
4. Reproduce the resource's mip-allocation convention.

FbRB and XDVDFS writing remain separate problems. A mod-folder exporter is
safer than editing an XGD2 image and avoids filesystem allocation, signing, and
console deployment concerns.
