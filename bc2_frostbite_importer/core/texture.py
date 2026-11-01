# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import CorruptDataError

try:
    import numpy as _np
except ImportError:
    _np = None


@dataclass(frozen=True)
class TextureAsset:
    width: int
    height: int
    mip_count: int
    format_code: int
    format_name: str
    dds: bytes


_FORMATS = {
    0: ("DXT1", b"DXT1", 4, 8),
    1: ("DXT3", b"DXT3", 4, 16),
    2: ("DXT5", b"DXT5", 4, 16),
    9: ("ARGB8888", None, 1, 4),
    10: ("L8", None, 1, 1),
    13: ("DXT5", b"DXT5", 4, 16),
    18: ("DXT1", b"DXT1", 4, 8),
    19: ("DXT5", b"DXT5", 4, 16),
    20: ("DXT5", b"DXT5", 4, 16),
}

_NUMPY_MIN_BLOCKS = 1024
_NUMPY_MIN_SWAP_BYTES = 4096
_NUMPY_UNTILE_CHUNK_BLOCKS = 1 << 18


def _header_values(data: bytes, endian: str, wrapped: bool):
    if wrapped:
        format_offset, width_offset, height_offset = 72, 80, 84
        mip_offset, sizes_offset, header_size = 92, 96, 156
    else:
        format_offset, width_offset, height_offset = 8, 16, 20
        mip_offset, sizes_offset, header_size = 28, 32, 92
    if len(data) < header_size:
        raise CorruptDataError("Texture resource is shorter than its header")
    format_code = struct.unpack_from(endian + "I", data, format_offset)[0]
    width = struct.unpack_from(endian + "I", data, width_offset)[0]
    height = struct.unpack_from(endian + "I", data, height_offset)[0]
    mip_count = struct.unpack_from(endian + "I", data, mip_offset)[0]
    return format_code, width, height, mip_count, sizes_offset, header_size


def _detect_header(data: bytes, platform: str | None):
    wrapped = data[:3] == b"RES"
    preferred = ">" if platform in {"xbox360", "ps3"} else "<"
    candidates = []
    for endian in (preferred, ">" if preferred == "<" else "<"):
        try:
            values = _header_values(data, endian, wrapped)
        except CorruptDataError:
            continue
        format_code, width, height, mip_count, _, _ = values
        score = (
            int(format_code in _FORMATS)
            + int(0 < width <= 32768)
            + int(0 < height <= 32768)
            + int(0 < mip_count <= 32)
        )
        candidates.append((score, endian, values))
    if not candidates:
        raise CorruptDataError("Texture header is invalid")
    candidates.sort(key=lambda item: (item[0], item[1] == preferred), reverse=True)
    if candidates[0][0] < 4:
        raise CorruptDataError("Texture header contains unsupported dimensions or format")
    return wrapped, candidates[0][1], candidates[0][2]


def _xg_log2_pitch(pitch: int) -> int:
    return (pitch >> 2) + ((pitch >> 1) >> (pitch >> 2))


def _tiled_x(offset: int, width: int, pitch: int) -> int:
    aligned_width = (width + 31) & ~31
    log_bpp = _xg_log2_pitch(pitch)
    offset_b = offset << log_bpp
    offset_t = ((offset_b & ~4095) >> 3) + ((offset_b & 1792) >> 2) + (
        offset_b & 63
    )
    offset_m = offset_t >> (7 + log_bpp)
    macro_x = (offset_m % max(1, aligned_width >> 5)) << 2
    tile = (((offset_t >> (5 + log_bpp)) & 2) + (offset_b >> 6)) & 3
    macro = (macro_x + tile) << 3
    micro = (
        (((offset_t >> 1) & ~15) + (offset_t & 15))
        & ((pitch << 3) - 1)
    ) >> log_bpp
    return macro + micro


def _tiled_y(offset: int, width: int, pitch: int) -> int:
    aligned_width = (width + 31) & ~31
    log_bpp = _xg_log2_pitch(pitch)
    offset_b = offset << log_bpp
    offset_t = ((offset_b & ~4095) >> 3) + ((offset_b & 1792) >> 2) + (
        offset_b & 63
    )
    offset_m = offset_t >> (7 + log_bpp)
    macro_y = (offset_m // max(1, aligned_width >> 5)) << 2
    tile = ((offset_t >> (6 + log_bpp)) & 1) + ((offset_b & 2048) >> 10)
    macro = (macro_y + tile) << 3
    micro = (
        (
            (offset_t & (((pitch << 6) - 1) & ~31))
            + ((offset_t & 15) << 1)
        )
        >> (3 + log_bpp)
    ) & ~1
    return macro + micro + ((offset_t & 16) >> 4)


def _untile_python(
    data: bytes, width_blocks: int, height_blocks: int, pitch: int
) -> bytes:
    result = bytearray(width_blocks * height_blocks * pitch)
    source_blocks = len(data) // pitch
    for source_index in range(source_blocks):
        x = _tiled_x(source_index, width_blocks, pitch)
        y = _tiled_y(source_index, width_blocks, pitch)
        if x >= width_blocks or y >= height_blocks:
            continue
        source_offset = source_index * pitch
        target_offset = (y * width_blocks + x) * pitch
        result[target_offset : target_offset + pitch] = data[
            source_offset : source_offset + pitch
        ]
    return bytes(result)


def _untile_numpy(
    data: bytes, width_blocks: int, height_blocks: int, pitch: int
) -> bytes:
    source_blocks = len(data) // pitch
    source = _np.frombuffer(
        data, dtype=_np.uint8, count=source_blocks * pitch
    ).reshape(-1, pitch)
    result = bytearray(width_blocks * height_blocks * pitch)
    destination = _np.frombuffer(result, dtype=_np.uint8).reshape(-1, pitch)
    aligned_width = (width_blocks + 31) & ~31
    log_bpp = _xg_log2_pitch(pitch)
    clear_low_12 = _np.bitwise_not(_np.uint64(4095))
    clear_low_4 = _np.bitwise_not(_np.uint64(15))
    clear_low_1 = _np.bitwise_not(_np.uint64(1))
    x_mask = _np.uint64((pitch << 3) - 1)
    y_mask = _np.uint64(((pitch << 6) - 1) & ~31)

    for start in range(0, source_blocks, _NUMPY_UNTILE_CHUNK_BLOCKS):
        end = min(start + _NUMPY_UNTILE_CHUNK_BLOCKS, source_blocks)
        offsets = _np.arange(start, end, dtype=_np.uint64)
        offset_b = offsets << log_bpp
        offset_t = (
            ((offset_b & clear_low_12) >> 3)
            + ((offset_b & 1792) >> 2)
            + (offset_b & 63)
        )
        offset_m = offset_t >> (7 + log_bpp)

        macro_x = (
            offset_m % max(1, aligned_width >> 5)
        ) << 2
        tile_x = (
            ((offset_t >> (5 + log_bpp)) & 2) + (offset_b >> 6)
        ) & 3
        micro_x = (
            (((offset_t >> 1) & clear_low_4) + (offset_t & 15)) & x_mask
        ) >> log_bpp
        x = ((macro_x + tile_x) << 3) + micro_x

        macro_y = (
            offset_m // max(1, aligned_width >> 5)
        ) << 2
        tile_y = (
            (offset_t >> (6 + log_bpp)) & 1
        ) + ((offset_b & 2048) >> 10)
        micro_y = (
            (
                (offset_t & y_mask)
                + ((offset_t & 15) << 1)
            )
            >> (3 + log_bpp)
        ) & clear_low_1
        y = (
            ((macro_y + tile_y) << 3)
            + micro_y
            + ((offset_t & 16) >> 4)
        )

        valid = (x < width_blocks) & (y < height_blocks)
        target_indices = (
            y[valid] * width_blocks + x[valid]
        ).astype(_np.intp, copy=False)
        destination[target_indices] = source[start:end][valid]
    return bytes(result)


def _untile(
    data: bytes, width_blocks: int, height_blocks: int, pitch: int
) -> bytes:
    source_blocks = len(data) // pitch
    if _np is not None and source_blocks >= _NUMPY_MIN_BLOCKS:
        return _untile_numpy(data, width_blocks, height_blocks, pitch)
    return _untile_python(data, width_blocks, height_blocks, pitch)


def _swap_16_python(data: bytes) -> bytes:
    swapped = bytearray(data)
    even_length = len(swapped) & ~1
    for offset in range(0, even_length, 2):
        swapped[offset], swapped[offset + 1] = swapped[offset + 1], swapped[offset]
    return bytes(swapped)


def _swap_16_numpy(data: bytes) -> bytes:
    swapped = bytearray(data)
    even_length = len(swapped) & ~1
    pairs = _np.frombuffer(
        swapped, dtype=_np.uint8, count=even_length
    ).reshape(-1, 2)
    first = pairs[:, 0].copy()
    pairs[:, 0] = pairs[:, 1]
    pairs[:, 1] = first
    return bytes(swapped)


def _swap_16(data: bytes) -> bytes:
    if _np is not None and len(data) >= _NUMPY_MIN_SWAP_BYTES:
        return _swap_16_numpy(data)
    return _swap_16_python(data)


def _tiled_surface_size(
    width: int,
    height: int,
    block_size: int,
    pitch: int,
) -> int:
    blocks_wide = max(1, (width + block_size - 1) // block_size)
    blocks_high = max(1, (height + block_size - 1) // block_size)
    aligned_width = (blocks_wide + 31) & ~31
    aligned_height = (blocks_high + 31) & ~31
    return aligned_width * aligned_height * pitch


def _linearize_xbox(
    payload: bytes,
    width: int,
    height: int,
    mip_count: int,
    block_size: int,
    pitch: int,
    mip_sizes: list[int],
    compressed: bool,
) -> bytes:
    output = bytearray()
    position = 0
    for mip in range(mip_count):
        mip_width = max(1, width >> mip)
        mip_height = max(1, height >> mip)
        blocks_wide = max(1, (mip_width + block_size - 1) // block_size)
        blocks_high = max(1, (mip_height + block_size - 1) // block_size)
        logical_size = blocks_wide * blocks_high * pitch
        declared_size = mip_sizes[mip] if mip < len(mip_sizes) else logical_size
        if declared_size <= 0:
            declared_size = logical_size
        if position + declared_size > len(payload):
            if position + logical_size > len(payload):
                raise CorruptDataError("Texture mip payload is truncated")
            declared_size = logical_size
        tiled = payload[position : position + declared_size]
        position += declared_size
        linear = _untile(tiled, blocks_wide, blocks_high, pitch)
        if compressed:
            linear = _swap_16(linear)
        output.extend(linear[:logical_size])
    return bytes(output)


def _dds_header(
    width: int,
    height: int,
    mip_count: int,
    fourcc: bytes | None,
    pitch: int,
    format_name: str,
) -> bytes:
    header = bytearray(128)
    header[:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)
    compressed = fourcc is not None
    flags = 0x1 | 0x2 | 0x4 | 0x1000
    flags |= 0x80000 if compressed else 0x8
    if mip_count > 1:
        flags |= 0x20000
    struct.pack_into("<I", header, 8, flags)
    struct.pack_into("<I", header, 12, height)
    struct.pack_into("<I", header, 16, width)
    linear_size = (
        max(1, (width + 3) // 4) * pitch if compressed else width * pitch
    )
    struct.pack_into("<I", header, 20, linear_size)
    struct.pack_into("<I", header, 28, mip_count)
    struct.pack_into("<I", header, 76, 32)
    if compressed:
        struct.pack_into("<I", header, 80, 0x4)
        header[84:88] = fourcc
    elif format_name == "L8":
        struct.pack_into("<I", header, 80, 0x20000)
        struct.pack_into("<I", header, 88, 8)
        struct.pack_into("<I", header, 92, 0xFF)
    else:
        struct.pack_into("<I", header, 80, 0x41)
        struct.pack_into("<I", header, 88, 32)
        struct.pack_into("<IIII", header, 92, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    caps = 0x1000
    if mip_count > 1:
        caps |= 0x8 | 0x400000
    struct.pack_into("<I", header, 108, caps)
    return bytes(header)


def create_luminance_dds(
    width: int,
    height: int,
    pixels: bytes,
) -> bytes:
    if width <= 0 or height <= 0:
        raise CorruptDataError("Luminance texture dimensions must be positive")
    expected_size = width * height
    if len(pixels) != expected_size:
        raise CorruptDataError(
            f"Luminance texture has {len(pixels):,} bytes, expected "
            f"{expected_size:,}"
        )
    return _dds_header(width, height, 1, None, 1, "L8") + pixels


def convert_texture(data: bytes, platform: str | None = None) -> TextureAsset:
    wrapped, endian, values = _detect_header(data, platform)
    format_code, width, height, mip_count, sizes_offset, header_size = values
    format_name, fourcc, block_size, pitch = _FORMATS[format_code]
    mip_sizes = []
    for mip in range(mip_count):
        offset = sizes_offset + mip * 4
        if offset + 4 > header_size:
            break
        mip_sizes.append(struct.unpack_from(endian + "I", data, offset)[0])
    payload = data[header_size:]
    is_xbox = platform == "xbox360" or (wrapped and endian == ">")
    if is_xbox:
        if (
            mip_count > 1
            and mip_sizes
            and mip_sizes[0] > 0
            and not any(mip_sizes[1:])
        ):
            base_size = min(
                len(payload),
                _tiled_surface_size(
                    width, height, block_size, pitch
                ),
            )
            mip_count = 1
            mip_sizes = [base_size]
        payload = _linearize_xbox(
            payload,
            width,
            height,
            mip_count,
            block_size,
            pitch,
            mip_sizes,
            compressed=fourcc is not None,
        )
    header = _dds_header(width, height, mip_count, fourcc, pitch, format_name)
    return TextureAsset(
        width=width,
        height=height,
        mip_count=mip_count,
        format_code=format_code,
        format_name=format_name,
        dds=header + payload,
    )
