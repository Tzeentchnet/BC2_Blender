# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import math
import struct
from collections.abc import Mapping
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
    additional_uvs: tuple[
        tuple[tuple[float, float], ...], ...
    ] = ()
    palette: tuple[int, ...] = ()
    part_indices: tuple[int, ...] = ()
    part_index_offset: int | None = None
    uv_source: str = "unknown"


@dataclass(frozen=True)
class MeshAsset:
    mesh_type: int
    endian: str
    subsets: tuple[MeshSubset, ...]


@dataclass(frozen=True)
class CompositePart:
    name: str
    bounds: tuple[float, float, float, float, float, float] | None
    transform: tuple[float, ...] | None
    source_offset: int


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
    *,
    require_variation: bool = True,
    max_magnitude: float | None = 64.0,
) -> tuple[tuple[float, float], ...] | None:
    values = []
    for vertex in range(count):
        value = _decode_half_pair(
            data,
            start + vertex * stride + offset,
            endian,
        )
        if value is None or (
            max_magnitude is not None
            and (
                abs(value[0]) > max_magnitude
                or abs(value[1]) > max_magnitude
            )
        ):
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
    if require_variation and u_span + v_span < 1e-5:
        return None
    return tuple(values)


def _pc_skinned_uv_offsets(
    mesh_type: int,
    stride: int,
    palette: tuple[int, ...],
    retain_vehicle_candidates: bool = False,
) -> tuple[int, ...]:
    if mesh_type == 1 and palette:
        if stride == 48:
            return (
                (40, 44, 32, 36)
                if retain_vehicle_candidates
                else (40, 44)
            )
        if stride == 64:
            # Retail PC character declarations expose v6.xy, v3.xy,
            # v6.zw, and v7.xy from these four byte offsets.
            return 40, 16, 44, 48
    return ()


def _pc_stride_48_uv_offsets(
    mesh_type: int,
    stride: int,
    part_index_offset: int | None,
    retain_vehicle_candidates: bool = False,
) -> tuple[int, ...]:
    if stride != 48:
        return ()
    if mesh_type == 2:
        if part_index_offset == 32:
            return 24, 28, 36, 40
        if part_index_offset == 36:
            return 24, 28, 32, 40
        if part_index_offset is None:
            return 28, 36, 32, 24
        return ()
    # The common static declaration exposes object and repeating UVs through
    # the .zw halves at byte 28 and 36. Some vehicle shader layouts declare a
    # fifth coordinate at byte 40.
    return (
        (28, 36, 32, 24, 40)
        if retain_vehicle_candidates and mesh_type == 0
        else (28, 36, 32, 24)
    )


def _uv_triangle_coverage(
    values: tuple[tuple[float, float], ...],
    header: _SubsetHeader,
    indices: tuple[int, ...],
    index_base: int,
) -> tuple[int, int]:
    nondegenerate = 0
    for face in range(header.primitive_count):
        offset = header.index_offset + face * 3
        first, second, third = (
            values[int(indices[offset + corner]) - index_base]
            for corner in range(3)
        )
        area = abs(
            (second[0] - first[0]) * (third[1] - first[1])
            - (second[1] - first[1]) * (third[0] - first[0])
        ) * 0.5
        if area >= 1e-10:
            nondegenerate += 1
    return nondegenerate, header.primitive_count


def _pc_composite_uv_offsets(
    data: bytes,
    vertex_start: int,
    headers: tuple[_SubsetHeader, ...],
    layouts: dict[_SubsetHeader, int],
    indices: tuple[int, ...],
    part_index_offset: int | None,
) -> tuple[int, ...]:
    if not headers:
        return ()
    stride = headers[0].stride
    candidates: dict[int, tuple[float, float]] = {}
    for offset in range(24, stride - 3, 4):
        if offset == part_index_offset:
            continue
        total_triangles = 0
        nondegenerate_triangles = 0
        typical_values = 0
        value_count = 0
        for header in headers:
            index_base = layouts[header]
            subset_start = (
                vertex_start
                + header.vertex_offset
                + index_base * header.stride
            )
            values = _decode_uv_channel(
                data,
                subset_start,
                header.vertex_count,
                header.stride,
                offset,
                "<",
                require_variation=False,
            )
            if values is None:
                break
            nondegenerate, triangle_count = _uv_triangle_coverage(
                values,
                header,
                indices,
                index_base,
            )
            nondegenerate_triangles += nondegenerate
            total_triangles += triangle_count
            typical_values += sum(
                -0.05 <= u <= 1.05 and -0.05 <= v <= 1.05
                for u, v in values
            )
            value_count += len(values)
        else:
            if (
                total_triangles
                and value_count
                and nondegenerate_triangles
            ):
                coverage = nondegenerate_triangles / total_triangles
                typical_fraction = typical_values / value_count
                candidates[offset] = (
                    coverage * 6.0 + typical_fraction * 4.0,
                    coverage,
                )
    if not candidates:
        return ()

    primary = max(
        candidates,
        key=lambda offset: (candidates[offset][0], -offset),
    )
    ordered = [primary]
    for direction in (-4, 4):
        offset = primary + direction
        while 24 <= offset + 3 < stride:
            if offset == part_index_offset:
                offset += direction
                continue
            if offset not in candidates:
                break
            ordered.append(offset)
            offset += direction

    primary_score = candidates[primary][0]
    for offset in sorted(
        candidates,
        key=lambda value: (-candidates[value][0], value),
    ):
        if offset in ordered:
            continue
        if candidates[offset][0] < primary_score * 0.8:
            continue
        ordered.append(offset)
    return tuple(ordered[:6])


def _read_influences(
    data: bytes,
    vertex_offset: int,
    vertex_count: int,
    stride: int,
    bones_per_vertex: int,
    palette: tuple[int, ...],
    mesh_type: int,
) -> tuple[tuple[tuple[int, float], ...], ...]:
    if not palette:
        return tuple(() for _ in range(vertex_count))
    if mesh_type != 1 or bones_per_vertex <= 0 or stride < 16:
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


def _subset_index_base(
    header: _SubsetHeader,
    indices: tuple[int, ...],
) -> int:
    index_end = header.index_offset + header.primitive_count * 3
    if index_end > len(indices):
        raise CorruptDataError(
            f"MeshData subset points outside the index buffer: {header.name}"
        )
    subset_indices = indices[header.index_offset:index_end]
    if not subset_indices:
        return 0
    maximum = max(subset_indices)
    if maximum < header.vertex_count:
        return 0
    base = min(subset_indices)
    if maximum - base >= header.vertex_count:
        raise CorruptDataError(
            f"MeshData index range exceeds vertex count in {header.name}"
        )
    return base


def _read_composite_part_indices(
    data: bytes,
    vertex_offset: int,
    vertex_count: int,
    stride: int,
    palette: tuple[int, ...],
) -> tuple[tuple[int, ...], int | None]:
    if not palette:
        return (), None
    if len(palette) == 1:
        return (palette[0],) * vertex_count, None

    palette_values = set(palette)
    sequences: dict[tuple[int, ...], list[int]] = {}
    for field_offset in range(0, stride - 3, 4):
        values = []
        for vertex in range(vertex_count):
            start = vertex_offset + vertex * stride + field_offset
            field = data[start:start + 4]
            if (
                len(field) != 4
                or not (field[0] == field[1] == field[2] == field[3])
                or field[0] not in palette_values
            ):
                break
            values.append(field[0])
        else:
            sequences.setdefault(tuple(values), []).append(field_offset)

    if not sequences:
        return (), None
    scored = []
    for values, offsets in sequences.items():
        unique = set(values)
        scored.append(
            (
                (len(unique), int(unique == palette_values)),
                values,
                offsets,
            )
        )
    best_score = max(item[0] for item in scored)
    winners = [item for item in scored if item[0] == best_score]
    if len(winners) != 1:
        return (), None
    _, values, offsets = winners[0]
    return values, min(offsets)


def parse_meshdata(
    data: bytes,
    platform: str | None = None,
    include_z_only: bool = False,
    position_format: str = "AUTO",
    retain_pc_vehicle_uv_candidates: bool = False,
    pc_declared_uv_offsets_by_stride: Mapping[
        int,
        tuple[int, ...],
    ]
    | None = None,
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
    layouts = {
        header: _subset_index_base(header, indices)
        for header in active_headers
    }
    composite_part_data = {}
    composite_layout_offsets: dict[int, set[int]] = {}
    if mesh_type == 2:
        for header in active_headers:
            index_base = layouts[header]
            vertex_bytes_end = (
                header.vertex_offset
                + (index_base + header.vertex_count) * header.stride
            )
            if vertex_bytes_end > vertex_size:
                raise CorruptDataError(
                    "MeshData subset points outside geometry buffers: "
                    f"{header.name}"
                )
            subset_vertex_start = (
                vertex_start
                + header.vertex_offset
                + index_base * header.stride
            )
            part_data = _read_composite_part_indices(
                data,
                subset_vertex_start,
                header.vertex_count,
                header.stride,
                header.palette,
            )
            composite_part_data[header] = part_data
            if part_data[1] is not None:
                composite_layout_offsets.setdefault(
                    header.stride,
                    set(),
                ).add(part_data[1])
    declaration_part_offsets = {}
    composite_declaration_headers: dict[
        tuple[int, int | None],
        list[_SubsetHeader],
    ] = {}
    for header in active_headers:
        _, part_index_offset = composite_part_data.get(
            header,
            ((), None),
        )
        declaration_part_offset = part_index_offset
        if declaration_part_offset is None:
            related_offsets = composite_layout_offsets.get(
                header.stride,
                set(),
            )
            if len(related_offsets) == 1:
                declaration_part_offset = next(iter(related_offsets))
        declaration_part_offsets[header] = declaration_part_offset
        if mesh_type == 2 and endian == "<":
            composite_declaration_headers.setdefault(
                (header.stride, declaration_part_offset),
                [],
            ).append(header)
    composite_uv_offsets = {}
    for key, related_headers in composite_declaration_headers.items():
        stride, declaration_part_offset = key
        if _pc_stride_48_uv_offsets(
            mesh_type,
            stride,
            declaration_part_offset,
            retain_pc_vehicle_uv_candidates,
        ):
            continue
        composite_uv_offsets[key] = _pc_composite_uv_offsets(
            data,
            vertex_start,
            tuple(related_headers),
            layouts,
            indices,
            declaration_part_offset,
        )
    position_formats = {}
    for stride in {header.stride for header in active_headers}:
        position_formats[stride] = _position_format(
            data,
            [
                (
                    vertex_start
                    + header.vertex_offset
                    + layouts[header] * header.stride,
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
        index_base = layouts[header]
        vertex_bytes_end = (
            header.vertex_offset
            + (index_base + header.vertex_count) * header.stride
        )
        index_end = header.index_offset + header.primitive_count * 3
        if vertex_bytes_end > vertex_size or index_end > total_indices:
            raise CorruptDataError(
                f"MeshData subset points outside geometry buffers: {header.name}"
            )
        subset_vertex_start = (
            vertex_start
            + header.vertex_offset
            + index_base * header.stride
        )
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
        part_indices, part_index_offset = composite_part_data.get(
            header,
            ((), None),
        )
        declaration_part_offset = declaration_part_offsets.get(header)
        explicit_uv_offsets = ()
        uv_source = "none"
        if endian == "<":
            explicit_uv_offsets = _pc_skinned_uv_offsets(
                mesh_type,
                header.stride,
                header.palette,
                retain_pc_vehicle_uv_candidates,
            )
            if explicit_uv_offsets:
                uv_source = (
                    "pc_vehicle_shader_route_candidates"
                    if retain_pc_vehicle_uv_candidates
                    and mesh_type == 1
                    and header.stride == 48
                    else "verified_pc_skinned_declaration"
                )
        if not explicit_uv_offsets and endian == "<":
            explicit_uv_offsets = _pc_stride_48_uv_offsets(
                mesh_type,
                header.stride,
                declaration_part_offset,
                retain_pc_vehicle_uv_candidates,
            )
            if explicit_uv_offsets:
                uv_source = (
                    "pc_vehicle_shader_route_candidates"
                    if retain_pc_vehicle_uv_candidates
                    and mesh_type == 0
                    else "verified_pc_stride_48_declaration"
                )
        if (
            not explicit_uv_offsets
            and endian == "<"
            and mesh_type == 2
        ):
            explicit_uv_offsets = composite_uv_offsets.get(
                (header.stride, declaration_part_offset),
                (),
            )
            if explicit_uv_offsets:
                uv_source = "topology_fallback"
        decoded_channels = ()
        if explicit_uv_offsets:
            decoded_channels = tuple(
                _decode_uv_channel(
                    data,
                    subset_vertex_start,
                    header.vertex_count,
                    header.stride,
                    offset,
                    endian,
                    require_variation=False,
                    max_magnitude=(
                        None
                        if (
                            mesh_type == 2
                            and declaration_part_offset in {32, 36}
                        )
                        else 64.0
                    ),
                )
                for offset in explicit_uv_offsets
            )
            if any(channel is None for channel in decoded_channels):
                if uv_source == "pc_vehicle_shader_route_candidates":
                    valid = tuple(
                        (offset, channel)
                        for offset, channel in zip(
                            explicit_uv_offsets,
                            decoded_channels,
                        )
                        if channel is not None
                    )
                    explicit_uv_offsets = tuple(
                        offset for offset, _ in valid
                    )
                    decoded_channels = tuple(
                        channel for _, channel in valid
                    )
                elif explicit_uv_offsets == (28, 36, 32, 24):
                    explicit_uv_offsets = ()
                    decoded_channels = ()
                    uv_source = "none"
                else:
                    raise CorruptDataError(
                        "PC declared UV channels are invalid for "
                        f"{header.name}"
                    )
        if explicit_uv_offsets:
            uvs = decoded_channels[0]
            secondary_uvs = (
                decoded_channels[1]
                if len(decoded_channels) > 1
                else ()
            )
            additional_uvs = decoded_channels[2:]
            uv_offsets = explicit_uv_offsets
        if not explicit_uv_offsets:
            uv_offset = _detect_uv_offset(
                data,
                subset_vertex_start,
                header.vertex_count,
                header.stride,
                endian,
            )
        if not explicit_uv_offsets and uv_offset is None:
            uvs = tuple((0.0, 0.0) for _ in range(header.vertex_count))
            secondary_uvs = ()
            additional_uvs = ()
            uv_offsets = ()
            uv_source = "none"
        elif not explicit_uv_offsets:
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
            additional_uvs = ()
            uv_offsets = (
                (uv_offset, secondary_offset)
                if secondary_uvs
                else (uv_offset,)
            )
            uv_source = "stride_heuristic"
        declared_offsets = (
            pc_declared_uv_offsets_by_stride.get(header.stride, ())
            if endian == "<"
            and pc_declared_uv_offsets_by_stride is not None
            else ()
        )
        declared_channels = []
        for offset in declared_offsets:
            if (
                offset in uv_offsets
                or offset == part_index_offset
                or offset < 0
                or offset + 4 > header.stride
            ):
                continue
            channel = _decode_uv_channel(
                data,
                subset_vertex_start,
                header.vertex_count,
                header.stride,
                offset,
                endian,
                require_variation=False,
            )
            if channel is not None:
                declared_channels.append((offset, channel))
        if declared_channels:
            channels = [uvs] if uv_offsets else []
            if len(uv_offsets) > 1:
                channels.append(secondary_uvs)
            channels.extend(additional_uvs)
            channels.extend(
                channel for _, channel in declared_channels
            )
            uv_offsets = uv_offsets + tuple(
                offset for offset, _ in declared_channels
            )
            uvs = channels[0]
            secondary_uvs = channels[1] if len(channels) > 1 else ()
            additional_uvs = tuple(channels[2:])
            uv_source = (
                "shader_layout_candidates"
                if uv_source == "none"
                else uv_source + "+shader_layout_candidates"
            )
        faces = []
        for face in range(header.primitive_count):
            offset = header.index_offset + face * 3
            triangle = (
                int(indices[offset]) - index_base,
                int(indices[offset + 1]) - index_base,
                int(indices[offset + 2]) - index_base,
            )
            if min(triangle) < 0 or max(triangle) >= header.vertex_count:
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
            mesh_type,
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
                additional_uvs=additional_uvs,
                palette=header.palette,
                part_indices=part_indices,
                part_index_offset=part_index_offset,
                uv_source=uv_source,
            )
        )
    if not subsets:
        raise CorruptDataError("MeshData contains no importable visual subsets")
    return MeshAsset(mesh_type=mesh_type, endian=endian, subsets=tuple(subsets))


def split_mesh_subset_by_parts(
    subset: MeshSubset,
) -> tuple[tuple[int, MeshSubset], ...]:
    if not subset.part_indices:
        return ()
    if len(subset.part_indices) != len(subset.positions):
        raise CorruptDataError(
            f"Composite part count does not match vertices in {subset.name}"
        )

    grouped_faces: dict[int, list[tuple[int, int, int]]] = {}
    for face in subset.faces:
        part_values = {subset.part_indices[index] for index in face}
        if len(part_values) != 1:
            raise CorruptDataError(
                f"Composite triangle crosses part boundaries in {subset.name}"
            )
        part_index = next(iter(part_values))
        grouped_faces.setdefault(part_index, []).append(face)

    result = []
    for part_index, faces in grouped_faces.items():
        source_indices = []
        remap = {}
        remapped_faces = []
        for face in faces:
            remapped = []
            for source_index in face:
                target_index = remap.get(source_index)
                if target_index is None:
                    target_index = len(source_indices)
                    remap[source_index] = target_index
                    source_indices.append(source_index)
                remapped.append(target_index)
            remapped_faces.append(tuple(remapped))

        def project(values):
            return (
                tuple(values[index] for index in source_indices)
                if values
                else ()
            )

        result.append(
            (
                part_index,
                MeshSubset(
                    name=subset.name,
                    positions=project(subset.positions),
                    faces=tuple(remapped_faces),
                    uvs=project(subset.uvs),
                    influences=project(subset.influences),
                    vertex_stride=subset.vertex_stride,
                    secondary_uvs=project(subset.secondary_uvs),
                    uv_offsets=subset.uv_offsets,
                    additional_uvs=tuple(
                        project(channel)
                        for channel in subset.additional_uvs
                    ),
                    palette=subset.palette,
                    part_indices=(part_index,) * len(source_indices),
                    part_index_offset=subset.part_index_offset,
                    uv_source=subset.uv_source,
                ),
            )
        )
    return tuple(result)


def _composite_part_payload(
    data: bytes,
    offset: int,
    endian: str,
) -> tuple[
    tuple[float, float, float, float, float, float],
    tuple[float, ...],
] | None:
    if offset + 72 > len(data):
        return None
    values = struct.unpack_from(endian + "18f", data, offset)
    if not all(math.isfinite(value) for value in values):
        return None
    bounds = values[:6]
    if any(bounds[axis] > bounds[axis + 3] for axis in range(3)):
        return None
    transform = values[6:]
    for row in range(3):
        length = math.sqrt(
            sum(transform[row * 3 + column] ** 2 for column in range(3))
        )
        if not 0.01 <= length <= 100.0:
            return None
    if any(abs(value) > 1_000_000 for value in values):
        return None
    return bounds, transform


def parse_composite_lod_parts(
    data: bytes,
    lod_index: int = 0,
    platform: str | None = None,
) -> tuple[CompositePart, ...]:
    marker = f"lod{lod_index}|".encode("ascii")
    lower = data.lower()
    parts = []
    position = 0
    if platform in {"xbox360", "ps3"}:
        endians = (">",)
    elif platform == "pc":
        endians = ("<",)
    else:
        endians = ("<", ">")
    while True:
        position = lower.find(marker, position)
        if position < 0:
            break
        value_start = position + len(marker)
        value_end = data.find(
            b"\0",
            value_start,
            min(len(data), value_start + 256),
        )
        if value_end > value_start:
            raw = data[value_start:value_end]
            if all(32 <= value < 127 for value in raw):
                payload = next(
                    (
                        decoded
                        for endian in endians
                        if (
                            decoded := _composite_part_payload(
                                data,
                                value_end + 1,
                                endian,
                            )
                        )
                        is not None
                    ),
                    None,
                )
                parts.append(
                    CompositePart(
                        name=raw.decode("ascii"),
                        bounds=payload[0] if payload else None,
                        transform=payload[1] if payload else None,
                        source_offset=position,
                    )
                )
        position = value_start
    return tuple(parts)


def parse_composite_lod_part_names(
    data: bytes,
    lod_index: int = 0,
) -> tuple[str, ...]:
    return tuple(
        part.name
        for part in parse_composite_lod_parts(data, lod_index)
    )


def parse_watermesh(data: bytes, platform: str | None = None) -> MeshAsset:
    endian = ">" if platform in {"xbox360", "ps3"} else "<"
    if len(data) < 4:
        raise CorruptDataError("WaterMesh is too short")
    body_count = struct.unpack_from(endian + "I", data, 0)[0]
    if body_count > (len(data) - 4) // 8:
        raise CorruptDataError(
            f"WaterMesh body count is invalid: {body_count}"
        )

    position = 4
    positions = []
    faces = []
    for body_index in range(body_count):
        if position + 4 > len(data):
            raise CorruptDataError(
                f"WaterMesh body {body_index} has no vertex count"
            )
        vertex_count = struct.unpack_from(endian + "I", data, position)[0]
        vertex_start = position + 4
        vertex_end = vertex_start + vertex_count * 12
        if vertex_end + 4 > len(data):
            raise CorruptDataError(
                f"WaterMesh body {body_index} vertex buffer is invalid"
            )
        body_positions = tuple(
            tuple(
                float(value)
                for value in struct.unpack_from(
                    endian + "3f",
                    data,
                    vertex_start + index * 12,
                )
            )
            for index in range(vertex_count)
        )

        index_count = struct.unpack_from(endian + "I", data, vertex_end)[0]
        index_start = vertex_end + 4
        index_end = index_start + index_count * 4
        if index_count % 3 or index_end > len(data):
            raise CorruptDataError(
                f"WaterMesh body {body_index} index buffer is invalid"
            )
        indices = struct.unpack_from(
            endian + f"{index_count}I",
            data,
            index_start,
        )
        if indices and max(indices) >= vertex_count:
            raise CorruptDataError(
                f"WaterMesh body {body_index} index exceeds the vertex count"
            )

        vertex_base = len(positions)
        positions.extend(body_positions)
        faces.extend(
            (
                vertex_base + int(indices[index]),
                vertex_base + int(indices[index + 1]),
                vertex_base + int(indices[index + 2]),
            )
            for index in range(0, index_count, 3)
        )
        position = index_end

    if position != len(data):
        raise CorruptDataError(
            f"WaterMesh has {len(data) - position} trailing byte(s)"
        )

    positions_tuple = tuple(positions)
    subset = MeshSubset(
        name="Water",
        positions=positions_tuple,
        faces=tuple(faces),
        uvs=tuple((0.0, 0.0) for _ in positions_tuple),
        influences=tuple(() for _ in positions_tuple),
        vertex_stride=12,
    )
    return MeshAsset(mesh_type=3, endian=endian, subsets=(subset,))
