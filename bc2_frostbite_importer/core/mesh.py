# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from .errors import CorruptDataError


@dataclass(frozen=True)
class MeshSubset:
    name: str
    positions: tuple[tuple[float, float, float], ...]
    faces: tuple[tuple[int, int, int], ...]
    uvs: tuple[tuple[float, float], ...]
    influences: tuple[tuple[tuple[int, float], ...], ...]
    vertex_stride: int
    secondary_uvs: tuple[tuple[float, float], ...] = ()
    uv_offsets: tuple[int, ...] = ()


@dataclass(frozen=True)
class MeshAsset:
    mesh_type: int
    endian: str
    subsets: tuple[MeshSubset, ...]


@dataclass(frozen=True)
class _SubsetHeader:
    name: str
    primitive_count: int
    vertex_count: int
    index_offset: int
    vertex_offset: int
    stride: int
    primitive_type: int
    bones_per_vertex: int
    palette: tuple[int, ...]


def _detect_endian(data: bytes, platform: str | None) -> str:
    if platform == "pc":
        return "<"
    if platform in {"xbox360", "ps3"}:
        return ">"
    candidates = []
    for endian in ("<", ">"):
        mesh_type, _, subset_count = struct.unpack_from(endian + "IHB", data, 0)
        score = int(mesh_type <= 16) + int(0 < subset_count <= 128)
        candidates.append((score, endian))
    candidates.sort(reverse=True)
    if candidates[0][0] < 2 or candidates[0][0] == candidates[1][0]:
        raise CorruptDataError("Unable to determine MeshData byte order")
    return candidates[0][1]


def _parse_headers(data: bytes, endian: str):
    if len(data) < 32:
        raise CorruptDataError("MeshData is too short")
    mesh_type, _, subset_count = struct.unpack_from(endian + "IHB", data, 0)
    if not 0 < subset_count <= 128:
        raise CorruptDataError(f"Invalid MeshData subset count: {subset_count}")
    position = 7
    headers: list[_SubsetHeader] = []
    for _ in range(subset_count):
        name_end = data.find(b"\0", position, min(len(data), position + 256))
        if name_end < 0:
            raise CorruptDataError("Unterminated MeshData subset name")
        name = data[position:name_end].decode("latin-1").replace("|", "_")
        position = name_end + 1
        if position + 22 > len(data):
            raise CorruptDataError(f"Truncated MeshData subset header: {name}")
        values = struct.unpack_from(endian + "IIIIBBBBH", data, position)
        position += 22
        (
            primitive_count,
            vertex_count,
            index_offset,
            vertex_offset,
            stride,
            primitive_type,
            bones_per_vertex,
            palette_count,
            palette_count_2,
        ) = values
        if palette_count != palette_count_2:
            raise CorruptDataError(f"MeshData palette counts disagree for {name}")
        if stride < 6 or stride > 128:
            raise CorruptDataError(f"Invalid vertex stride {stride} for {name}")
        if primitive_type != 3:
            raise CorruptDataError(
                f"Unsupported primitive type {primitive_type} for {name}"
            )
        palette_size = palette_count * 2
        if position + palette_size > len(data):
            raise CorruptDataError(f"Truncated MeshData bone palette: {name}")
        palette = (
            struct.unpack_from(endian + f"{palette_count}H", data, position)
            if palette_count
            else ()
        )
        position += palette_size
        headers.append(
            _SubsetHeader(
                name=name,
                primitive_count=primitive_count,
                vertex_count=vertex_count,
                index_offset=index_offset,
                vertex_offset=vertex_offset,
                stride=stride,
                primitive_type=primitive_type,
                bones_per_vertex=bones_per_vertex,
                palette=tuple(palette),
            )
        )
    return mesh_type, headers, position


def _find_payload(data: bytes, endian: str, search_start: int) -> tuple[int, int, int]:
    search_end = min(len(data) - 12, max(search_start + 8192, 1 << 20))
    search_end = min(search_end, len(data) - 12)
    for offset in range(search_start, search_end + 1):
        vertex_size, index_size, reserved = struct.unpack_from(
            endian + "III", data, offset
        )
        if (
            reserved == 0
            and vertex_size > 0
            and index_size > 0
            and offset + 12 + vertex_size + index_size == len(data)
        ):
            return offset + 12, vertex_size, index_size
    raise CorruptDataError("MeshData geometry payload could not be located")


def _position_score(values: list[tuple[float, float, float]]) -> float:
    if not values or any(not math.isfinite(value) for row in values for value in row):
        return -math.inf
    spans = [
        max(row[axis] for row in values) - min(row[axis] for row in values)
        for axis in range(3)
    ]
    maximum = max(abs(value) for row in values for value in row)
    if maximum > 1_000_000:
        return -math.inf
    varying = sum(span > 1e-5 for span in spans)
    unique = sum(
        len({round(row[axis], 7) for row in values}) > 2 for axis in range(3)
    )
    score = varying * 10.0 + unique * 4.0
    if 1e-4 < maximum < 100_000:
        score += 3.0
    if max(spans) < 1e-7:
        score -= 20.0
    if maximum > 4_096:
        score -= 80.0
    elif maximum > 1_024:
        score -= 30.0
    elif maximum > 256:
        score -= 5.0
    positive_spans = sorted(span for span in spans if span > 1e-7)
    if (
        len(positive_spans) >= 2
        and positive_spans[-1] > positive_spans[-2] * 10_000
    ):
        score -= 40.0
    return score


def _position_format(
    data: bytes,
    regions: list[tuple[int, int, int]],
    endian: str,
    requested: str,
) -> str:
    if requested in {"HALF", "FLOAT"}:
        return requested
    formats = [("HALF", "eee", 6)]
    if all(stride >= 12 for _, _, stride in regions):
        formats.append(("FLOAT", "fff", 12))
    scored = []
    for name, fmt, width in formats:
        values = []
        for start, count, stride in regions:
            sample_count = min(count, 128)
            if sample_count == count:
                sample_indices = range(count)
            elif sample_count <= 1:
                sample_indices = (0,)
            else:
                sample_indices = (
                    round(index * (count - 1) / (sample_count - 1))
                    for index in range(sample_count)
                )
            values.extend(
                struct.unpack_from(
                    endian + fmt,
                    data,
                    start + index * stride,
                )
                for index in sample_indices
                if start + index * stride + width <= len(data)
            )
        scored.append((_position_score(values), name))
    scored.sort(reverse=True)
    if not scored or not math.isfinite(scored[0][0]):
        raise CorruptDataError("MeshData vertex positions are not finite")
    return scored[0][1]


def _decode_half_pair(
    data: bytes, offset: int, endian: str
) -> tuple[float, float] | None:
    try:
        u, v = struct.unpack_from(endian + "ee", data, offset)
    except (struct.error, OverflowError):
        return None
    if not math.isfinite(u) or not math.isfinite(v):
        return None
    return float(u), float(v)


def _detect_uv_offset(
    data: bytes, start: int, count: int, stride: int, endian: str
) -> int | None:
    preferred_by_stride = {
        20: 16,
        24: 16,
        28: 20,
        32: 24,
        36: 24,
        40: 24,
        44: 24,
        48: 24,
        52: 24,
        64: 24,
        68: 24,
        72: 24,
        76: 24,
    }
    sample_count = min(count, 64)
    best: tuple[float, int] | None = None
    for offset in range(16, stride - 3, 2):
        values = []
        for index in range(sample_count):
            value = _decode_half_pair(
                data, start + index * stride + offset, endian
            )
            if value is None or abs(value[0]) > 64 or abs(value[1]) > 64:
                values = []
                break
            values.append(value)
        if not values:
            continue
        u_span = max(value[0] for value in values) - min(value[0] for value in values)
        v_span = max(value[1] for value in values) - min(value[1] for value in values)
        if u_span + v_span < 1e-5:
            continue
        score = 0.0
        if u_span > 1e-4 and v_span > 1e-4:
            score += 8.0
        score += sum(
            -2.0 <= u <= 3.0 and -2.0 <= v <= 3.0 for u, v in values
        ) / len(values) * 4.0
        if preferred_by_stride.get(stride) == offset:
            score += 12.0
        if offset % 4 == 0:
            score += 1.0
        candidate = (score, -offset)
        if best is None or candidate > (best[0], -best[1]):
            best = (score, offset)
    return best[1] if best else None


def _decode_uv_channel(
    data: bytes,
    start: int,
    count: int,
    stride: int,
    offset: int,
    endian: str,
) -> tuple[tuple[float, float], ...] | None:
    values = []
    for vertex in range(count):
        value = _decode_half_pair(
            data,
            start + vertex * stride + offset,
            endian,
        )
        if value is None or abs(value[0]) > 64 or abs(value[1]) > 64:
            return None
        values.append(value)
    if not values:
        return None
    u_span = max(value[0] for value in values) - min(
        value[0] for value in values
    )
    v_span = max(value[1] for value in values) - min(
        value[1] for value in values
    )
    if u_span + v_span < 1e-5:
        return None
    return tuple(values)


def _read_influences(
    data: bytes,
    vertex_offset: int,
    vertex_count: int,
    stride: int,
    bones_per_vertex: int,
    palette: tuple[int, ...],
) -> tuple[tuple[tuple[int, float], ...], ...]:
    if not palette:
        return tuple(() for _ in range(vertex_count))
    if stride < 16:
        return tuple(((palette[0], 1.0),) for _ in range(vertex_count))
    result = []
    for index in range(vertex_count):
        start = vertex_offset + index * stride
        indices = data[start + 8 : start + 12]
        weights = data[start + 12 : start + 16]
        if bones_per_vertex <= 1:
            palette_index = indices[0]
            if palette_index >= len(palette):
                raise CorruptDataError(
                    f"MeshData palette index {palette_index} exceeds "
                    f"{len(palette)} entries at vertex {index}"
                )
            bone = palette[palette_index]
            result.append(((bone, 1.0),))
            continue
        total = sum(weights)
        influences = []
        if total:
            for palette_index, weight in zip(indices, weights):
                if not weight:
                    continue
                if palette_index >= len(palette):
                    raise CorruptDataError(
                        f"MeshData palette index {palette_index} exceeds "
                        f"{len(palette)} entries at vertex {index}"
                    )
                bone = palette[palette_index]
                influences.append((bone, weight / total))
        if not influences:
            influences.append((palette[0], 1.0))
        result.append(tuple(influences))
    return tuple(result)


def parse_meshdata(
    data: bytes,
    platform: str | None = None,
    include_z_only: bool = False,
    position_format: str = "AUTO",
) -> MeshAsset:
    endian = _detect_endian(data, platform)
    mesh_type, headers, metadata_end = _parse_headers(data, endian)
    vertex_start, vertex_size, index_size = _find_payload(
        data, endian, metadata_end
    )
    index_start = vertex_start + vertex_size
    total_indices = index_size // 2
    indices = struct.unpack_from(endian + f"{total_indices}H", data, index_start)
    active_headers = [
        header
        for header in headers
        if include_z_only or "zonly" not in header.name.casefold()
    ]
    position_formats = {}
    for stride in {header.stride for header in active_headers}:
        position_formats[stride] = _position_format(
            data,
            [
                (
                    vertex_start + header.vertex_offset,
                    header.vertex_count,
                    header.stride,
                )
                for header in active_headers
                if header.stride == stride
            ],
            endian,
            position_format,
        )
    subsets: list[MeshSubset] = []
    for header in active_headers:
        vertex_bytes_end = (
            header.vertex_offset + header.vertex_count * header.stride
        )
        index_end = header.index_offset + header.primitive_count * 3
        if vertex_bytes_end > vertex_size or index_end > total_indices:
            raise CorruptDataError(
                f"MeshData subset points outside geometry buffers: {header.name}"
            )
        subset_vertex_start = vertex_start + header.vertex_offset
        encoding = position_formats[header.stride]
        position_struct = "fff" if encoding == "FLOAT" else "eee"
        positions = tuple(
            tuple(
                float(value)
                for value in struct.unpack_from(
                    endian + position_struct,
                    data,
                    subset_vertex_start + vertex * header.stride,
                )
            )
            for vertex in range(header.vertex_count)
        )
        uv_offset = _detect_uv_offset(
            data,
            subset_vertex_start,
            header.vertex_count,
            header.stride,
            endian,
        )
        if uv_offset is None:
            uvs = tuple((0.0, 0.0) for _ in range(header.vertex_count))
            secondary_uvs = ()
            uv_offsets = ()
        else:
            decoded_uvs = _decode_uv_channel(
                data,
                subset_vertex_start,
                header.vertex_count,
                header.stride,
                uv_offset,
                endian,
            )
            uvs = decoded_uvs or tuple(
                (0.0, 0.0) for _ in range(header.vertex_count)
            )
            secondary_offset = uv_offset + 4
            secondary_uvs = (
                _decode_uv_channel(
                    data,
                    subset_vertex_start,
                    header.vertex_count,
                    header.stride,
                    secondary_offset,
                    endian,
                )
                if secondary_offset + 4 <= header.stride
                else None
            ) or ()
            uv_offsets = (
                (uv_offset, secondary_offset)
                if secondary_uvs
                else (uv_offset,)
            )
        faces = []
        for face in range(header.primitive_count):
            offset = header.index_offset + face * 3
            triangle = (
                int(indices[offset]),
                int(indices[offset + 1]),
                int(indices[offset + 2]),
            )
            if max(triangle) >= header.vertex_count:
                raise CorruptDataError(
                    f"MeshData index exceeds vertex count in {header.name}"
                )
            faces.append(triangle)
        influences = _read_influences(
            data,
            subset_vertex_start,
            header.vertex_count,
            header.stride,
            header.bones_per_vertex,
            header.palette,
        )
        subsets.append(
            MeshSubset(
                name=header.name,
                positions=positions,
                faces=tuple(faces),
                uvs=uvs,
                influences=influences,
                vertex_stride=header.stride,
                secondary_uvs=secondary_uvs,
                uv_offsets=uv_offsets,
            )
        )
    if not subsets:
        raise CorruptDataError("MeshData contains no importable visual subsets")
    return MeshAsset(mesh_type=mesh_type, endian=endian, subsets=tuple(subsets))


def parse_watermesh(data: bytes, platform: str | None = None) -> MeshAsset:
    endian = ">" if platform in {"xbox360", "ps3"} else "<"
    if len(data) < 12:
        raise CorruptDataError("WaterMesh is too short")
    vertex_count = struct.unpack_from(endian + "I", data, 4)[0]
    vertex_end = 8 + vertex_count * 12
    if vertex_count == 0 or vertex_end + 4 > len(data):
        raise CorruptDataError("WaterMesh vertex buffer is invalid")
    index_count = struct.unpack_from(endian + "I", data, vertex_end)[0]
    index_start = vertex_end + 4
    if index_count % 3 or index_start + index_count * 2 > len(data):
        raise CorruptDataError("WaterMesh index buffer is invalid")
    positions = tuple(
        tuple(
            float(value)
            for value in struct.unpack_from(endian + "3f", data, 8 + index * 12)
        )
        for index in range(vertex_count)
    )
    indices = struct.unpack_from(endian + f"{index_count}H", data, index_start)
    if indices and max(indices) >= vertex_count:
        raise CorruptDataError("WaterMesh index exceeds the vertex count")
    faces = tuple(
        (int(indices[index]), int(indices[index + 1]), int(indices[index + 2]))
        for index in range(0, index_count, 3)
    )
    subset = MeshSubset(
        name="Water",
        positions=positions,
        faces=faces,
        uvs=tuple((0.0, 0.0) for _ in positions),
        influences=tuple(() for _ in positions),
        vertex_stride=12,
    )
    return MeshAsset(mesh_type=3, endian=endian, subsets=(subset,))
