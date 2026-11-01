# Xbox 360 platform

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

## Archive access

Xbox FbRB files are directly addressable in XDVDFS. The importer stores their
absolute image offset and size in the catalog and opens them through a bounded
range reader. No intermediate archive extraction is required for indexing.

The tested disc contains:

- 630 FbRB archives
- 384,241 metadata records including deleted records
- 251,251 active catalog entries

The full-disc scan indexes every archive. A corrupt archive records an error
without discarding successfully indexed archives.

## Xbox resource conventions

Observed conventions:

- MeshData and WaterMesh numeric fields are big-endian.
- Xenon texture headers are big-endian.
- Texture payloads use Xbox 360 tiled addressing.
- BC/DXT blocks require a 16-bit byte swap after untile.
- Some resources declare many mips but store one padded allocation in the
  first size entry; see [Textures](../formats/textures.md).
- The tested 2048 x 2048 terrain header is big-endian while its height samples
  are little-endian. Mixed byte order must be handled per field, not per file.

## Export implications

A correct Xbox texture writer must reverse every read-side transform:

1. Encode pixels or BC blocks.
2. Apply the console block byte order.
3. Tile each surface using Xbox 360 addressing and allocation alignment.
4. Reproduce the resource's mip-allocation convention.

FbRB and XDVDFS writing remain separate problems. A mod-folder exporter is
safer than editing an XGD2 image and avoids filesystem allocation, signing, and
console deployment concerns.
