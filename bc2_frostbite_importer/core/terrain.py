# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import CorruptDataError

if TYPE_CHECKING:
    import numpy as np

try:
    import numpy as _np
except ImportError:
    _np = None


@dataclass(frozen=True)
class TerrainMesh:
    bounds: tuple[float, float, float, float, float, float]
    source_resolution: int
    sample_endian: str
    positions: tuple[tuple[float, float, float], ...] | np.ndarray
    faces: tuple[tuple[int, int, int], ...] | np.ndarray
    header_endian: str = ">"


@dataclass(frozen=True)
class TerrainMaterialMap:
    side: int
    names: tuple[str, ...]
    indices: bytes
    endian: str


_HEIGHTFIELD_HEADER_SIZE = struct.calcsize("<6f6IB")


def _header(
    data: bytes, platform: str | None
) -> tuple[str, tuple[float, ...], int]:
    preferred = (
        "<" if platform == "pc"
        else ">" if platform in {"xbox360", "ps3"}
        else None
    )
    candidates = []
    for endian in ("<", ">"):
        bounds = struct.unpack_from(endian + "6f", data, 0)
        resolution = struct.unpack_from(endian + "I", data, 24)[0]
        sample_end = (
            _HEIGHTFIELD_HEADER_SIZE + resolution * resolution * 2
        )
        finite = all(math.isfinite(value) for value in bounds)
        ordered = all(bounds[index] <= bounds[index + 3] for index in range(3))
        valid = (
            finite
            and ordered
            and 2 <= resolution <= 8192
            and sample_end <= len(data)
        )
        if valid:
            score = int(endian == preferred) * 1_000_000
            score -= len(data) - sample_end
            candidates.append((score, endian, bounds, resolution))
    if not candidates:
        raise CorruptDataError(
            "TerrainHeightfield header has no valid byte order"
        )
    candidates.sort(reverse=True)
    _, endian, bounds, resolution = candidates[0]
    return endian, bounds, resolution


def parse_heightfield(
    data: bytes,
    target_resolution: int = 256,
    platform: str | None = None,
) -> TerrainMesh:
    if len(data) < _HEIGHTFIELD_HEADER_SIZE + 4:
        raise CorruptDataError("TerrainHeightfield is too short")
    header_endian, bounds, resolution = _header(data, platform)
    sample_offset = _HEIGHTFIELD_HEADER_SIZE
    sample_size = resolution * resolution * 2
    if sample_offset + sample_size > len(data):
        raise CorruptDataError(
            f"Terrain height samples are truncated for {resolution}x{resolution}"
        )
    endian = ">" if platform == "xbox360" else "<"
    target_resolution = max(2, min(int(target_resolution), 1024))
    sample_count = min(resolution, target_resolution)
    samples = [
        round(index * (resolution - 1) / (sample_count - 1))
        for index in range(sample_count)
    ]
    min_x, min_y, min_z, max_x, max_y, max_z = bounds
    x_scale = (max_x - min_x) / resolution
    z_scale = (max_z - min_z) / resolution
    height_scale = (max_y - min_y) / 65535.0
    if _np is not None:
        sample_indices = _np.asarray(samples, dtype=_np.intp)
        heights = _np.frombuffer(
            data,
            dtype=_np.dtype(endian + "u2"),
            count=resolution * resolution,
            offset=sample_offset,
        ).reshape(resolution, resolution)
        selected_heights = heights[
            _np.ix_(sample_indices, sample_indices)
        ].astype(_np.float32)
        selected_heights *= height_scale
        selected_heights += min_y
        x_values = min_x + sample_indices.astype(_np.float32) * x_scale
        z_values = min_z + sample_indices.astype(_np.float32) * z_scale
        x_grid, z_grid = _np.meshgrid(x_values, z_values)
        positions = _np.column_stack(
            (
                x_grid.reshape(-1),
                z_grid.reshape(-1),
                selected_heights.reshape(-1),
            )
        ).astype(_np.float32, copy=False)
        width = len(samples)
        top_left = _np.arange(
            (width - 1) * (width - 1),
            dtype=_np.int32,
        )
        top_left += top_left // (width - 1)
        faces = _np.empty(
            ((width - 1) * (width - 1) * 2, 3),
            dtype=_np.int32,
        )
        faces[0::2, 0] = top_left
        faces[0::2, 1] = top_left + width
        faces[0::2, 2] = top_left + width + 1
        faces[1::2, 0] = top_left
        faces[1::2, 1] = top_left + width + 1
        faces[1::2, 2] = top_left + 1
        return TerrainMesh(
            bounds=tuple(float(value) for value in bounds),
            source_resolution=resolution,
            sample_endian=endian,
            positions=positions,
            faces=faces,
            header_endian=header_endian,
        )

    positions = []
    for row in samples:
        for column in samples:
            sample_index = row * resolution + column
            raw_height = struct.unpack_from(
                endian + "H", data, sample_offset + sample_index * 2
            )[0]
            positions.append(
                (
                    min_x + column * x_scale,
                    min_z + row * z_scale,
                    min_y + raw_height * height_scale,
                )
            )
    width = len(samples)
    faces = []
    for row in range(width - 1):
        for column in range(width - 1):
            top_left = row * width + column
            top_right = top_left + 1
            bottom_left = top_left + width
            bottom_right = bottom_left + 1
            faces.append((top_left, bottom_left, bottom_right))
            faces.append((top_left, bottom_right, top_right))
    return TerrainMesh(
        bounds=tuple(float(value) for value in bounds),
        source_resolution=resolution,
        sample_endian=endian,
        positions=tuple(positions),
        faces=tuple(faces),
        header_endian=header_endian,
    )


def _material_map_header(
    data: bytes,
    platform: str | None,
) -> tuple[str, int, int]:
    preferred = (
        "<" if platform == "pc"
        else ">" if platform in {"xbox360", "ps3"}
        else None
    )
    candidates = []
    for endian in ("<", ">"):
        if len(data) < 10:
            continue
        side = struct.unpack_from(endian + "I", data, 0)[0]
        tile_count = struct.unpack_from(endian + "H", data, 4)[0]
        across = side // 512
        down = side // 256
        valid = (
            512 <= side <= 16384
            and side % 512 == 0
            and tile_count == across * down
        )
        if valid:
            candidates.append(
                (int(endian == preferred), endian, side, tile_count)
            )
    if not candidates:
        raise CorruptDataError(
            "TerrainMaterialMap header has no valid byte order"
        )
    candidates.sort(reverse=True)
    _, endian, side, tile_count = candidates[0]
    return endian, side, tile_count


def _decode_material_row(
    data: bytes,
    tile_index: int,
) -> bytes:
    decoded = bytearray()
    position = 0
    while position < len(data):
        value = data[position]
        if position + 1 < len(data) and data[position + 1] == value:
            if position + 2 >= len(data):
                raise CorruptDataError(
                    f"TerrainMaterialMap tile {tile_index} row ends in a run"
                )
            decoded.extend(bytes((value,)) * (data[position + 2] + 1))
            position += 3
        else:
            decoded.append(value)
            position += 1
    if len(decoded) != 256:
        raise CorruptDataError(
            f"TerrainMaterialMap tile {tile_index} row decodes to "
            f"{len(decoded)} bytes, expected 256"
        )
    return bytes(decoded)


def parse_material_map(
    data: bytes,
    platform: str | None = None,
) -> TerrainMaterialMap:
    endian, side, tile_count = _material_map_header(data, platform)
    across = side // 512
    position = 6
    if _np is not None:
        grid = _np.zeros((side, side), dtype=_np.uint8)
    else:
        grid = bytearray(side * side)

    for tile_index in range(tile_count):
        if position + 4 > len(data):
            raise CorruptDataError(
                f"TerrainMaterialMap ends before tile {tile_index}"
            )
        body_size = struct.unpack_from(endian + "I", data, position)[0]
        body_start = position + 4
        lengths_start = body_start + body_size
        lengths_end = lengths_start + 512
        if lengths_end > len(data):
            raise CorruptDataError(
                f"TerrainMaterialMap tile {tile_index} is truncated"
            )
        body = data[body_start:lengths_start]
        row_lengths = struct.unpack_from(
            endian + "256H",
            data,
            lengths_start,
        )
        if sum(row_lengths) != body_size:
            raise CorruptDataError(
                f"TerrainMaterialMap tile {tile_index} row lengths do not "
                "match its body"
            )
        tile_y, tile_x = divmod(tile_index, across)
        body_position = 0
        for row_index, row_length in enumerate(row_lengths):
            packed = _decode_material_row(
                body[body_position:body_position + row_length],
                tile_index,
            )
            body_position += row_length
            destination_y = tile_y * 256 + row_index
            destination_x = tile_x * 512
            if _np is not None:
                values = _np.frombuffer(packed, dtype=_np.uint8)
                target = grid[
                    destination_y,
                    destination_x:destination_x + 512,
                ]
                target[0::2] = values & 0x0F
                target[1::2] = values >> 4
            else:
                destination = destination_y * side + destination_x
                for packed_index, value in enumerate(packed):
                    grid[destination + packed_index * 2] = value & 0x0F
                    grid[destination + packed_index * 2 + 1] = value >> 4
        position = lengths_end

    if position + 4 > len(data):
        raise CorruptDataError(
            "TerrainMaterialMap ends before its material-name table"
        )
    names_size = struct.unpack_from(endian + "I", data, position)[0]
    names_start = position + 4
    names_end = names_start + names_size
    if names_end != len(data):
        raise CorruptDataError(
            "TerrainMaterialMap material-name table has an invalid size"
        )
    name_data = data[names_start:names_end]
    if name_data and not name_data.endswith(b"\0"):
        raise CorruptDataError(
            "TerrainMaterialMap material-name table is not NUL terminated"
        )
    try:
        names = tuple(
            value.decode("ascii")
            for value in name_data.split(b"\0")[:-1]
        )
    except UnicodeDecodeError as exc:
        raise CorruptDataError(
            "TerrainMaterialMap contains a non-ASCII material name"
        ) from exc
    indices = grid.tobytes() if _np is not None else bytes(grid)
    used_index = max(indices, default=-1)
    if used_index >= len(names):
        raise CorruptDataError(
            f"TerrainMaterialMap uses material {used_index}, but only "
            f"{len(names)} names are present"
        )
    return TerrainMaterialMap(
        side=side,
        names=names,
        indices=indices,
        endian=endian,
    )
