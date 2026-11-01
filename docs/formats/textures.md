# Texture formats

The importer converts supported Frostbite texture resources to DDS. Storage
decoding and material interpretation are separate: the resource header tells
the importer how to obtain pixels or blocks, while a shader determines color
space, channel meaning, UV transforms, and blending.

## Header variants

Two layouts are implemented.

### Bare 92-byte header

| Offset | Type | Meaning |
|---:|---|---|
| `0x08` | `uint32` | format code |
| `0x10` | `uint32` | width |
| `0x14` | `uint32` | height |
| `0x1C` | `uint32` | mip count |
| `0x20` | `uint32[]` | mip allocation sizes within the header |
| `0x5C` | bytes | payload |

### `RES`-wrapped 156-byte header

The wrapped form begins with `RES`.

| Offset | Type | Meaning |
|---:|---|---|
| `0x48` | `uint32` | format code |
| `0x50` | `uint32` | width |
| `0x54` | `uint32` | height |
| `0x5C` | `uint32` | mip count |
| `0x60` | `uint32[]` | mip allocation sizes within the header |
| `0x9C` | bytes | payload |

PC data normally prefers little-endian fields. Xbox 360 and PS3 conventions
prefer big-endian fields. Header detection validates format, dimensions, and
mip count, but a known platform should always be supplied.

## Implemented format codes

| Codes | Output | Block dimensions | Bytes per block/pixel |
|---|---|---:|---:|
| `0`, `18` | DXT1 / BC1 | 4 x 4 | 8 |
| `1` | DXT3 / BC2 | 4 x 4 | 16 |
| `2`, `13`, `19`, `20` | DXT5 / BC3 | 4 x 4 | 16 |
| `9` | ARGB8888 | 1 x 1 | 4 |
| `10` | L8 | 1 x 1 | 1 |

The DDS writer emits a standard 128-byte little-endian header. L8 uses an
8-bit luminance mask. ARGB8888 uses explicit channel masks.

## Xbox 360 tiled surfaces

Xbox payloads are not row-major. The implementation uses the Xenos tiled
address calculation to map each source block/pixel to linear `(x, y)`.
Allocation dimensions are aligned to 32 blocks in each direction.

For compressed formats:

1. Work in 4 x 4 BC blocks.
2. Untile using the block byte size as pitch.
3. Swap every 16-bit pair after untile.
4. Append only the logical block rectangle to the DDS.

For uncompressed formats, work in pixels and do not apply the compressed block
swap.

When NumPy is available, untile calculations run in bounded vectorized chunks
and compressed block swaps use array operations. Small resources and
environments without NumPy use the equivalent pure-Python implementation. See
[Import performance](../import-performance.md).

See [`texture.py`](../../bc2_frostbite_importer/core/texture.py).

## Mip allocation behavior

The normal case has one declared allocation size per mip. Dimensions halve at
each level but never fall below one.

Some tested Xbox resources instead declare many mips while only
`mip_sizes[0]` is nonzero. That first value describes a shared padded
allocation rather than a standalone logical base mip. Examples included:

- 512 x 512 DXT1 with 10 declared mips
- 256 x 256 DXT5 with grouped allocation

The current importer safely decodes the padded base tiled surface and emits a
one-mip DDS, allowing Blender to generate display mips. It does not claim to
recover the lower stored mips from that allocation.

This distinction matters to an exporter: do not rewrite such a resource as
ordinary consecutive mips without proving the console allocation layout.

## Material semantics

Do not infer any of the following from `DXT5` alone:

- sRGB versus non-color data;
- alpha transparency;
- normal-map channels;
- channel packing;
- UV repetition;
- normal strength or blend method.

The validated MedKit detail normal is DXT5nm-style data with X in alpha and Y
in green. Its correct interpretation comes from the compiled shader, not from
the texture header. See [Shaders and materials](../shaders-and-materials.md).

## Blender image loading

After storage conversion, the importer writes a DDS cache file atomically and
loads it as a native file-backed Blender Image. It deliberately avoids
expanding pixels into Python RGBA arrays. Reimporting the same cache path lets
Blender reuse the existing image datablock.

Images are not packed by default. Deleting the cache therefore requires
reimporting the source texture unless the user has packed the image into a
saved `.blend`.

## External-reference coverage

The audited BF3/BF4 mesh scripts only locate already converted PNG or DDS files
by `_d`, `_s`, and `_n` filename suffixes. They do not parse Frostbite texture
headers, mip allocations, BC formats, Xbox tiling, console block byte order,
channel packing, or color space. They provide no storage-format evidence for
this decoder. See the
[external MaxScript audit](../maxscript-reference-audit.md).

## Writer requirements

PC texture writing needs a validated header template, correct mip sizes, and
the encoded DDS-equivalent payload without a DDS header.

Xbox writing additionally requires:

- inverse 16-bit block byte order for compressed data;
- inverse Xenos tiling;
- correct padded surface allocation;
- preservation of grouped-mip conventions;
- platform-endian headers.

Keep original header bytes and allocation metadata in an export sidecar until
every unknown field is understood. A converted DDS alone does not contain
enough information for a lossless XenonTexture round trip.
