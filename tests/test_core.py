# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import gzip
import struct
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from bc2_frostbite_importer.core import texture
from bc2_frostbite_importer.core.assembly import (
    animation_tree_base_set_references,
    animation_tree_clip_references,
    animation_tree_replacement_references,
    character_animation_links,
    modular_assembly,
    vehicle_entity_path_for_mesh_set,
    vehicle_part_state,
)
from bc2_frostbite_importer.core.catalog import AssetCatalog
from bc2_frostbite_importer.core.dbx import (
    DbxDocument,
    DbxElement,
    dbx_to_xml,
    parse_dbx,
)
from bc2_frostbite_importer.core.errors import (
    CorruptDataError,
    ExtractionError,
    UnsupportedSourceError,
)
from bc2_frostbite_importer.core.fbrb import FbrbEntry
from bc2_frostbite_importer.core.granny import (
    GrannyCurve,
    parse_granny_animation,
    parse_granny_model,
)
from bc2_frostbite_importer.core.level import (
    FrostbiteTransform,
    LevelResolver,
    _unique_virtual_paths,
    discover_levels,
    discover_pc_levels,
    index_level_archives,
    level_archive_paths,
    pc_level_archive_paths,
    xbox360_level_archive_paths,
)
from bc2_frostbite_importer.core.mesh import (
    _position_format,
    parse_composite_lod_part_names,
    parse_composite_lod_parts,
    parse_meshdata,
    parse_watermesh,
    split_mesh_subset_by_parts,
)
from bc2_frostbite_importer.core.records import ArchiveDescriptor
from bc2_frostbite_importer.core.repository import AssetRepository
from bc2_frostbite_importer.core.dxbc import (
    DxbcCoordinateRoute,
    rdef_texture_bindings,
    texture_coordinate_routes,
)
from bc2_frostbite_importer.core.shaderdb import (
    _rdef_texture_parameters,
    _vertex_layout,
    parse_shader_database,
)
from bc2_frostbite_importer.core.stfs import StfsPackage
from bc2_frostbite_importer.core.terrain import (
    parse_heightfield,
    parse_material_map,
)
from bc2_frostbite_importer.core.texture import (
    _tiled_x,
    _tiled_y,
    convert_texture,
    create_luminance_dds,
)


def make_meshdata(endian: str = ">") -> bytes:
    metadata = bytearray(struct.pack(endian + "IHB", 1, 0, 1))
    metadata.extend(b"triangle\0")
    metadata.extend(struct.pack(endian + "IIIIBBBBH", 1, 3, 0, 0, 12, 3, 0, 0, 0))
    metadata.extend(b"\0" * 4)
    vertices = bytearray()
    for position in ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)):
        vertices.extend(struct.pack(endian + "eee", *position))
        vertices.extend(b"\0" * 6)
    indices = struct.pack(endian + "3H", 0, 1, 2)
    metadata.extend(struct.pack(endian + "III", len(vertices), len(indices), 0))
    return bytes(metadata + vertices + indices)


def make_watermesh(endian: str, bodies) -> bytes:
    data = bytearray(struct.pack(endian + "I", len(bodies)))
    for positions, faces in bodies:
        data.extend(struct.pack(endian + "I", len(positions)))
        for position in positions:
            data.extend(struct.pack(endian + "3f", *position))
        indices = tuple(index for face in faces for index in face)
        data.extend(struct.pack(endian + "I", len(indices)))
        data.extend(struct.pack(endian + f"{len(indices)}I", *indices))
    return bytes(data)


def make_composite_meshdata() -> bytes:
    endian = "<"
    metadata = bytearray(struct.pack(endian + "IHB", 2, 0, 2))
    metadata.extend(b"body\0")
    metadata.extend(
        struct.pack(
            endian + "IIIIBBBBH",
            2,
            6,
            0,
            0,
            32,
            3,
            0,
            2,
            2,
        )
    )
    metadata.extend(struct.pack(endian + "2H", 0, 1))
    metadata.extend(b"door\0")
    metadata.extend(
        struct.pack(
            endian + "IIIIBBBBH",
            1,
            3,
            6,
            0,
            32,
            3,
            0,
            1,
            1,
        )
    )
    metadata.extend(struct.pack(endian + "H", 2))

    vertices = bytearray()
    positions = (
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (2.0, 0.0, 0.0),
        (3.0, 0.0, 0.0),
        (2.0, 1.0, 0.0),
        (4.0, 0.0, 0.0),
        (5.0, 0.0, 0.0),
        (4.0, 1.0, 0.0),
    )
    uvs = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)) * 3
    part_indices = (0, 0, 0, 1, 1, 1, 2, 2, 2)
    for position, uv, part_index in zip(positions, uvs, part_indices):
        vertex = bytearray(32)
        struct.pack_into(endian + "fff", vertex, 0, *position)
        struct.pack_into(endian + "ee", vertex, 24, *uv)
        vertex[28:32] = bytes((part_index,)) * 4
        vertices.extend(vertex)
    indices = struct.pack(endian + "9H", *range(9))
    metadata.extend(
        struct.pack(endian + "III", len(vertices), len(indices), 0)
    )
    return bytes(metadata + vertices + indices)


def make_granny_container(
    section_data: list[bytes],
    section_relocations: list[list[tuple[int, int, int]]],
    *,
    endian: str = "<",
) -> bytes:
    section_count = 8
    if (
        len(section_data) != section_count
        or len(section_relocations) != section_count
    ):
        raise ValueError("Synthetic Granny fixture requires eight sections")
    descriptor_start = 32 + 72
    data_start = descriptor_start + section_count * 44
    granny = bytearray(data_start)
    magic = bytes.fromhex(
        (
            "29de6cc0baa4532b25f5b7a5f666e2"
            "eec80100000000000000000000000000"
        )
        if endian == "<"
        else (
            "b595110e4bb5a56a502828eb04b37825"
            "000001c8000000000000000000000000"
        )
    )
    granny[:32] = magic
    data_offsets = []
    for payload in section_data:
        data_offsets.append(len(granny))
        granny.extend(payload)
    relocation_offsets = []
    for relocations in section_relocations:
        relocation_offsets.append(len(granny))
        for relocation in relocations:
            granny.extend(struct.pack(endian + "3I", *relocation))
    file_size = len(granny)
    struct.pack_into(
        endian + "18I",
        granny,
        32,
        7,
        file_size,
        0,
        72,
        section_count,
        6,
        0,
        0,
        0,
        0x80000028,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    for index, payload in enumerate(section_data):
        size = len(payload)
        struct.pack_into(
            endian + "11I",
            granny,
            descriptor_start + index * 44,
            0,
            data_offsets[index],
            size,
            size,
            4,
            size,
            size,
            relocation_offsets[index],
            len(section_relocations[index]),
            file_size,
            0,
        )
    return bytes(granny)


def granny_wrapper(
    strings: list[str],
    granny: bytes,
    *,
    animation: bool,
    additive_bone_indices: tuple[int, ...] = (),
    xbox_wrapper_records: tuple[
        tuple[int | float, ...],
        ...,
    ] = (),
    endian: str = "<",
) -> bytes:
    if animation:
        if additive_bone_indices and xbox_wrapper_records:
            raise ValueError("Synthetic Granny wrapper modes are exclusive")
        record_count = (
            len(xbox_wrapper_records)
            if xbox_wrapper_records
            else len(additive_bone_indices)
        )
        header = bytearray(
            struct.pack(endian + "II", 10, record_count)
        )
        if xbox_wrapper_records:
            for record in xbox_wrapper_records:
                bone_index, component_count, *values = record
                if len(values) != int(component_count) * 2:
                    raise ValueError(
                        "Synthetic Xbox wrapper bounds are incomplete"
                    )
                header.extend(
                    struct.pack(
                        endian + "II" + "f" * len(values),
                        int(bone_index),
                        int(component_count),
                        *values,
                    )
                )
        elif additive_bone_indices:
            header.extend(
                struct.pack(
                    endian + "I" * len(additive_bone_indices),
                    *additive_bone_indices,
                )
            )
            header.extend(struct.pack(endian + "I", 0))
        header.extend(struct.pack(endian + "I", len(strings)))
    else:
        header = bytearray(struct.pack(endian + "II", 10, len(strings)))
    result = bytearray(header)
    for value in strings:
        encoded = value.encode("latin-1")
        result.extend(struct.pack(endian + "H", len(encoded)))
        result.extend(encoded)
    result.extend(b"\0" * 4)
    result.extend(granny)
    return bytes(result)


def make_granny_model(*, endian: str = "<") -> bytes:
    strings = ["<file>", "root", "child"]
    record_start = 184 if endian == "<" else 180
    count_offset = 172 if endian == "<" else 168
    pointer_offset = 176 if endian == "<" else 172
    section = bytearray(record_start + 2 * 152)
    struct.pack_into(endian + "I", section, count_offset, 2)
    records = (
        (1, -1, (0.0, 0.0, 0.0)),
        (2, 0, (1.0, 0.0, 0.0)),
    )
    identity_3 = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    identity_4 = (
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )
    for index, (name_index, parent, translation) in enumerate(records):
        offset = record_start + index * 152
        struct.pack_into(endian + "IiI", section, offset, name_index, parent, 0)
        struct.pack_into(endian + "3f", section, offset + 12, *translation)
        struct.pack_into(
            endian + "4f",
            section,
            offset + 24,
            0.0,
            0.0,
            0.0,
            1.0,
        )
        struct.pack_into(endian + "9f", section, offset + 40, *identity_3)
        struct.pack_into(endian + "16f", section, offset + 76, *identity_4)
    sections = [bytes(section)] + [b""] * 7
    relocations = [[(pointer_offset, 0, record_start)]] + [
        [] for _ in range(7)
    ]
    return granny_wrapper(
        strings,
        make_granny_container(sections, relocations, endian=endian),
        animation=False,
        endian=endian,
    )


def make_granny_animation(
    additive_bone_indices: tuple[int, ...] = (),
    *,
    endian: str = "<",
    xbox_wrapper_records: tuple[
        tuple[int | float, ...],
        ...,
    ] = (),
) -> bytes:
    strings = [
        "<file>",
        "root",
        "group",
        "CurveDataHeader_D4Constant32f",
        "CurveDataHeader_D3Constant32f",
        "CurveDataHeader_DaIdentity",
    ]
    section = bytearray(548)
    struct.pack_into(endian + "I", section, 76, 1)
    struct.pack_into(endian + "I", section, 204, 2)
    struct.pack_into(endian + "I", section, 216, 1)
    struct.pack_into(endian + "II", section, 340, 1, 0)
    struct.pack_into(
        endian + "BBH4f",
        section,
        400,
        5,
        0,
        0,
        0.0,
        0.0,
        0.0,
        1.0,
    )
    struct.pack_into(
        endian + "BBH3f",
        section,
        424,
        4,
        0,
        0,
        1.0,
        2.0,
        3.0,
    )
    struct.pack_into(
        endian + "IfffI",
        section,
        504,
        0,
        1.0,
        1.0 / 30.0,
        1.0,
        1,
    )
    type_section = bytearray(160)
    struct.pack_into(endian + "II", type_section, 0, 1, 3)
    struct.pack_into(endian + "II", type_section, 64, 1, 4)
    struct.pack_into(endian + "II", type_section, 128, 1, 5)
    sections = [bytes(section)] + [b""] * 5 + [bytes(type_section), b""]
    relocations = [
        [
            (80, 0, 500),
            (500, 0, 504),
            (524, 0, 528),
            (528, 0, 204),
            (220, 0, 340),
            (348, 6, 0),
            (352, 0, 400),
            (356, 6, 64),
            (360, 0, 424),
            (364, 6, 128),
        ]
    ] + [[] for _ in range(7)]
    return granny_wrapper(
        strings,
        make_granny_container(sections, relocations, endian=endian),
        animation=True,
        additive_bone_indices=additive_bone_indices,
        xbox_wrapper_records=xbox_wrapper_records,
        endian=endian,
    )


def make_cubic_granny_animation() -> bytes:
    strings = [
        "<file>",
        "root",
        "group",
        "CurveDataHeader_D4Constant32f",
        "CurveDataHeader_D3I1K32fC32f",
        "CurveDataHeader_DaIdentity",
    ]
    section = bytearray(760)
    struct.pack_into("<I", section, 76, 1)
    struct.pack_into("<I", section, 204, 2)
    struct.pack_into("<I", section, 216, 1)
    struct.pack_into("<II", section, 340, 1, 0)
    struct.pack_into(
        "<BBH4f",
        section,
        400,
        5,
        0,
        0,
        0.0,
        0.0,
        0.0,
        1.0,
    )
    knots = tuple(float(index) for index in range(7))
    controls = tuple(float(value) for value in range(0, 70, 10))
    struct.pack_into("<BBH", section, 424, 2, 3, 0)
    struct.pack_into("<3f", section, 428, 1.0, 0.0, 0.0)
    struct.pack_into("<3f", section, 440, 0.0, 0.0, 0.0)
    struct.pack_into("<I", section, 452, len(knots) + len(controls))
    struct.pack_into("<14f", section, 480, *(knots + controls))
    struct.pack_into(
        "<IfffI",
        section,
        704,
        0,
        6.0,
        1.0,
        1.0,
        1,
    )
    type_section = bytearray(160)
    struct.pack_into("<II", type_section, 0, 1, 3)
    struct.pack_into("<II", type_section, 64, 1, 4)
    struct.pack_into("<II", type_section, 128, 1, 5)
    sections = [bytes(section)] + [b""] * 5 + [bytes(type_section), b""]
    relocations = [
        [
            (80, 0, 700),
            (700, 0, 704),
            (724, 0, 728),
            (728, 0, 204),
            (220, 0, 340),
            (348, 6, 0),
            (352, 0, 400),
            (356, 6, 64),
            (360, 0, 424),
            (364, 6, 128),
            (456, 0, 480),
        ]
    ] + [[] for _ in range(7)]
    return granny_wrapper(
        strings,
        make_granny_container(sections, relocations),
        animation=True,
    )


def make_sampled_granny_animation() -> bytes:
    endian = ">"
    strings = [
        "<file>",
        "root",
        "group",
        "CurveDataHeader_DaKeyframes32f",
    ]
    section = bytearray(760)
    struct.pack_into(endian + "I", section, 76, 1)
    struct.pack_into(endian + "I", section, 204, 2)
    struct.pack_into(endian + "I", section, 216, 1)
    struct.pack_into(endian + "II", section, 340, 1, 0)
    curves = (
        (
            400,
            4,
            480,
            (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0),
        ),
        (412, 3, 512, (0.0, 0.0, 0.0, 2.0, 4.0, 6.0)),
        (
            424,
            9,
            536,
            (
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
                2.0,
                0.0,
                0.0,
                0.0,
                2.0,
                0.0,
                0.0,
                0.0,
                2.0,
            ),
        ),
    )
    for curve_offset, dimension, controls_offset, controls in curves:
        struct.pack_into(
            endian + "II",
            section,
            curve_offset,
            dimension,
            len(controls),
        )
        struct.pack_into(
            endian + "f" * len(controls),
            section,
            controls_offset,
            *controls,
        )
    struct.pack_into(
        endian + "IfffI",
        section,
        704,
        0,
        1.0,
        1.0,
        1.0,
        1,
    )
    type_section = bytearray(160)
    for offset in (0, 64, 128):
        struct.pack_into(endian + "II", type_section, offset, 1, 3)
    sections = [bytes(section)] + [b""] * 5 + [bytes(type_section), b""]
    relocations = [
        [
            (80, 0, 700),
            (700, 0, 704),
            (724, 0, 728),
            (728, 0, 204),
            (220, 0, 340),
            (348, 6, 0),
            (352, 0, 400),
            (356, 6, 64),
            (360, 0, 412),
            (364, 6, 128),
            (368, 0, 424),
            (408, 0, 480),
            (420, 0, 512),
            (432, 0, 536),
        ]
    ] + [[] for _ in range(7)]
    return granny_wrapper(
        strings,
        make_granny_container(sections, relocations, endian=endian),
        animation=True,
        endian=endian,
    )


def make_fbrb(
    path: str,
    asset_type: str,
    payload: bytes,
    *,
    compress_payload: bool = False,
) -> bytes:
    strings = path.encode() + b"\0" + asset_type.encode() + b"\0"
    type_offset = len(path.encode()) + 1
    metadata = bytearray(struct.pack(">II", 2, len(strings)))
    metadata.extend(strings)
    metadata.extend(struct.pack(">I", 1))
    metadata.extend(
        struct.pack(
            ">6I",
            0,
            0x10000,
            0,
            len(payload),
            len(payload),
            type_offset,
        )
    )
    metadata.extend(b"\1" if compress_payload else b"\0")
    metadata.extend(struct.pack(">I", len(payload)))
    compressed = gzip.compress(bytes(metadata), mtime=0)
    stored_payload = (
        gzip.compress(payload, mtime=0)
        if compress_payload
        else payload
    )
    return (
        b"FbRB"
        + struct.pack(">I", len(compressed))
        + compressed
        + stored_payload
    )


def make_xdvdfs_image(path: Path, archive: bytes):
    sector_size = 2048
    descriptor_offset = 0x10000
    root_sector = 40
    file_sector = 50
    image_size = file_sector * sector_size + len(archive)
    image = bytearray(image_size)
    descriptor = bytearray(sector_size)
    magic = b"MICROSOFT*XBOX*MEDIA"
    descriptor[: len(magic)] = magic
    struct.pack_into("<II", descriptor, 20, root_sector, sector_size)
    descriptor[-len(magic) :] = magic
    image[descriptor_offset : descriptor_offset + sector_size] = descriptor
    name = b"test.fbrb"
    root = bytearray(sector_size)
    struct.pack_into("<HHII", root, 0, 0, 0, file_sector, len(archive))
    root[12] = 0x80
    root[13] = len(name)
    root[14 : 14 + len(name)] = name
    root_offset = root_sector * sector_size
    image[root_offset : root_offset + sector_size] = root
    file_offset = file_sector * sector_size
    image[file_offset : file_offset + len(archive)] = archive
    path.write_bytes(image)


def make_stfs_package(
    path: Path,
    archive: bytes,
    *,
    consecutive: bool = True,
):
    block_size = 0x1000
    data_offset = 0xB000
    archive_start_block = 0xA9
    archive_block_count = (len(archive) + block_size - 1) // block_size
    total_allocated_blocks = archive_start_block + archive_block_count

    def block_offset(block: int) -> int:
        physical_block = block + (block + 0xAA) // 0xAA
        if block > 0xAA:
            physical_block += (block + 0x70E4) // 0x70E4
            if block > 0x70E4:
                physical_block += (block + 0x4AF768) // 0x4AF768
        return data_offset + physical_block * block_size

    image_size = block_offset(total_allocated_blocks - 1) + block_size
    image = bytearray(image_size)
    image[:4] = b"LIVE"
    struct.pack_into(">I", image, 0x340, 0xAD0E)
    struct.pack_into(">I", image, 0x344, 0x000D0000)
    struct.pack_into(">I", image, 0x348, 2)
    struct.pack_into(">Q", image, 0x34C, image_size - data_offset)
    struct.pack_into(">I", image, 0x360, 0x5841097E)
    descriptor = memoryview(image)[0x379 : 0x379 + 0x24]
    descriptor[0] = 0x24
    descriptor[2] = 1
    struct.pack_into("<H", descriptor, 3, 1)
    descriptor[5:8] = (0).to_bytes(3, "little")
    struct.pack_into(">I", descriptor, 0x1C, total_allocated_blocks)
    struct.pack_into(">I", image, 0x3A9, 0)

    def file_record(
        name: str,
        *,
        flags: int,
        block_count: int,
        start_block: int,
        parent: int,
        size: int,
    ) -> bytes:
        encoded = name.encode("ascii")
        record = bytearray(0x40)
        record[: len(encoded)] = encoded
        record[0x28] = len(encoded) | flags
        record[0x29:0x2C] = block_count.to_bytes(3, "little")
        record[0x2C:0x2F] = block_count.to_bytes(3, "little")
        record[0x2F:0x32] = start_block.to_bytes(3, "little")
        struct.pack_into(">H", record, 0x32, parent)
        struct.pack_into(">I", record, 0x34, size)
        return bytes(record)

    table = bytearray(block_size)
    table[0x00:0x40] = file_record(
        "dist",
        flags=0x80,
        block_count=0,
        start_block=0,
        parent=0xFFFF,
        size=0,
    )
    table[0x40:0x80] = file_record(
        "xenon",
        flags=0x80,
        block_count=0,
        start_block=0,
        parent=0,
        size=0,
    )
    table[0x80:0xC0] = file_record(
        "test.fbrb",
        flags=0x40 if consecutive else 0,
        block_count=archive_block_count,
        start_block=archive_start_block,
        parent=1,
        size=len(archive),
    )
    table_offset = block_offset(0)
    image[table_offset : table_offset + block_size] = table
    for index in range(archive_block_count):
        source_start = index * block_size
        chunk = archive[source_start : source_start + block_size]
        destination = block_offset(archive_start_block + index)
        image[destination : destination + len(chunk)] = chunk
    path.write_bytes(image)


def make_heightfield(header_endian: str, sample_endian: str) -> bytes:
    resolution = 4
    data = bytearray(49 + resolution * resolution * 2)
    struct.pack_into(
        header_endian + "6f6IB",
        data,
        0,
        -2.0,
        0.0,
        -2.0,
        2.0,
        10.0,
        2.0,
        resolution,
        0,
        0,
        resolution * 256,
        0,
        1,
        0,
    )
    for index in range(resolution * resolution):
        struct.pack_into(
            sample_endian + "H",
            data,
            49 + index * 2,
            index * 100,
        )
    return bytes(data)


def make_terrain_material_map(endian: str) -> bytes:
    side = 512
    tile_count = 2
    data = bytearray(struct.pack(endian + "IH", side, tile_count))
    for tile_index in range(tile_count):
        packed_value = 0x10 if tile_index == 0 else 0x01
        body = bytes((packed_value, packed_value, 255)) * 256
        data.extend(struct.pack(endian + "I", len(body)))
        data.extend(body)
        data.extend(struct.pack(endian + "256H", *([3] * 256)))
    names = b"defaultmaterial\0Grass\0"
    data.extend(struct.pack(endian + "I", len(names)))
    data.extend(names)
    return bytes(data)


def make_shader_database() -> bytes:
    parameter_reference = b"P" * 16
    material_reference = b"M" * 16
    layout_reference = b"L" * 16
    texture_name = b"Textures/Test/Color"
    group_size = 4 + 20 + 6 + 2 + 4 + 136

    data = bytearray(b"SHDR\0")
    data.extend(struct.pack("<i", 1))
    data.extend(struct.pack("<i", group_size))
    data.extend(b"\0" * 20)
    data.extend(bytes((0, 0, 0, 1, 0, 0)))
    data.extend(b"\0" * 2)
    data.extend(struct.pack("<i", 3))
    data.extend(texture_name + b"\0" * (136 - len(texture_name)))
    data.extend(struct.pack("<ii", 0, 0))
    data.extend(struct.pack("<ii", 0, 0))
    data.extend(b"\0" * 12)
    data.extend(struct.pack("<i", 1))
    data.extend(parameter_reference)
    data.extend(struct.pack("<8i", 0, 0, 0, 0, 0, 0, 0, 0))
    data.extend(struct.pack("<i", 1))
    data.extend(parameter_reference * 2 + layout_reference)
    data.extend(b"\0" * 10)
    data.extend(struct.pack("<i", 0))
    data.extend(b"\0" * 2)
    data.extend(struct.pack("<i", 1))
    layout = bytearray(89)
    layout[:16] = layout_reference
    struct.pack_into("<H", layout, 16, 259)
    layout[80] = 1
    struct.pack_into("<I", layout, 81, 0xFFFFFF00)
    layout[85] = 12
    data.extend(layout)
    data.extend(struct.pack("<i", 1))
    data.extend(b"Objects/Test/Mesh/Shaders/Surface\0")
    data.extend(material_reference)
    data.extend(struct.pack("<ii", 0, 0))
    data.extend(struct.pack("<i", 1))
    data.extend(material_reference)
    data.extend(struct.pack("<i", 1))
    data.extend(struct.pack("<h", 0))
    return bytes(data)


def make_xbox_shader_database(
    *,
    vertex_shader_index: int = 0,
    key_count: int = 1,
    trailing: bytes = b"",
) -> bytes:
    entry_reference = b"E" * 16
    layout_reference = b"L" * 16
    program_reference = b"P" * 16
    vertex_reference = b"V" * 16
    pixel_reference = b"X" * 16
    material_key = "Objects/Test/Mesh/Shaders/Surface"
    texture_name = b"Textures/Test/XboxColor"

    data = bytearray(struct.pack(">IBI", 117, 0, 2))
    data.extend(struct.pack(">I", 32))
    data.extend(b"\0" * 20)
    data.extend(bytes(8))

    group_size = 32 + 140
    data.extend(struct.pack(">I", group_size))
    data.extend(b"\0" * 20)
    data.extend(bytes((0, 0, 0, 1, 0, 0)))
    data.extend(b"\0" * 2)
    data.extend(struct.pack("<I", 0x104))
    data.extend(texture_name + b"\0" * (136 - len(texture_name)))

    data.extend(struct.pack(">IHH", 1, 0, 0))
    data.extend(struct.pack(">II", 1, 0))

    data.extend(struct.pack(">I", 1))
    data.extend(vertex_reference)
    data.extend(struct.pack(">I", 4))
    data.extend(b"VERT")
    data.extend(struct.pack(">3I", 0, 0, 0))
    data.extend(struct.pack(">I", 1))
    data.extend(b"\0" * 28)

    data.extend(struct.pack(">I", 1))
    data.extend(pixel_reference)
    data.extend(struct.pack(">I", 4))
    data.extend(b"PIXL")
    data.extend(struct.pack(">3I", 1, 0, 0))

    data.extend(struct.pack(">I", 1))
    data.extend(program_reference)
    data.extend(
        struct.pack(
            ">8I",
            0,
            0,
            0,
            0,
            vertex_shader_index,
            0,
            0,
            1,
        )
    )

    data.extend(struct.pack(">I", key_count))
    for _ in range(key_count):
        data.extend(entry_reference)
        data.extend(bytes(16))
        data.extend(layout_reference)
        data.extend(bytes(44))

    data.extend(struct.pack(">I", 1))
    layout = bytearray(89)
    layout[:16] = layout_reference
    struct.pack_into("<I", layout, 16, 0x103)
    layout[20:24] = bytes((6, 0, 24, 0))
    layout[24:28] = bytes((6, 0, 28, 0))
    layout[28:32] = bytes((6, 0, 32, 0))
    layout[80] = 4
    layout[85] = 36
    data.extend(layout)

    data.extend(struct.pack(">I", 1))
    data.extend(material_key.encode("ascii") + b"\0")
    data.extend(entry_reference)
    data.extend(struct.pack(">I", 0))
    data.extend(struct.pack(">I", 1))
    data.extend(b"Objects/Test/Mesh_lod0/Surface/Standard\0")
    data.extend(layout_reference)
    data.extend(struct.pack(">I", 1))
    data.extend(entry_reference)
    data.extend(struct.pack(">IH", 1, 0))
    data.extend(trailing)
    return bytes(data)


def _dxbc_operand(
    operand_type: int,
    register: int,
    *,
    mask: int | None = None,
    swizzle: tuple[int, int, int, int] | None = None,
    modifier: int = 0,
) -> tuple[int, ...]:
    token = 2 | (operand_type << 12) | (1 << 20)
    if mask is not None:
        token |= mask << 4
    else:
        token |= 1 << 2
        for component, source in enumerate(swizzle or (0, 1, 2, 3)):
            token |= source << (4 + component * 2)
    if modifier:
        return token | 0x80000000, modifier << 6, register
    return token, register


def _dxbc_immediate(*values: float) -> tuple[int, ...]:
    padded = (*values, *(0.0 for _ in range(4 - len(values))))
    return (
        2 | (4 << 12),
        *(
            struct.unpack("<I", struct.pack("<f", value))[0]
            for value in padded
        ),
    )


def _dxbc_instruction(opcode: int, *operands: tuple[int, ...]):
    length = 1 + sum(len(operand) for operand in operands)
    return (opcode | (length << 24), *(value for operand in operands for value in operand))


def _dxbc_signature(
    semantic: str,
    semantic_index: int,
    register: int,
) -> bytes:
    name = semantic.encode("ascii") + b"\0"
    chunk = bytearray(8 + 24 + len(name))
    struct.pack_into("<II", chunk, 0, 1, 8)
    struct.pack_into(
        "<5I",
        chunk,
        8,
        32,
        semantic_index,
        0,
        3,
        register,
    )
    chunk[28] = 0xF
    chunk[29] = 0xF
    chunk[32:] = name
    return bytes(chunk)


def _dxbc_container(chunks: tuple[tuple[bytes, bytes], ...]) -> bytes:
    table_size = 32 + len(chunks) * 4
    offsets = []
    body = bytearray()
    for name, chunk in chunks:
        offsets.append(table_size + len(body))
        body.extend(name)
        body.extend(struct.pack("<I", len(chunk)))
        body.extend(chunk)
    total_size = table_size + len(body)
    return (
        b"DXBC"
        + b"\0" * 16
        + struct.pack("<III", 1, total_size, len(chunks))
        + struct.pack(f"<{len(offsets)}I", *offsets)
        + body
    )


def _dxbc_program(
    instructions: tuple[int, ...],
    signature: bytes | None = None,
) -> bytes:
    tokens = (0x00010050, 2 + len(instructions), *instructions)
    shader = struct.pack(f"<{len(tokens)}I", *tokens)
    chunks = ((b"SHEX", shader),)
    if signature is not None:
        chunks = ((b"ISGN", signature), *chunks)
    return _dxbc_container(chunks)


def element(
    tag: str,
    *children: DbxElement,
    value: str | None = None,
    **attributes: str,
) -> DbxElement:
    return DbxElement(
        tag=tag,
        attributes=tuple(attributes.items()),
        value=value,
        children=tuple(children),
    )


class MeshTests(unittest.TestCase):
    def test_parses_big_and_little_endian_geometry(self):
        for endian, platform in ((">", "xbox360"), ("<", "pc")):
            with self.subTest(platform=platform):
                mesh = parse_meshdata(make_meshdata(endian), platform)
                subset = mesh.subsets[0]
                self.assertEqual(len(subset.positions), 3)
                self.assertEqual(subset.faces, ((0, 1, 2),))
                self.assertEqual(subset.positions[1], (1.0, 0.0, 0.0))

    def test_parses_uint32_index_buffer(self):
        metadata = bytearray(struct.pack(">IHB", 0, 0, 1))
        metadata.extend(b"edge\0")
        metadata.extend(
            struct.pack(
                ">IIIIBBBBH",
                1,
                3,
                0,
                0,
                12,
                3,
                0,
                0,
                0,
            )
        )
        vertices = b"".join(
            struct.pack(">fff", *position)
            for position in (
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
            )
        )
        indices = struct.pack(">3I", 0, 1, 2)
        metadata.extend(
            struct.pack(">III", len(vertices), len(indices), 1)
        )
        subset = parse_meshdata(
            bytes(metadata + vertices + indices),
            "xbox360",
            position_format="FLOAT",
        ).subsets[0]
        self.assertEqual(subset.faces, ((0, 1, 2),))

    def test_xbox_uv_detection_rejects_degenerate_preferred_field(self):
        endian = ">"
        metadata = bytearray(struct.pack(endian + "IHB", 0, 0, 1))
        metadata.extend(b"visual\0")
        metadata.extend(
            struct.pack(
                endian + "IIIIBBBBH",
                1,
                3,
                0,
                0,
                32,
                3,
                0,
                0,
                0,
            )
        )
        vertices = bytearray()
        for position, uv, degenerate in zip(
            (
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
            ),
            ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
            ((0.0, 0.0), (0.001, 0.001), (0.0005, 0.0005)),
        ):
            vertex = bytearray(32)
            struct.pack_into(endian + "fff", vertex, 0, *position)
            struct.pack_into(endian + "ee", vertex, 16, *uv)
            struct.pack_into(endian + "ee", vertex, 24, *degenerate)
            vertices.extend(vertex)
        indices = struct.pack(endian + "3H", 0, 1, 2)
        metadata.extend(
            struct.pack(endian + "III", len(vertices), len(indices), 0)
        )

        subset = parse_meshdata(
            bytes(metadata + vertices + indices),
            "xbox360",
            position_format="FLOAT",
        ).subsets[0]

        self.assertEqual(subset.uv_offsets, (16,))
        self.assertEqual(
            subset.uvs,
            ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        )

    def test_parses_triangle_strip_and_omits_degenerates(self):
        metadata = bytearray(struct.pack(">IHB", 0, 0, 1))
        metadata.extend(b"strip\0")
        metadata.extend(
            struct.pack(
                ">IIIIBBBBH",
                3,
                4,
                0,
                0,
                12,
                5,
                0,
                0,
                0,
            )
        )
        vertices = b"".join(
            struct.pack(">fff", *position)
            for position in (
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
                (1.0, 1.0, 0.0),
            )
        )
        indices = struct.pack(">5H", 0, 1, 2, 2, 3)
        metadata.extend(
            struct.pack(">III", len(vertices), len(indices), 0)
        )
        subset = parse_meshdata(
            bytes(metadata + vertices + indices),
            "xbox360",
            position_format="FLOAT",
        ).subsets[0]
        self.assertEqual(subset.faces, ((0, 1, 2),))

    def test_watermesh_uses_platform_endian_uint32_indices(self):
        positions = (
            (0.0, 2.0, 0.0),
            (1.0, 2.0, 0.0),
            (1.0, 2.0, 1.0),
            (0.0, 2.0, 1.0),
        )
        faces = ((2, 3, 0), (0, 1, 2))
        for endian, platform in ((">", "xbox360"), ("<", "pc")):
            with self.subTest(platform=platform):
                mesh = parse_watermesh(
                    make_watermesh(endian, ((positions, faces),)),
                    platform,
                )
                subset = mesh.subsets[0]
                self.assertEqual(subset.positions, positions)
                self.assertEqual(subset.faces, faces)

    def test_watermesh_combines_bodies_and_consumes_exact_payload(self):
        triangle = (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 0.0, 1.0),
        )
        data = make_watermesh(
            "<",
            (
                (triangle, ((0, 1, 2),)),
                (triangle, ((0, 2, 1),)),
                ((), ()),
            ),
        )
        subset = parse_watermesh(data, "pc").subsets[0]
        self.assertEqual(len(subset.positions), 6)
        self.assertEqual(subset.faces, ((0, 1, 2), (3, 5, 4)))
        with self.assertRaisesRegex(CorruptDataError, "trailing byte"):
            parse_watermesh(data + b"\0", "pc")

    def test_composite_shared_indices_and_part_splitting(self):
        mesh = parse_meshdata(
            make_composite_meshdata(),
            "pc",
            position_format="FLOAT",
        )
        body, door = mesh.subsets
        self.assertEqual(body.part_indices, (0, 0, 0, 1, 1, 1))
        self.assertEqual(body.part_index_offset, 28)
        self.assertEqual(body.faces, ((0, 1, 2), (3, 4, 5)))
        self.assertEqual(door.positions[0], (4.0, 0.0, 0.0))
        self.assertEqual(door.faces, ((0, 1, 2),))
        self.assertEqual(door.part_indices, (2, 2, 2))

        parts = split_mesh_subset_by_parts(body)
        self.assertEqual(tuple(index for index, _ in parts), (0, 1))
        for index, subset in parts:
            self.assertEqual(subset.faces, ((0, 1, 2),))
            self.assertEqual(len(subset.positions), 3)
            self.assertEqual(subset.part_indices, (index, index, index))
            self.assertEqual(len(subset.uvs), 3)

    def test_composite_lod_part_names_preserve_source_order(self):
        data = (
            b"header\0lod1|LowPart\0"
            b"lod0|Fuselage\0noise\0lod0|LeftDoor\0"
        )
        self.assertEqual(
            parse_composite_lod_part_names(data),
            ("Fuselage", "LeftDoor"),
        )

    def test_composite_lod_part_payload_is_little_endian(self):
        payload = (
            (-1.0, -2.0, -3.0, 4.0, 5.0, 6.0)
            + (1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 1.0, 0.0)
            + (7.0, 8.0, 9.0)
        )
        data = b"header\0lod0|DoorLeft\0" + struct.pack(
            "<18f",
            *payload,
        )
        parts = parse_composite_lod_parts(data, platform="pc")
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].name, "DoorLeft")
        self.assertEqual(parts[0].bounds, payload[:6])
        self.assertEqual(parts[0].transform, payload[6:])

    def test_composite_lod_part_payload_is_big_endian(self):
        payload = (
            (-6.0, -5.0, -4.0, 3.0, 2.0, 1.0)
            + (0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
            + (-9.0, -8.0, -7.0)
        )
        data = b"header\0lod0|RotorTail\0" + struct.pack(
            ">18f",
            *payload,
        )
        parts = parse_composite_lod_parts(
            data,
            platform="xbox360",
        )
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].name, "RotorTail")
        self.assertEqual(parts[0].bounds, payload[:6])
        self.assertEqual(parts[0].transform, payload[6:])

    def test_one_influence_vertices_use_their_palette_indices(self):
        metadata = bytearray(struct.pack("<IHB", 1, 0, 1))
        metadata.extend(b"rigid\0")
        metadata.extend(
            struct.pack(
                "<IIIIBBBBH",
                1,
                3,
                0,
                0,
                16,
                3,
                1,
                2,
                2,
            )
        )
        metadata.extend(struct.pack("<2H", 4, 9))
        vertices = bytearray()
        for index, position in enumerate(
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        ):
            vertex = bytearray(16)
            struct.pack_into("<eee", vertex, 0, *position)
            vertex[8] = index % 2
            vertex[12] = 255
            vertices.extend(vertex)
        indices = struct.pack("<3H", 0, 1, 2)
        metadata.extend(struct.pack("<III", len(vertices), len(indices), 0))
        subset = parse_meshdata(
            bytes(metadata + vertices + indices), "pc"
        ).subsets[0]
        self.assertEqual(
            subset.influences,
            (((4, 1.0),), ((9, 1.0),), ((4, 1.0),)),
        )

    def test_full_float_vertices_shift_skin_fields_after_positions(self):
        metadata = bytearray(struct.pack(">IHB", 1, 0, 1))
        metadata.extend(b"rigid\0")
        metadata.extend(
            struct.pack(
                ">IIIIBBBBH",
                1,
                3,
                0,
                0,
                20,
                3,
                1,
                3,
                3,
            )
        )
        metadata.extend(struct.pack(">3H", 42, 9, 89))
        vertices = bytearray()
        for index, position in enumerate(
            ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        ):
            vertex = bytearray(20)
            struct.pack_into(">fff", vertex, 0, *position)
            vertex[12:16] = bytes((index,)) * 4
            vertex[16] = 255
            vertices.extend(vertex)
        indices = struct.pack(">3H", 0, 1, 2)
        metadata.extend(struct.pack(">III", len(vertices), len(indices), 0))
        subset = parse_meshdata(
            bytes(metadata + vertices + indices),
            "xbox360",
            position_format="FLOAT",
        ).subsets[0]
        self.assertEqual(
            subset.influences,
            (((42, 1.0),), ((9, 1.0),), ((89, 1.0),)),
        )

    def test_composite_palette_does_not_select_skinned_layout(self):
        for stride in (48, 64):
            with self.subTest(stride=stride):
                metadata = bytearray(struct.pack("<IHB", 2, 0, 1))
                metadata.extend(b"composite\0")
                metadata.extend(
                    struct.pack(
                        "<IIIIBBBBH",
                        1,
                        3,
                        0,
                        0,
                        stride,
                        3,
                        1,
                        2,
                        2,
                    )
                )
                metadata.extend(struct.pack("<2H", 4, 9))
                vertices = bytearray()
                values = (
                    ((0.0, 0.0, 0.0), (0.0, 0.0), (0.1, 0.2)),
                    ((1.0, 0.0, 0.0), (1.0, 0.0), (0.9, 0.2)),
                    ((0.0, 1.0, 0.0), (0.0, 1.0), (0.1, 0.8)),
                )
                for position, primary_uv, secondary_uv in values:
                    vertex = bytearray(stride)
                    struct.pack_into("<eee", vertex, 0, *position)
                    vertex[8] = 57
                    primary_offset = 28 if stride == 48 else 24
                    secondary_offset = 36 if stride == 48 else 28
                    struct.pack_into(
                        "<ee", vertex, primary_offset, *primary_uv
                    )
                    struct.pack_into(
                        "<ee", vertex, secondary_offset, *secondary_uv
                    )
                    vertices.extend(vertex)
                indices = struct.pack("<3H", 0, 1, 2)
                metadata.extend(
                    struct.pack("<III", len(vertices), len(indices), 0)
                )
                raw = bytes(metadata + vertices + indices)
                subset = parse_meshdata(raw, "pc").subsets[0]
                self.assertEqual(
                    subset.uv_offsets,
                    (28, 36, 32, 24) if stride == 48 else (24, 28),
                )
                self.assertEqual(
                    subset.influences,
                    (((4, 1.0),), ((4, 1.0),), ((4, 1.0),)),
                )

    def test_pc_composite_stride_48_uses_declared_uv_channels(self):
        metadata = bytearray(struct.pack("<IHB", 2, 0, 1))
        metadata.extend(b"composite\0")
        metadata.extend(
            struct.pack(
                "<IIIIBBBBH",
                2,
                6,
                0,
                0,
                48,
                3,
                0,
                2,
                2,
            )
        )
        metadata.extend(struct.pack("<2H", 31, 32))
        positions = (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (2.0, 0.0, 0.0),
            (3.0, 0.0, 0.0),
            (2.0, 1.0, 0.0),
        )
        vertices = bytearray()
        for index, position in enumerate(positions):
            vertex = bytearray(48)
            struct.pack_into("<eee", vertex, 0, *position)
            struct.pack_into(
                "<ee",
                vertex,
                24,
                index / 10.0,
                index / 20.0,
            )
            struct.pack_into(
                "<ee",
                vertex,
                28,
                index / 6.0,
                1.0 - index / 6.0,
            )
            struct.pack_into(
                "<ee",
                vertex,
                32,
                0.25 + index / 20.0,
                0.5,
            )
            part_index = 31 if index < 3 else 32
            vertex[36:40] = bytes((part_index,)) * 4
            struct.pack_into(
                "<ee",
                vertex,
                40,
                0.75,
                index / 10.0,
            )
            vertices.extend(vertex)
        indices = struct.pack("<6H", *range(6))
        metadata.extend(
            struct.pack("<III", len(vertices), len(indices), 0)
        )

        subset = parse_meshdata(
            bytes(metadata + vertices + indices),
            "pc",
            position_format="HALF",
        ).subsets[0]

        self.assertEqual(subset.part_index_offset, 36)
        self.assertEqual(subset.part_indices, (31, 31, 31, 32, 32, 32))
        self.assertEqual(subset.uv_offsets, (24, 28, 32, 40))
        self.assertAlmostEqual(subset.uvs[5][0], 0.5, places=3)
        self.assertAlmostEqual(
            subset.secondary_uvs[1][0],
            1.0 / 6.0,
            places=3,
        )
        self.assertAlmostEqual(
            subset.additional_uvs[0][4][0],
            0.45,
            places=3,
        )
        self.assertAlmostEqual(
            subset.additional_uvs[1][5][1],
            0.5,
            places=3,
        )
        split = dict(split_mesh_subset_by_parts(subset))
        self.assertEqual(split[31].uv_offsets, (24, 28, 32, 40))
        self.assertEqual(len(split[31].additional_uvs), 2)

    def test_pc_composite_stride_48_offset_32_layout_propagates(self):
        metadata = bytearray(struct.pack("<IHB", 2, 0, 2))
        definitions = (
            ("multi_part", 0, 0, (31, 32)),
            ("single_part", 3, 3 * 48, (12,)),
        )
        for name, index_offset, vertex_offset, palette in definitions:
            metadata.extend(name.encode("ascii") + b"\0")
            metadata.extend(
                struct.pack(
                    "<IIIIBBBBH",
                    1,
                    3,
                    index_offset,
                    vertex_offset,
                    48,
                    3,
                    0,
                    len(palette),
                    len(palette),
                )
            )
            metadata.extend(
                struct.pack(f"<{len(palette)}H", *palette)
            )

        vertices = bytearray()
        positions = (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
        )
        for subset_index, (_name, _indices, _vertices, palette) in enumerate(
            definitions
        ):
            for index, position in enumerate(positions):
                vertex = bytearray(48)
                struct.pack_into("<eee", vertex, 0, *position)
                struct.pack_into(
                    "<ee",
                    vertex,
                    24,
                    (
                        index * 75.0
                        if subset_index == 0
                        else index / 2.0
                    ),
                    subset_index / 2.0,
                )
                struct.pack_into(
                    "<ee",
                    vertex,
                    28,
                    index / 3.0,
                    1.0 - index / 3.0,
                )
                part_index = (
                    palette[index % len(palette)]
                    if len(palette) > 1
                    else palette[0]
                )
                vertex[32:36] = bytes((part_index,)) * 4
                struct.pack_into(
                    "<ee",
                    vertex,
                    36,
                    0.25 + index / 4.0,
                    0.5,
                )
                struct.pack_into(
                    "<ee",
                    vertex,
                    40,
                    0.75,
                    index / 4.0,
                )
                vertices.extend(vertex)
        indices = struct.pack("<6H", 0, 1, 2, 0, 1, 2)
        metadata.extend(
            struct.pack("<III", len(vertices), len(indices), 0)
        )

        multi_part, single_part = parse_meshdata(
            bytes(metadata + vertices + indices),
            "pc",
            position_format="HALF",
        ).subsets

        self.assertEqual(multi_part.part_index_offset, 32)
        self.assertEqual(multi_part.part_indices, (31, 32, 31))
        self.assertEqual(multi_part.uv_offsets, (24, 28, 36, 40))
        self.assertEqual(multi_part.uvs[2][0], 150.0)
        self.assertEqual(single_part.part_index_offset, None)
        self.assertEqual(single_part.part_indices, (12, 12, 12))
        self.assertEqual(single_part.uv_offsets, (24, 28, 36, 40))
        self.assertAlmostEqual(single_part.uvs[2][0], 1.0, places=3)
        self.assertAlmostEqual(
            single_part.additional_uvs[1][2][1],
            0.5,
            places=3,
        )

    def test_pc_composite_stride_64_ranks_uvs_by_topology(self):
        metadata = bytearray(struct.pack("<IHB", 2, 0, 2))
        definitions = (
            ("multi_part", 2, 6, 0, 0, (2, 3)),
            ("single_part", 1, 3, 6, 6 * 64, (7,)),
        )
        for (
            name,
            primitive_count,
            vertex_count,
            index_offset,
            vertex_offset,
            palette,
        ) in definitions:
            metadata.extend(name.encode("ascii") + b"\0")
            metadata.extend(
                struct.pack(
                    "<IIIIBBBBH",
                    primitive_count,
                    vertex_count,
                    index_offset,
                    vertex_offset,
                    64,
                    3,
                    0,
                    len(palette),
                    len(palette),
                )
            )
            metadata.extend(
                struct.pack(f"<{len(palette)}H", *palette)
            )

        positions = (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (2.0, 0.0, 0.0),
            (3.0, 0.0, 0.0),
            (2.0, 1.0, 0.0),
        )
        primary_uvs = (
            (0.0, 0.0),
            (1.0, 0.0),
            (0.0, 1.0),
            (0.0, 0.0),
            (1.0, 0.0),
            (0.0, 1.0),
        )
        secondary_uvs = (
            (0.1, 0.2),
            (0.9, 0.2),
            (0.1, 0.8),
            (0.1, 0.2),
            (0.9, 0.2),
            (0.1, 0.8),
        )
        vertices = bytearray()
        for subset_index, definition in enumerate(definitions):
            palette = definition[-1]
            subset_positions = positions if subset_index == 0 else positions[:3]
            for index, position in enumerate(subset_positions):
                vertex = bytearray(64)
                struct.pack_into("<eee", vertex, 0, *position)
                struct.pack_into("<ee", vertex, 24, index / 3.0, 0.0)
                struct.pack_into("<ee", vertex, 28, index / 4.0, 0.0)
                struct.pack_into("<ee", vertex, 32, index / 5.0, 0.0)
                struct.pack_into(
                    "<ee",
                    vertex,
                    36,
                    *primary_uvs[index],
                )
                struct.pack_into(
                    "<ee",
                    vertex,
                    40,
                    *secondary_uvs[index],
                )
                part_index = (
                    palette[0 if index < 3 else 1]
                    if len(palette) > 1
                    else palette[0]
                )
                vertex[48:52] = bytes((part_index,)) * 4
                vertices.extend(vertex)

        indices = struct.pack(
            "<9H",
            0,
            1,
            2,
            3,
            4,
            5,
            0,
            1,
            2,
        )
        metadata.extend(
            struct.pack("<III", len(vertices), len(indices), 0)
        )
        mesh = parse_meshdata(
            bytes(metadata + vertices + indices),
            "pc",
            position_format="HALF",
        )
        multi_part, single_part = mesh.subsets

        self.assertEqual(multi_part.part_index_offset, 48)
        self.assertEqual(multi_part.uv_offsets, (36, 40))
        self.assertEqual(single_part.part_index_offset, None)
        self.assertEqual(single_part.uv_offsets, (36, 40))
        self.assertAlmostEqual(multi_part.uvs[2][1], 1.0, places=3)
        self.assertAlmostEqual(
            single_part.secondary_uvs[1][0],
            0.9,
            places=3,
        )
        split = dict(split_mesh_subset_by_parts(multi_part))
        self.assertEqual(split[2].uv_offsets, (36, 40))

    def test_stride_48_uses_declared_uv_channels(self):
        metadata = bytearray(struct.pack("<IHB", 0, 0, 1))
        metadata.extend(b"surface\0")
        metadata.extend(
            struct.pack(
                "<IIIIBBBBH",
                1,
                3,
                0,
                0,
                48,
                3,
                0,
                0,
                0,
            )
        )
        metadata.extend(b"\0" * 4)
        vertices = bytearray()
        values = (
            ((0.0, 0.0, 0.0), (0.2, 0.3), (0.0, 0.0), (0.1, 0.2)),
            ((1.0, 0.0, 0.0), (0.8, 0.3), (1.0, 0.0), (0.9, 0.2)),
            ((0.0, 1.0, 0.0), (0.2, 0.9), (0.0, 1.0), (0.1, 0.8)),
        )
        for index, (
            position,
            packed_vector,
            primary_uv,
            secondary_uv,
        ) in enumerate(values):
            vertex = bytearray(48)
            struct.pack_into("<eee", vertex, 0, *position)
            struct.pack_into("<ee", vertex, 8, *packed_vector)
            struct.pack_into("<ee", vertex, 24, 0.25, index / 10.0)
            struct.pack_into("<ee", vertex, 28, *primary_uv)
            struct.pack_into("<ee", vertex, 32, 0.75, index / 20.0)
            struct.pack_into("<ee", vertex, 36, *secondary_uv)
            struct.pack_into("<ee", vertex, 40, index / 4.0, 0.5)
            struct.pack_into("<ee", vertex, 44, index / 5.0, 0.75)
            vertices.extend(vertex)
        indices = struct.pack("<3H", 0, 1, 2)
        metadata.extend(
            struct.pack("<III", len(vertices), len(indices), 0)
        )
        parsed = parse_meshdata(
            bytes(metadata + vertices + indices),
            "pc",
        )
        subset = parsed.subsets[0]
        self.assertEqual(subset.uv_offsets, (28, 36, 32, 24))
        self.assertEqual(subset.uvs[1], (1.0, 0.0))
        self.assertAlmostEqual(subset.secondary_uvs[2][1], 0.8, places=3)
        self.assertAlmostEqual(
            subset.additional_uvs[0][2][1], 0.1, places=3
        )
        self.assertAlmostEqual(
            subset.additional_uvs[1][1][1], 0.1, places=3
        )
        vehicle_subset = parse_meshdata(
            bytes(metadata + vertices + indices),
            "pc",
            retain_pc_vehicle_uv_candidates=True,
        ).subsets[0]
        self.assertEqual(
            vehicle_subset.uv_offsets,
            (28, 36, 32, 24, 40),
        )
        self.assertEqual(
            vehicle_subset.uv_source,
            "pc_vehicle_shader_route_candidates",
        )
        shader_subset = parse_meshdata(
            bytes(metadata + vertices + indices),
            "pc",
            pc_declared_uv_offsets_by_stride={48: (32, 36, 40, 44)},
        ).subsets[0]
        self.assertEqual(
            shader_subset.uv_offsets,
            (28, 36, 32, 24, 40, 44),
        )
        self.assertEqual(
            shader_subset.uv_source,
            "verified_pc_stride_48_declaration"
            "+shader_layout_candidates",
        )
        self.assertAlmostEqual(
            shader_subset.additional_uvs[-1][2][0],
            0.4,
            places=3,
        )

    def test_big_endian_mesh_retains_shader_declared_uv_channels(self):
        metadata = bytearray(struct.pack(">IHB", 0, 0, 1))
        metadata.extend(b"carrier\0")
        metadata.extend(
            struct.pack(
                ">IIIIBBBBH",
                1,
                3,
                0,
                0,
                36,
                3,
                0,
                0,
                0,
            )
        )
        metadata.extend(b"\0" * 4)
        vertices = bytearray()
        values = (
            ((0.0, 0.0, 0.0), (0.0, 0.0), (0.1, 0.2), (0.2, 0.3)),
            ((1.0, 0.0, 0.0), (1.0, 0.0), (0.8, 0.2), (0.7, 0.3)),
            ((0.0, 1.0, 0.0), (0.0, 1.0), (0.1, 0.9), (0.2, 0.8)),
        )
        for position, primary_uv, palette_uv, ao_uv in values:
            vertex = bytearray(36)
            struct.pack_into(">fff", vertex, 0, *position)
            struct.pack_into(">ee", vertex, 24, *primary_uv)
            struct.pack_into(">ee", vertex, 28, *palette_uv)
            struct.pack_into(">ee", vertex, 32, *ao_uv)
            vertices.extend(vertex)
        indices = struct.pack(">3H", 0, 1, 2)
        metadata.extend(
            struct.pack(">III", len(vertices), len(indices), 0)
        )
        subset = parse_meshdata(
            bytes(metadata + vertices + indices),
            "xbox360",
            declared_uv_offsets_by_stride={36: (24, 28, 32)},
        ).subsets[0]
        self.assertEqual(subset.uv_offsets, (24, 28, 32))
        self.assertEqual(
            subset.uv_source,
            "stride_heuristic+shader_layout_candidates",
        )
        self.assertAlmostEqual(
            subset.additional_uvs[0][2][1],
            0.8,
            places=3,
        )

    def test_pc_skinned_stride_64_uses_declared_uv_channels(self):
        metadata = bytearray(struct.pack("<IHB", 1, 0, 1))
        metadata.extend(b"character\0")
        metadata.extend(
            struct.pack(
                "<IIIIBBBBH",
                1,
                3,
                0,
                0,
                64,
                3,
                4,
                1,
                1,
            )
        )
        metadata.extend(struct.pack("<H", 7))
        vertices = bytearray()
        channels = (
            (
                (0.1, 0.2),
                (0.3, 0.4),
                (0.5, 0.6),
                (0.7, 0.8),
            ),
            (
                (0.9, 0.2),
                (0.4, 0.5),
                (0.6, 0.7),
                (0.7, 0.8),
            ),
            (
                (0.1, 0.9),
                (0.5, 0.6),
                (0.7, 0.8),
                (0.7, 0.8),
            ),
        )
        positions = (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
        )
        for position, vertex_channels in zip(positions, channels):
            vertex = bytearray(64)
            struct.pack_into("<eee", vertex, 0, *position)
            vertex[12] = 255
            struct.pack_into("<ee", vertex, 16, *vertex_channels[1])
            struct.pack_into("<eeee", vertex, 24, 0.8, -0.5, 0.3, 1.0)
            struct.pack_into(
                "<eeee",
                vertex,
                40,
                *vertex_channels[0],
                *vertex_channels[2],
            )
            struct.pack_into("<ee", vertex, 48, *vertex_channels[3])
            vertices.extend(vertex)
        indices = struct.pack("<3H", 0, 1, 2)
        metadata.extend(
            struct.pack("<III", len(vertices), len(indices), 0)
        )
        raw = bytes(metadata + vertices + indices)
        subset = parse_meshdata(raw, "pc").subsets[0]
        self.assertEqual(subset.uv_offsets, (40, 16, 44, 48))
        self.assertAlmostEqual(subset.uvs[1][0], 0.9, places=3)
        self.assertAlmostEqual(
            subset.secondary_uvs[2][1], 0.6, places=3
        )
        self.assertAlmostEqual(
            subset.additional_uvs[0][0][0], 0.5, places=3
        )
        self.assertAlmostEqual(
            subset.additional_uvs[1][2][1], 0.8, places=3
        )

    def test_pc_skinned_stride_48_uses_declared_uv_channels(self):
        metadata = bytearray(struct.pack("<IHB", 1, 0, 1))
        metadata.extend(b"bird\0")
        metadata.extend(
            struct.pack(
                "<IIIIBBBBH",
                1,
                3,
                0,
                0,
                48,
                3,
                4,
                1,
                1,
            )
        )
        metadata.extend(struct.pack("<H", 7))
        primary_uvs = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
        secondary_uvs = ((0.2, 0.3), (0.8, 0.3), (0.2, 0.9))
        positions = (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
        )
        vertices = bytearray()
        for position, primary_uv, secondary_uv in zip(
            positions,
            primary_uvs,
            secondary_uvs,
        ):
            vertex = bytearray(48)
            struct.pack_into("<eee", vertex, 0, *position)
            vertex[12] = 255
            struct.pack_into(
                "<eeee",
                vertex,
                24,
                -1.0,
                0.5,
                0.25,
                1.0,
            )
            struct.pack_into(
                "<eeee",
                vertex,
                32,
                0.75,
                -0.5,
                0.1,
                1.0,
            )
            struct.pack_into(
                "<eeee",
                vertex,
                40,
                *primary_uv,
                *secondary_uv,
            )
            vertices.extend(vertex)
        indices = struct.pack("<3H", 0, 1, 2)
        metadata.extend(
            struct.pack("<III", len(vertices), len(indices), 0)
        )

        raw = bytes(metadata + vertices + indices)
        subset = parse_meshdata(raw, "pc").subsets[0]

        self.assertEqual(subset.uv_offsets, (40, 44))
        self.assertEqual(subset.uvs, primary_uvs)
        for actual, expected in zip(
            subset.secondary_uvs,
            secondary_uvs,
        ):
            self.assertAlmostEqual(actual[0], expected[0], places=3)
            self.assertAlmostEqual(actual[1], expected[1], places=3)
        self.assertEqual(subset.additional_uvs, ())
        vehicle_subset = parse_meshdata(
            raw,
            "pc",
            retain_pc_vehicle_uv_candidates=True,
        ).subsets[0]
        self.assertEqual(
            vehicle_subset.uv_offsets,
            (40, 44, 32, 36),
        )
        self.assertEqual(
            vehicle_subset.uv_source,
            "pc_vehicle_shader_route_candidates",
        )

    def test_selects_one_position_format_for_related_subsets(self):
        half_data = bytearray()
        for position in (
            (-10.0, 5.94, 2.4),
            (0.0, 6.01, 2.85),
            (-5.0, 5.98, 2.6),
            (-10.0, -2.0, 0.0),
            (0.0, 6.0, 2.8),
            (-3.0, 1.0, 1.0),
        ):
            half_data.extend(struct.pack("<eee", *position) + b"\0" * 42)
        self.assertEqual(
            _position_format(
                bytes(half_data),
                [(0, 3, 48), (144, 3, 48)],
                "<",
                "AUTO",
            ),
            "HALF",
        )

        float_data = bytearray()
        for position in (
            (-35.0, -0.05, -6.8),
            (35.0, 1.65, 6.8),
            (0.0, 0.0, 0.0),
            (-35.0, -20.0, -7.4),
            (35.0, 0.8, 7.3),
            (0.0, -10.0, 0.0),
        ):
            float_data.extend(struct.pack("<fff", *position) + b"\0" * 52)
        self.assertEqual(
            _position_format(
                bytes(float_data),
                [(0, 3, 64), (192, 3, 64)],
                "<",
                "AUTO",
            ),
            "FLOAT",
        )


class GrannyTests(unittest.TestCase):
    def test_parses_pc_skeleton_hierarchy_and_transforms(self):
        skeleton = parse_granny_model(make_granny_model(), "pc")
        self.assertEqual([bone.name for bone in skeleton.bones], ["root", "child"])
        self.assertEqual(skeleton.bones[0].parent_index, -1)
        self.assertEqual(skeleton.bones[1].parent_index, 0)
        self.assertEqual(skeleton.bones[1].translation, (1.0, 0.0, 0.0))

    def test_parses_xbox360_skeleton_hierarchy_and_transforms(self):
        skeleton = parse_granny_model(
            make_granny_model(endian=">"),
            "xbox360",
        )

        self.assertEqual([bone.name for bone in skeleton.bones], ["root", "child"])
        self.assertEqual(skeleton.bones[0].parent_index, -1)
        self.assertEqual(skeleton.bones[1].parent_index, 0)
        self.assertEqual(skeleton.bones[1].translation, (1.0, 0.0, 0.0))

    def test_parses_constant_animation_curves(self):
        animation = parse_granny_animation(
            make_granny_animation(), "pc", "Synthetic"
        )
        self.assertEqual(animation.name, "Synthetic")
        self.assertAlmostEqual(animation.duration, 1.0)
        self.assertEqual(len(animation.tracks), 1)
        track = animation.tracks[0]
        self.assertEqual(track.name, "root")
        self.assertEqual(track.rotation_at(0.5), (0.0, 0.0, 0.0, 1.0))
        self.assertEqual(
            track.translation.evaluate(0.5), (1.0, 2.0, 3.0)
        )

    def test_parses_xbox360_constant_animation_curves(self):
        animation = parse_granny_animation(
            make_granny_animation(
                endian=">",
                xbox_wrapper_records=(
                    (1, 1, -1.0, 2.0),
                    (2, 2, -0.5, 0.0, 0.5, 1.0),
                    (
                        3,
                        6,
                        -6.0,
                        -5.0,
                        -4.0,
                        -3.0,
                        -2.0,
                        -1.0,
                        1.0,
                        2.0,
                        3.0,
                        4.0,
                        5.0,
                        6.0,
                    ),
                ),
            ),
            "xbox360",
            "SyntheticXbox",
        )

        self.assertEqual(animation.name, "SyntheticXbox")
        self.assertAlmostEqual(animation.duration, 1.0)
        self.assertEqual(len(animation.tracks), 1)
        track = animation.tracks[0]
        self.assertEqual(track.name, "root")
        self.assertEqual(track.rotation_at(0.5), (0.0, 0.0, 0.0, 1.0))
        self.assertEqual(
            track.translation.evaluate(0.5),
            (1.0, 2.0, 3.0),
        )
        self.assertEqual(len(animation.wrapper_records), 3)
        self.assertEqual(animation.wrapper_records[0].bone_index, 1)
        self.assertEqual(animation.wrapper_records[0].component_count, 1)
        self.assertEqual(animation.wrapper_records[0].ranges, ((-1.0, 2.0),))
        self.assertEqual(animation.wrapper_records[1].component_count, 2)
        self.assertEqual(
            animation.wrapper_records[1].ranges,
            ((-0.5, 0.5), (0.0, 1.0)),
        )
        self.assertEqual(animation.wrapper_records[2].component_count, 6)
        self.assertEqual(
            animation.wrapper_records[2].ranges,
            (
                (-6.0, 1.0),
                (-5.0, 2.0),
                (-4.0, 3.0),
                (-3.0, 4.0),
                (-2.0, 5.0),
                (-1.0, 6.0),
            ),
        )

    def test_parses_xbox360_sampled_float_animation_curves(self):
        animation = parse_granny_animation(
            make_sampled_granny_animation(),
            "xbox360",
            "SampledXbox",
        )

        track = animation.tracks[0]
        self.assertEqual(track.rotation.codec, "DaKeyframes32f")
        self.assertEqual(track.rotation.degree, 1)
        self.assertEqual(track.rotation.knots, (0.0, 1.0))
        rotation = track.rotation_at(0.5)
        self.assertEqual(rotation[:2], (0.0, 0.0))
        self.assertAlmostEqual(rotation[2], 2**-0.5)
        self.assertAlmostEqual(rotation[3], 2**-0.5)
        self.assertEqual(
            track.translation.evaluate(0.5),
            (1.0, 2.0, 3.0),
        )
        self.assertEqual(
            track.scale_shear.evaluate(0.5),
            (
                1.5,
                0.0,
                0.0,
                0.0,
                1.5,
                0.0,
                0.0,
                0.0,
                1.5,
            ),
        )

    def test_parses_additive_animation_bone_indices(self):
        animation = parse_granny_animation(
            make_granny_animation((0,)), "pc", "Additive"
        )

        self.assertEqual(animation.additive_bone_indices, (0,))
        self.assertEqual(
            tuple(track.name for track in animation.tracks),
            ("root",),
        )

    def test_parses_and_evaluates_cubic_animation_curve(self):
        animation = parse_granny_animation(
            make_cubic_granny_animation(), "pc", "Cubic"
        )
        curve = animation.tracks[0].translation
        self.assertEqual(curve.degree, 3)
        self.assertEqual(curve.evaluate(0.0), (0.0, 0.0, 0.0))
        self.assertEqual(curve.evaluate(6.0), (60.0, 0.0, 0.0))
        self.assertAlmostEqual(curve.evaluate(3.5)[0], 25.0)

    def test_quadratic_curve_clamps_boundary_control_windows(self):
        curve = GrannyCurve(
            "SyntheticQuadratic",
            2,
            (0.0, 1.0, 2.0, 3.0),
            ((0.0,), (10.0,), (20.0,), (100.0,)),
        )

        self.assertAlmostEqual(curve.evaluate(0.5)[0], 1.25)
        self.assertAlmostEqual(curve.evaluate(2.5)[0], 38.75)
        self.assertGreater(curve.evaluate(2.999)[0], 99.8)
        self.assertEqual(curve.evaluate(3.0), (100.0,))

    def test_rejects_truncated_granny_container(self):
        with self.assertRaises(CorruptDataError):
            parse_granny_model(make_granny_model()[:-1], "pc")


class TextureTests(unittest.TestCase):
    def test_xbox_format_three_uses_dxt1_storage(self):
        header = bytearray(92)
        struct.pack_into(">I", header, 8, 3)
        struct.pack_into(">II", header, 16, 4, 4)
        struct.pack_into(">I", header, 28, 1)
        struct.pack_into(">I", header, 32, 8192)
        converted = convert_texture(
            bytes(header) + b"\0" * 8192,
            "xbox360",
        )
        self.assertEqual(converted.format_name, "DXT1")
        self.assertEqual(converted.dds[84:88], b"DXT1")
        self.assertEqual(converted.dds[128:], b"\0" * 8)

    def test_pc_luminance_texture_to_dds(self):
        header = bytearray(92)
        struct.pack_into("<I", header, 8, 10)
        struct.pack_into("<II", header, 16, 2, 2)
        struct.pack_into("<I", header, 28, 1)
        struct.pack_into("<I", header, 32, 4)
        converted = convert_texture(bytes(header) + b"\x00\x40\x80\xff", "pc")
        self.assertEqual((converted.width, converted.height), (2, 2))
        self.assertEqual(converted.dds[:4], b"DDS ")
        self.assertEqual(converted.dds[128:], b"\x00\x40\x80\xff")

    def test_creates_luminance_dds_from_decoded_pixels(self):
        dds = create_luminance_dds(2, 2, b"\x00\x01\x02\x03")
        self.assertEqual(dds[:4], b"DDS ")
        self.assertEqual(dds[128:], b"\x00\x01\x02\x03")

    def test_xbox_luminance_texture_is_untiled(self):
        width = height = 64
        linear = bytes(index & 0xFF for index in range(width * height))
        tiled = bytearray(len(linear))
        for source_index in range(len(tiled)):
            x = _tiled_x(source_index, width, 1)
            y = _tiled_y(source_index, width, 1)
            if x < width and y < height:
                tiled[source_index] = linear[y * width + x]
        header = bytearray(156)
        header[:17] = b"RES\x02XenonTexture\0"
        struct.pack_into(">I", header, 72, 10)
        struct.pack_into(">II", header, 80, width, height)
        struct.pack_into(">I", header, 92, 1)
        struct.pack_into(">I", header, 96, len(tiled))
        converted = convert_texture(bytes(header) + bytes(tiled), "xbox360")
        self.assertEqual(converted.dds[128:], linear)

    def test_xbox_grouped_mips_imports_base_level(self):
        width = height = 64
        linear = bytes(index & 0xFF for index in range(width * height))
        tiled = bytearray(len(linear))
        for source_index in range(len(tiled)):
            x = _tiled_x(source_index, width, 1)
            y = _tiled_y(source_index, width, 1)
            if x < width and y < height:
                tiled[source_index] = linear[y * width + x]
        allocation = bytes(tiled) + b"\xCC" * len(tiled)
        header = bytearray(92)
        struct.pack_into(">I", header, 8, 10)
        struct.pack_into(">II", header, 16, width, height)
        struct.pack_into(">I", header, 28, 7)
        struct.pack_into(">I", header, 32, len(allocation))
        converted = convert_texture(
            bytes(header) + allocation, "xbox360"
        )
        self.assertEqual(converted.mip_count, 1)
        self.assertEqual(converted.dds[128:], linear)

    @unittest.skipUnless(
        texture._np is not None, "NumPy is unavailable"
    )
    def test_numpy_texture_paths_match_python_fallback(self):
        width = height = 64
        for pitch in (1, 4, 8, 16):
            with self.subTest(pitch=pitch):
                tiled = bytes(
                    index & 0xFF
                    for index in range(width * height * pitch)
                )
                accelerated = texture._untile(
                    tiled, width, height, pitch
                )
                with mock.patch.object(texture, "_np", None):
                    fallback = texture._untile(
                        tiled, width, height, pitch
                    )
                self.assertEqual(accelerated, fallback)
        swapped = bytes(index & 0xFF for index in range(4096))
        accelerated_swap = texture._swap_16(swapped)
        with mock.patch.object(texture, "_np", None):
            fallback_swap = texture._swap_16(swapped)
        self.assertEqual(accelerated_swap, fallback_swap)

    @unittest.skipUnless(
        texture._np is not None, "NumPy is unavailable"
    )
    def test_chunked_numpy_untile_matches_python_fallback(self):
        width, height = 1024, 512
        tiled = bytes(index & 0xFF for index in range(width * height))
        accelerated = texture._untile(tiled, width, height, 1)
        with mock.patch.object(texture, "_np", None):
            fallback = texture._untile(tiled, width, height, 1)
        self.assertEqual(accelerated, fallback)


class TerrainTests(unittest.TestCase):
    def test_heightfield_downsamples_pc_and_xbox_headers(self):
        cases = (
            ("pc", "<", "<"),
            ("xbox360", ">", ">"),
        )
        for platform, header_endian, sample_endian in cases:
            with self.subTest(platform=platform):
                terrain = parse_heightfield(
                    make_heightfield(header_endian, sample_endian),
                    target_resolution=4,
                    platform=platform,
                )
                self.assertEqual(terrain.source_resolution, 4)
                self.assertEqual(terrain.header_endian, header_endian)
                self.assertEqual(terrain.sample_endian, sample_endian)
                self.assertEqual(len(terrain.positions), 16)
                self.assertEqual(len(terrain.faces), 18)
                self.assertEqual(tuple(terrain.faces[0]), (0, 4, 5))
                self.assertEqual(tuple(terrain.faces[1]), (0, 5, 1))

    def test_heightfield_uses_requested_even_sample_count(self):
        terrain = parse_heightfield(
            make_heightfield("<", "<"),
            target_resolution=3,
            platform="pc",
        )
        self.assertEqual(len(terrain.positions), 9)
        self.assertEqual(len(terrain.faces), 8)
        self.assertEqual(tuple(terrain.positions[-1][:2]), (1.0, 1.0))

    def test_parses_terrain_material_map_byte_orders(self):
        for endian, platform in (("<", "pc"), (">", "xbox360")):
            with self.subTest(platform=platform):
                material_map = parse_material_map(
                    make_terrain_material_map(endian),
                    platform,
                )
                self.assertEqual(material_map.side, 512)
                self.assertEqual(
                    material_map.names,
                    ("defaultmaterial", "Grass"),
                )
                self.assertEqual(
                    material_map.indices[:4],
                    b"\x00\x01\x00\x01",
                )
                second_tile = 256 * material_map.side
                self.assertEqual(
                    material_map.indices[second_tile:second_tile + 4],
                    b"\x01\x00\x01\x00",
                )

    def test_rejects_corrupt_terrain_material_row_lengths(self):
        data = bytearray(make_terrain_material_map("<"))
        first_row_size = 6 + 4 + 768
        struct.pack_into("<H", data, first_row_size, 4)
        with self.assertRaises(CorruptDataError):
            parse_material_map(bytes(data), "pc")


class DbxTests(unittest.TestCase):
    def test_character_animation_links_separate_shared_tree(self):
        mesh = element(
            "complex",
            element(
                "field",
                name="Mesh",
                ref="Characters/Jap/Jap_Mesh/MESH-GUID",
            ),
        )
        entity = element(
            "instance",
            element("array", mesh, name="Meshes3p"),
            element("field", name="AnimTree3p", ref="TREE"),
            guid="ROOT",
            type="GameSharedResources.SoldierEntityData",
            id="JapaneseInfantry",
        )
        tree = element(
            "instance",
            element("field", name="FacialAnimations", ref="FACIAL"),
            element(
                "field",
                name="KitJointsAnimTree",
                ref="Characters/AnimTree/JapKit/KIT-GUID",
            ),
            element("field", name="ReviveAnimTree", ref="null"),
            element(
                "field",
                name="StaticPose",
                ref="Characters/Jap/Jap_SkeletonPose/POSE-GUID",
            ),
            element(
                "field",
                name="Shared",
                ref="Characters/AnimTree/Shared/SHARED-GUID",
            ),
            guid="TREE",
            type="GameSharedResources.SoldierAnimationTreeData",
        )
        facial = element(
            "instance",
            element(
                "field",
                name="AnimTree",
                ref="Characters/AnimTree/JapFacial/FACIAL-GUID",
            ),
            guid="FACIAL",
            type="GameSharedResources.IndexedAnimationData",
        )
        document = DbxDocument(
            element(
                "partition",
                entity,
                tree,
                facial,
                guid="PARTITION",
                primaryInstance="ROOT",
            )
        )

        links = character_animation_links(
            document,
            "characters/jap/jap_mesh.res",
        )

        self.assertIsNotNone(links)
        self.assertEqual(
            links.animation_references,
            ("Characters/Jap/Jap_SkeletonPose/POSE-GUID",),
        )
        self.assertEqual(
            links.animation_tree_references,
            (
                "Characters/AnimTree/JapKit/KIT-GUID",
                "Characters/AnimTree/JapFacial/FACIAL-GUID",
            ),
        )
        self.assertEqual(
            links.shared_animation_tree_references,
            ("Characters/AnimTree/Shared/SHARED-GUID",),
        )
        self.assertIsNone(
            character_animation_links(
                document,
                "Characters/Jap/Other_Mesh.res",
            )
        )
        assembly = modular_assembly(
            document,
            "Objects/Soldiers/JapaneseInfantry.dbx",
        )
        self.assertEqual(assembly.animation_links, links)

    def test_animation_tree_clips_use_explicit_lists(self):
        result = element(
            "instance",
            element(
                "array",
                element(
                    "item",
                    ref="Animations/Faces/DeadPose/DEAD-GUID",
                ),
                element(
                    "item",
                    ref="Characters/Jap/FacePose/FACE-GUID",
                ),
                name="AnimationList",
            ),
            element(
                "array",
                element(
                    "item",
                    ref="Animations/Faces/DeadPose/DEAD-GUID",
                ),
                name="ReplacmentAnimations",
            ),
            element(
                "field",
                name="Skeleton",
                ref="Characters/Skeletons/ske01/SKELETON-GUID",
            ),
            guid="RESULT",
            type="Animation.ResultAnimTreeData",
        )
        document = DbxDocument(
            element(
                "partition",
                result,
                guid="PARTITION",
                primaryInstance="RESULT",
            )
        )

        self.assertEqual(
            animation_tree_clip_references(document),
            (
                "Animations/Faces/DeadPose/DEAD-GUID",
                "Characters/Jap/FacePose/FACE-GUID",
            ),
        )
        self.assertEqual(
            animation_tree_replacement_references(document),
            ("Animations/Faces/DeadPose/DEAD-GUID",),
        )

    def test_shared_animation_tree_uses_explicit_base_set(self):
        shared = element(
            "instance",
            element(
                "array",
                element(
                    "item",
                    ref="Characters/AnimTree/SoldierAnimTree3pRif/RIF-GUID",
                ),
                element(
                    "item",
                    ref="Characters/AnimTree/SoldierAnimTree3pHg/HG-GUID",
                ),
                name="BaseSetAnimTrees",
            ),
            element(
                "field",
                name="DeathAnimTree",
                ref="Characters/AnimTree/DeathAnimTree3p/DEATH-GUID",
            ),
            guid="SHARED",
            type="GameSharedResources.SharedSoldierAnimationTreeData",
        )
        document = DbxDocument(
            element(
                "partition",
                shared,
                guid="PARTITION",
                primaryInstance="SHARED",
            )
        )

        self.assertEqual(
            animation_tree_base_set_references(document),
            (
                "Characters/AnimTree/SoldierAnimTree3pRif/RIF-GUID",
                "Characters/AnimTree/SoldierAnimTree3pHg/HG-GUID",
            ),
        )


    def test_character_modular_assembly_uses_meshes3p(self):
        body = element(
            "complex",
            element(
                "field",
                name="Mesh",
                ref="Characters/US_01_Body_Mesh/BODY-GUID",
            ),
            element(
                "field",
                name="MeshShaderSet",
                ref="Characters/US_01_Body_Shaders/SHADER-GUID",
            ),
            element("field", name="RandomGroup", value="-1"),
        )
        head = element(
            "complex",
            element(
                "field",
                name="Mesh",
                ref="Characters/US_01_Head_Mesh/HEAD-GUID",
            ),
            element("field", name="RandomGroup", value="0"),
        )
        instance = element(
            "instance",
            element("array", body, head, name="Meshes3p"),
            guid="ROOT",
            type="GameSharedResources.SoldierEntityData",
            id="US_Assault",
        )
        document = DbxDocument(
            element(
                "partition",
                instance,
                guid="PARTITION",
                primaryInstance="ROOT",
            )
        )

        assembly = modular_assembly(
            document,
            "Objects/Soldiers/US/Assault.dbx",
        )

        self.assertIsNotNone(assembly)
        self.assertEqual(assembly.kind, "CHARACTER")
        self.assertEqual(assembly.name, "US_Assault")
        self.assertEqual(
            tuple(component.role for component in assembly.components),
            ("BODY", "HEAD"),
        )
        self.assertEqual(assembly.components[0].random_group, -1)
        self.assertEqual(assembly.components[1].random_group, 0)
        self.assertEqual(
            assembly.components[0].mesh_set_path,
            "Characters/US_01_Body_Mesh.res",
        )
        self.assertEqual(
            assembly.components[0].shader_set_reference,
            "Characters/US_01_Body_Shaders/SHADER-GUID",
        )

    def test_vehicle_modular_assembly_uses_composite_mesh(self):
        instance = element(
            "instance",
            element(
                "field",
                name="Mesh",
                ref="Objects/Vehicles/Air/AH60/AH60_Mesh/MESH-GUID",
            ),
            guid="ROOT",
            type="GameSharedResources.VehicleEntityData",
            id="AH60",
        )
        document = DbxDocument(
            element(
                "partition",
                instance,
                guid="PARTITION",
                primaryInstance="ROOT",
            )
        )

        assembly = modular_assembly(
            document,
            "Objects/Vehicles/Air/AH60/AH60.dbx",
        )

        self.assertIsNotNone(assembly)
        self.assertEqual(assembly.kind, "VEHICLE")
        self.assertEqual(len(assembly.components), 1)
        self.assertEqual(assembly.components[0].role, "VEHICLE")
        self.assertEqual(
            assembly.components[0].mesh_set_path,
            "Objects/Vehicles/Air/AH60/AH60_Mesh.res",
        )

    def test_vehicle_entity_path_uses_same_name_mesh_convention(self):
        self.assertEqual(
            vehicle_entity_path_for_mesh_set(
                "Objects/Vehicles/Land/T90/T90_Mesh.res"
            ),
            "Objects/Vehicles/Land/T90/T90.dbx",
        )
        self.assertEqual(
            vehicle_entity_path_for_mesh_set(
                "objects/vehicles/air/zero_fighter/"
                "zero_fighter_mesh.RES"
            ),
            "objects/vehicles/air/zero_fighter/zero_fighter.dbx",
        )
        self.assertIsNone(
            vehicle_entity_path_for_mesh_set(
                "Characters/US/US_Scout_Mesh.res"
            )
        )
        self.assertIsNone(
            vehicle_entity_path_for_mesh_set(
                "Objects/Vehicles/Land/T90/"
                "T90_Mesh_lod0_data.res"
            )
        )

    def test_vehicle_modular_assembly_preserves_track_instances(self):
        entity = element(
            "instance",
            element(
                "field",
                name="Mesh",
                ref="Objects/Vehicles/Land/BMD3/BMD3_Mesh/MESH-GUID",
            ),
            element(
                "array",
                element("item", ref="CHASSIS"),
                name="Components",
            ),
            guid="ROOT",
            type="GameSharedResources.VehicleEntityData",
            id="Bmd3",
        )
        chassis = element(
            "instance",
            element(
                "array",
                element("item", ref="TRACK-RIGHT"),
                element("item", ref="TRACK-LEFT"),
                name="Components",
            ),
            guid="CHASSIS",
            type="GameSharedResources.VehicleComponentData",
        )

        def track(guid, name, x):
            return element(
                "instance",
                element("array", name="Components"),
                element("field", name="Name", value=name),
                element(
                    "complex",
                    name="Transform",
                    value=(
                        "1/0/0/0/0/1/0/0/0/0/1/0/"
                        f"{x}/0.25/-0.1/0"
                    ),
                ),
                element(
                    "field",
                    name="Mesh",
                    ref=(
                        "Objects/Vehicles/Land/BMD3/"
                        "BMD3Tracks_Mesh/TRACK-MESH-GUID"
                    ),
                ),
                element(
                    "field",
                    name="Skeleton",
                    ref=(
                        "Objects/Vehicles/Land/BMD3/"
                        "BMD3Tracks_Skeleton/SKELETON-GUID"
                    ),
                ),
                guid=guid,
                type="GameSharedResources.TrackComponentData",
            )

        document = DbxDocument(
            element(
                "partition",
                entity,
                chassis,
                track("TRACK-RIGHT", "Track Right", -1.5),
                track("TRACK-LEFT", "Track Left", 1.5),
                guid="PARTITION",
                primaryInstance="ROOT",
            )
        )

        assembly = modular_assembly(
            document,
            "Objects/Vehicles/Land/BMD3/BMD3.dbx",
        )

        self.assertIsNotNone(assembly)
        self.assertEqual(
            tuple(component.role for component in assembly.components),
            ("VEHICLE", "TRACK", "TRACK"),
        )
        right, left = assembly.components[1:]
        self.assertEqual((right.name, left.name), ("Track Right", "Track Left"))
        self.assertEqual(right.mesh_set_path, left.mesh_set_path)
        self.assertEqual(
            right.mesh_set_path,
            "Objects/Vehicles/Land/BMD3/BMD3Tracks_Mesh.res",
        )
        self.assertEqual(
            right.skeleton_reference,
            (
                "Objects/Vehicles/Land/BMD3/"
                "BMD3Tracks_Skeleton/SKELETON-GUID"
            ),
        )
        self.assertEqual(right.transform.translation, (-1.5, 0.25, -0.1))
        self.assertEqual(left.transform.translation, (1.5, 0.25, -0.1))
        self.assertEqual(right.source_guid, "TRACK-RIGHT")
        self.assertEqual(
            right.source_type,
            "GameSharedResources.TrackComponentData",
        )

    def test_vehicle_parts_use_health_and_model_states(self):
        entity = element(
            "instance",
            element(
                "field",
                name="Mesh",
                ref="Objects/Vehicles/Air/AH60/AH60_Mesh/MESH-GUID",
            ),
            element(
                "array",
                element("item", ref="COMPONENT"),
                name="Components",
            ),
            guid="ROOT",
            type="GameSharedResources.VehicleEntityData",
            id="AH60",
        )
        component = element(
            "instance",
            element(
                "array",
                element("item", ref="FUSELAGE"),
                element("item", ref="ROTOR"),
                element("item", ref="WRECK"),
                name="Components",
            ),
            guid="COMPONENT",
            type="GameSharedResources.VehicleComponentData",
        )
        fuselage = element(
            "instance",
            element(
                "array",
                element("item", ref="HEALTHY"),
                element("item", ref="DAMAGED"),
                name="HealthStates",
            ),
            guid="FUSELAGE",
            type="GameSharedResources.PartComponentData",
            id="Fuselage",
        )
        healthy = element(
            "instance",
            element("field", name="Health", value="100"),
            element("field", name="MeshName", value="MainFuselage"),
            guid="HEALTHY",
            type="GameSharedResources.VehicleHealthStateData",
        )
        damaged = element(
            "instance",
            element("field", name="Health", value="40"),
            element("field", name="MeshName", value="MainFuselageDamaged"),
            guid="DAMAGED",
            type="GameSharedResources.VehicleHealthStateData",
        )
        rotor = element(
            "instance",
            element(
                "complex",
                element("field", name="MeshName", value="Rotor"),
                name="LowRpmModel",
            ),
            element(
                "complex",
                element("field", name="MeshName", value="RotorBlurred"),
                name="HighRpmModel",
            ),
            guid="ROTOR",
            type="GameSharedResources.RotorComponentData",
            id="MainRotor",
        )
        wreck = element(
            "instance",
            element(
                "array",
                element("item", ref="WRECK-STATE"),
                name="HealthStates",
            ),
            guid="WRECK",
            type="GameSharedResources.PartComponentData",
            id="Nose",
        )
        wreck_state = element(
            "instance",
            element("field", name="Health", value="100"),
            element("field", name="MeshName", value="NoseWreck"),
            guid="WRECK-STATE",
            type="GameSharedResources.VehicleHealthStateData",
        )
        document = DbxDocument(
            element(
                "partition",
                entity,
                component,
                fuselage,
                healthy,
                damaged,
                rotor,
                wreck,
                wreck_state,
                guid="PARTITION",
                primaryInstance="ROOT",
            )
        )

        assembly = modular_assembly(
            document,
            "Objects/Vehicles/Air/AH60/AH60.dbx",
        )

        states = {part.name: part.state for part in assembly.parts}
        self.assertEqual(states["MainFuselage"], "DEFAULT")
        self.assertEqual(states["MainFuselageDamaged"], "DAMAGED")
        self.assertEqual(states["Rotor"], "DEFAULT")
        self.assertEqual(states["RotorBlurred"], "ALTERNATE")
        self.assertEqual(states["NoseWreck"], "DEFAULT")

    def test_vehicle_part_state_classifies_name_fallbacks(self):
        self.assertEqual(vehicle_part_state("MainFuselage"), "DEFAULT")
        self.assertEqual(vehicle_part_state("EngineDamaged"), "DAMAGED")
        self.assertEqual(vehicle_part_state("RotorBlurred"), "ALTERNATE")
        self.assertEqual(vehicle_part_state("WreckPartTail"), "DEFAULT")
        self.assertEqual(
            vehicle_part_state("WreckPartTail_Wreck"),
            "DESTROYED",
        )
        self.assertEqual(
            vehicle_part_state("MainFuselageDestroyed"),
            "DESTROYED",
        )

    def test_binary_dbx_to_typed_tree_and_xml(self):
        strings = (b"\0", b"partition\0", b"guid\0", b"ABC\0")
        offsets = []
        table = bytearray()
        for value in strings:
            offsets.append(len(table))
            table.extend(value)
        relocation_offset = len(offsets) * 4 + len(table)
        data = bytearray(b"{binary}")
        data.extend(struct.pack(">IIII", 0, 0, relocation_offset, len(offsets)))
        data.extend(struct.pack(">" + "I" * len(offsets), *offsets))
        data.extend(table)
        data.extend(b"\x01\xA1\x02\x03\x00\x00")
        document = parse_dbx(bytes(data))
        self.assertEqual(document.guid, "ABC")
        self.assertEqual(document.root.tag, "partition")
        self.assertEqual(document.root.attribute("guid"), "ABC")
        self.assertEqual(document.instances, ())
        xml = dbx_to_xml(bytes(data))
        self.assertIn('<partition guid="ABC">', xml)
        self.assertIn("</partition>", xml)


class ShaderDatabaseTests(unittest.TestCase):
    def test_parses_rdef_texture_parameter_names(self):
        data = bytearray(32 + 64)
        struct.pack_into("<4I", data, 0, 0, 0, 2, 32)
        normal_offset = len(data)
        data.extend(b"texture_NormalMap\0")
        color_offset = len(data)
        data.extend(b"texture_Texture\0")
        struct.pack_into(
            "<8I",
            data,
            32,
            normal_offset,
            2,
            0,
            0,
            0,
            2,
            1,
            0,
        )
        struct.pack_into(
            "<8I",
            data,
            64,
            color_offset,
            2,
            0,
            0,
            0,
            3,
            1,
            0,
        )
        self.assertEqual(
            _rdef_texture_parameters(bytes(data)),
            {
                2: ("texture_NormalMap",),
                3: ("texture_Texture",),
            },
        )
        self.assertEqual(
            tuple(
                (binding.index, binding.name, binding.dimension)
                for binding in rdef_texture_bindings(bytes(data))
            ),
            (
                (2, "texture_NormalMap", 0),
                (3, "texture_Texture", 0),
            ),
        )

    def test_parses_material_texture_bindings(self):
        database = parse_shader_database(make_shader_database())
        self.assertEqual(database.trailing_bytes, 0)
        self.assertEqual(len(database.materials), 1)
        material = database.material(
            "objects/test/mesh/shaders/surface"
        )
        self.assertIsNotNone(material)
        self.assertEqual(
            tuple(
                (texture.index, texture.path)
                for texture in material.textures
            ),
            (
                (3, "Textures/Test/Color"),
            ),
        )
        self.assertEqual(len(material.variants), 1)
        self.assertEqual(material.variants[0].layout.stride, 12)
        self.assertEqual(
            tuple(
                (field.code, field.offset)
                for field in material.variants[0].layout.fields
            ),
            ((259, 0),),
        )

    def test_parses_xbox_shader_material_bindings(self):
        database = parse_shader_database(
            make_xbox_shader_database(trailing=b"\xA5"),
            platform="xbox360",
            asset_type="Dx9ShaderDatabase",
        )
        self.assertEqual(database.trailing_bytes, 1)
        self.assertEqual(
            database.material_keys,
            ("Objects/Test/Mesh/Shaders/Surface",),
        )
        material = database.material(
            "objects/test/mesh/shaders/surface"
        )
        self.assertIsNotNone(material)
        self.assertEqual(len(material.variants), 1)
        self.assertEqual(material.variants[0].layout.stride, 36)
        self.assertEqual(
            material.variants[0].layout.uv_offsets,
            (24, 28, 32),
        )
        self.assertEqual(
            tuple(layout.uv_offsets for layout in database.layouts),
            ((24, 28, 32),),
        )
        self.assertEqual(
            tuple(
                (
                    texture.index,
                    texture.slot,
                    texture.path,
                    texture.parameters,
                    texture.dimension,
                )
                for texture in material.textures
            ),
            (
                (
                    0x104,
                    4,
                    "Textures/Test/XboxColor",
                    (),
                    None,
                ),
            ),
        )

    def test_rejects_unsafe_xbox_shader_count(self):
        data = bytearray(make_xbox_shader_database())
        struct.pack_into(">I", data, 5, 1_000_001)
        with self.assertRaisesRegex(
            CorruptDataError,
            "Unsafe Xbox shader parameter-group count",
        ):
            parse_shader_database(
                bytes(data),
                platform="xbox360",
                asset_type="Dx9ShaderDatabase",
            )

    def test_rejects_xbox_shader_program_index(self):
        with self.assertRaisesRegex(
            CorruptDataError,
            "Xbox vertex shader index is out of range",
        ):
            parse_shader_database(
                make_xbox_shader_database(vertex_shader_index=1),
                platform="xbox360",
                asset_type="Dx9ShaderDatabase",
            )

    def test_rejects_xbox_shader_program_key_count(self):
        with self.assertRaisesRegex(
            CorruptDataError,
            "programs but 0 program keys",
        ):
            parse_shader_database(
                make_xbox_shader_database(key_count=0),
                platform="xbox360",
                asset_type="Dx9ShaderDatabase",
            )

    def test_selective_shader_parse_skips_unrelated_variants(self):
        raw = make_shader_database()
        with mock.patch(
            "bc2_frostbite_importer.core.shaderdb.shader_resources",
            side_effect=AssertionError(
                "Unselected shader variants were resolved"
            ),
        ):
            database = parse_shader_database(
                raw,
                material_keys=("Objects/Other/Shaders/Missing",),
            )
        self.assertEqual(database.materials, ())
        self.assertEqual(
            database.material_keys,
            ("Objects/Test/Mesh/Shaders/Surface",),
        )

        selected = parse_shader_database(
            raw,
            material_keys=(),
            shader_partitions=("objects/test/mesh/shaders/surface",),
        )
        self.assertEqual(len(selected.materials), 1)
        self.assertEqual(len(selected.materials[0].variants), 1)

    def test_traces_texture_coordinates_to_physical_uv_offset(self):
        vertex_shader = _dxbc_program(
            _dxbc_instruction(
                54,
                _dxbc_operand(2, 6, mask=0x3),
                _dxbc_operand(1, 2, swizzle=(2, 3, 2, 3)),
            ),
            _dxbc_signature("TEXCOORD", 3, 2),
        )
        pixel_shader = _dxbc_program(
            (
                *_dxbc_instruction(
                    56,
                    _dxbc_operand(0, 0, mask=0x3),
                    _dxbc_operand(1, 6, swizzle=(0, 1, 0, 1)),
                    _dxbc_immediate(8.0, 8.0),
                ),
                *_dxbc_instruction(
                    69,
                    _dxbc_operand(0, 1, mask=0xF),
                    _dxbc_operand(0, 0, swizzle=(0, 1, 0, 1)),
                    _dxbc_operand(7, 3, mask=0),
                    _dxbc_operand(6, 0, mask=0),
                ),
            )
        )
        layout = _vertex_layout(
            b"L" * 16,
            48,
            0xFFFFFF00,
            (263, 1797, 1032, 1288, 6, 6, 6, 524, 6, 6),
        )
        self.assertEqual(
            texture_coordinate_routes(
                vertex_shader,
                pixel_shader,
                layout.uv_offset,
            ),
            (
                DxbcCoordinateRoute(
                    texture_index=3,
                    uv_offset=28,
                    source_semantic_index=3,
                    source_components=(2, 3),
                    scale=(8.0, 8.0),
                    offset=(0.0, 0.0),
                ),
            ),
        )

    def test_preserves_uv_source_across_zeroed_shader_branch(self):
        vertex_shader = _dxbc_program(
            (
                *_dxbc_instruction(
                    31,
                    _dxbc_operand(1, 0, swizzle=(0, 0, 0, 0)),
                ),
                *_dxbc_instruction(
                    54,
                    _dxbc_operand(2, 6, mask=0x3),
                    _dxbc_operand(1, 2, swizzle=(2, 3, 2, 3)),
                ),
                *_dxbc_instruction(18),
                *_dxbc_instruction(
                    54,
                    _dxbc_operand(2, 6, mask=0x3),
                    _dxbc_immediate(0.0, 0.0),
                ),
                *_dxbc_instruction(21),
            ),
            _dxbc_signature("TEXCOORD", 3, 2),
        )
        pixel_shader = _dxbc_program(
            _dxbc_instruction(
                69,
                _dxbc_operand(0, 1, mask=0xF),
                _dxbc_operand(1, 6, swizzle=(0, 1, 0, 1)),
                _dxbc_operand(7, 3, mask=0),
                _dxbc_operand(6, 0, mask=0),
            )
        )
        layout = _vertex_layout(
            b"layout-reference",
            52,
            0xFFFFFF00,
            (263, 1797, 1032, 1288, 6, 6, 6, 524, 6, 6),
        )

        routes = texture_coordinate_routes(
            vertex_shader,
            pixel_shader,
            layout.uv_offset,
        )

        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].texture_index, 3)
        self.assertEqual(routes[0].uv_offset, 28)
        self.assertEqual(routes[0].scale, (None, None))
        self.assertEqual(routes[0].offset, (None, None))

    def test_applies_shader_operand_modifier_to_uv_transform(self):
        vertex_shader = _dxbc_program(
            _dxbc_instruction(
                54,
                _dxbc_operand(2, 6, mask=0x3),
                _dxbc_operand(
                    1,
                    2,
                    swizzle=(2, 3, 2, 3),
                    modifier=1,
                ),
            ),
            _dxbc_signature("TEXCOORD", 3, 2),
        )
        pixel_shader = _dxbc_program(
            _dxbc_instruction(
                69,
                _dxbc_operand(0, 1, mask=0xF),
                _dxbc_operand(1, 6, swizzle=(0, 1, 0, 1)),
                _dxbc_operand(7, 3, mask=0),
                _dxbc_operand(6, 0, mask=0),
            )
        )
        layout = _vertex_layout(
            b"layout-reference",
            52,
            0xFFFFFF00,
            (263, 1797, 1032, 1288, 6, 6, 6, 524, 6, 6),
        )

        routes = texture_coordinate_routes(
            vertex_shader,
            pixel_shader,
            layout.uv_offset,
        )

        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].scale, (-1.0, -1.0))
        self.assertEqual(routes[0].offset, (-0.0, -0.0))

    def test_preserves_uv_source_through_non_affine_operation(self):
        vertex_shader = _dxbc_program(
            _dxbc_instruction(
                54,
                _dxbc_operand(2, 6, mask=0x3),
                _dxbc_operand(1, 2, swizzle=(2, 3, 2, 3)),
            ),
            _dxbc_signature("TEXCOORD", 3, 2),
        )
        pixel_shader = _dxbc_program(
            (
                *_dxbc_instruction(
                    26,
                    _dxbc_operand(0, 0, mask=0x3),
                    _dxbc_operand(1, 6, swizzle=(0, 1, 0, 1)),
                ),
                *_dxbc_instruction(
                    69,
                    _dxbc_operand(0, 1, mask=0xF),
                    _dxbc_operand(0, 0, swizzle=(0, 1, 0, 1)),
                    _dxbc_operand(7, 3, mask=0),
                    _dxbc_operand(6, 0, mask=0),
                ),
            )
        )
        layout = _vertex_layout(
            b"layout-reference",
            52,
            0xFFFFFF00,
            (263, 1797, 1032, 1288, 6, 6, 6, 524, 6, 6),
        )

        routes = texture_coordinate_routes(
            vertex_shader,
            pixel_shader,
            layout.uv_offset,
        )

        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].uv_offset, 28)
        self.assertEqual(routes[0].scale, (None, None))
        self.assertEqual(routes[0].offset, (None, None))

    def test_discards_stale_temp_provenance_after_unknown_opcode(self):
        vertex_shader = _dxbc_program(
            _dxbc_instruction(
                54,
                _dxbc_operand(2, 6, mask=0x3),
                _dxbc_operand(1, 2, swizzle=(2, 3, 2, 3)),
            ),
            _dxbc_signature("TEXCOORD", 3, 2),
        )
        pixel_shader = _dxbc_program(
            (
                *_dxbc_instruction(
                    54,
                    _dxbc_operand(0, 0, mask=0x3),
                    _dxbc_operand(1, 6, swizzle=(0, 1, 0, 1)),
                ),
                *_dxbc_instruction(
                    15,
                    _dxbc_operand(0, 1, mask=0x1),
                    _dxbc_operand(0, 2, swizzle=(0, 1, 0, 1)),
                    _dxbc_operand(0, 3, swizzle=(0, 1, 0, 1)),
                ),
                *_dxbc_instruction(
                    69,
                    _dxbc_operand(0, 1, mask=0xF),
                    _dxbc_operand(0, 0, swizzle=(0, 1, 0, 1)),
                    _dxbc_operand(7, 3, mask=0),
                    _dxbc_operand(6, 0, mask=0),
                ),
            )
        )
        layout = _vertex_layout(
            b"layout-reference",
            52,
            0xFFFFFF00,
            (263, 1797, 1032, 1288, 6, 6, 6, 524, 6, 6),
        )

        self.assertEqual(
            texture_coordinate_routes(
                vertex_shader,
                pixel_shader,
                layout.uv_offset,
            ),
            (),
        )


class LevelTests(unittest.TestCase):
    @staticmethod
    def _transform(x: float, y: float, z: float) -> tuple[float, ...]:
        values = [0.0] * 16
        values[0] = 1.0
        values[5] = 1.0
        values[10] = 1.0
        values[12] = x
        values[13] = y
        values[14] = z
        values[15] = 1.0
        return tuple(values)

    def test_transform_converts_translation_and_basis(self):
        transform = FrostbiteTransform(self._transform(2.0, 3.0, 4.0))
        self.assertEqual(transform.translation, (2.0, 3.0, 4.0))
        self.assertEqual(
            transform.blender_translation(unit_scale=2.0),
            (4.0, -8.0, 6.0),
        )
        self.assertEqual(
            transform.blender_matrix(),
            (
                (1.0, 0.0, 0.0, 2.0),
                (0.0, 1.0, 0.0, -4.0),
                (0.0, 0.0, 1.0, 3.0),
                (0.0, 0.0, 0.0, 1.0),
            ),
        )

    def test_sparse_shader_override_merges_with_default_set(self):
        def surface(name: str, reference: str) -> DbxElement:
            return element(
                "complex",
                element("field", value=name, name="Name"),
                element("field", name="Shader", ref=reference),
            )

        mesh = element(
            "instance",
            element("field", value="Objects/Test/Mesh", name="Name"),
            element(
                "array",
                element("item", ref="BASE"),
                element("item", ref="OVERRIDE"),
                name="ShaderSets",
            ),
            guid="MESH",
        )
        base = element(
            "instance",
            element(
                "array",
                surface("Wall", "Objects/Test/Shaders/Wall/GUID"),
                surface("Metal", "Objects/Test/Shaders/Metal/GUID"),
                name="SurfaceShaders",
            ),
            guid="BASE",
        )
        override = element(
            "instance",
            element(
                "array",
                surface(
                    "Metal",
                    "Objects/Test/Shaders/Cheap_Metal/GUID",
                ),
                name="SurfaceShaders",
            ),
            guid="OVERRIDE",
        )
        document = DbxDocument(
            element(
                "partition",
                mesh,
                base,
                override,
                guid="PARTITION",
            )
        )
        repository = SimpleNamespace(
            catalog=SimpleNamespace(
                find_mesh_data=lambda *_: {
                    "path": "Objects/Test/Mesh_lod0_data.res"
                }
            )
        )
        resolver = LevelResolver(repository, "TEST")
        with mock.patch.object(
            resolver,
            "_document",
            return_value=document,
        ):
            definition = resolver._mesh(
                "Objects/Test/Mesh/MESH",
                "OVERRIDE",
            )
        self.assertEqual(
            [surface.name for surface in definition.surfaces],
            ["Wall", "Metal"],
        )
        self.assertTrue(
            definition.surfaces[1].reference.endswith(
                "/Cheap_Metal/GUID"
            )
        )

    def _group_documents(self, second_spawn: int = 9):
        mesh_reference = "Objects/Test/Mesh/GUID"
        group = element(
            "instance",
            element(
                "array",
                element("item", ref="MEMBER"),
                name="MemberDatas",
            ),
            guid="GROUP",
            type="GameSharedResources.StaticModelGroupEntityData",
        )
        member = element(
            "instance",
            element("field", name="MeshEntityType", ref="ENTITY"),
            element("field", value="Test member", name="Name"),
            element(
                "array",
                element(
                    "complex",
                    element("field", value="7", name="Enumeration"),
                ),
                element(
                    "complex",
                    element(
                        "field",
                        value=str(second_spawn),
                        name="Enumeration",
                    ),
                ),
                name="SpawnDatas",
            ),
            guid="MEMBER",
        )
        mesh_entity = element(
            "instance",
            element("field", name="Mesh", ref=mesh_reference),
            guid="ENTITY",
            type="GameSharedResources.StaticModelEntityData",
        )
        level = DbxDocument(
            element(
                "partition",
                group,
                member,
                mesh_entity,
                guid="LEVEL",
            )
        )
        transforms = self._transform(1.0, 2.0, 3.0) + self._transform(
            4.0, 5.0, 6.0
        )
        havok = element(
            "instance",
            element(
                "array",
                value="/".join(str(value) for value in transforms),
                name="Transforms",
            ),
            element("array", value="7/9", name="Enums"),
            element(
                "array",
                element("item", ref="Physics/Test/A"),
                element("item", ref="Physics/Test/B"),
                name="Assets",
            ),
            guid="HAVOK",
        )
        havok_document = DbxDocument(
            element(
                "partition",
                havok,
                guid="HAVOK_PARTITION",
                primaryInstance="HAVOK",
            )
        )
        return level, group, havok_document

    def test_group_placements_follow_spawn_and_havok_order(self):
        level, group, havok = self._group_documents()
        resolver = LevelResolver(SimpleNamespace(), "TEST")
        with (
            mock.patch.object(
                resolver,
                "_group_havok_document",
                return_value=havok,
            ),
            mock.patch.object(
                resolver,
                "_mesh",
                return_value=SimpleNamespace(key="mesh"),
            ),
        ):
            placements = resolver._group_placements(level, group)
        self.assertEqual(
            [placement.enumeration for placement in placements],
            [7, 9],
        )
        self.assertEqual(
            [placement.reference for placement in placements],
            ["Physics/Test/A", "Physics/Test/B"],
        )
        self.assertEqual(placements[1].transform.translation, (4.0, 5.0, 6.0))

    def test_group_enumeration_mismatch_is_rejected(self):
        level, group, havok = self._group_documents(second_spawn=8)
        resolver = LevelResolver(SimpleNamespace(), "TEST")
        with (
            mock.patch.object(
                resolver,
                "_group_havok_document",
                return_value=havok,
            ),
            mock.patch.object(
                resolver,
                "_mesh",
                return_value=SimpleNamespace(key="mesh"),
            ),
            self.assertRaisesRegex(
                CorruptDataError,
                "enumeration does not match",
            ),
        ):
            resolver._group_placements(level, group)


class LevelDiscoveryTests(unittest.TestCase):
    def test_collapses_duplicate_virtual_paths_case_insensitively(self):
        self.assertEqual(
            _unique_virtual_paths(
                (
                    "Terrains/MP_001/MP_001.Heightfield-000.res",
                    "terrains/mp_001/mp_001.heightfield-000.RES",
                    "Terrains/MP_001/MP_001.Heightfield-001.res",
                )
            ),
            (
                "Terrains/MP_001/MP_001.Heightfield-000.res",
                "Terrains/MP_001/MP_001.Heightfield-001.res",
            ),
        )

    def test_discovers_only_complete_pc_level_archive_pairs(self):
        levels = discover_pc_levels(
            (
                "Dist/win32/levels/mp_002/terrain-00.fbrb",
                "Dist/win32/levels/mp_001/level-00.fbrb",
                "Dist/win32/levels/mp_common/level-00.fbrb",
                r"Dist\win32\levels\mp_002\level-00.fbrb",
                "Dist/win32/levels/mp_001/terrain-00.fbrb",
                "Dist/win32/objects/level-00.fbrb",
            )
        )

        self.assertEqual(levels, ("MP_001", "MP_002"))

    def test_discovers_complete_pc_and_bf1943_level_archive_pairs(self):
        levels = discover_levels(
            (
                "Dist/win32/levels/mp_001/level-00.fbrb",
                "Dist/win32/levels/mp_001/terrain-00.fbrb",
                "dist/xenon/levels/coral_sea/reduced-level-00.fbrb",
                "dist/xenon/levels/coral_sea/terrain-00.fbrb",
                "dist/xenon/levels/incomplete/reduced-level-00.fbrb",
                "dist/xenon/levels/levels-common-00.fbrb",
            )
        )

        self.assertEqual(levels, ("CORAL_SEA", "MP_001"))

    def test_builds_targeted_object_archive_paths_without_terrain(self):
        self.assertEqual(
            pc_level_archive_paths("MP_001", include_terrain=False),
            (
                "Dist/win32/levels/mp_001/level-00.fbrb",
                "Dist/win32/levels/mp_common/level-00.fbrb",
            ),
        )
        self.assertEqual(
            pc_level_archive_paths("SP_003_B"),
            (
                "Dist/win32/levels/sp_003_b/level-00.fbrb",
                "Dist/win32/levels/sp_003_b/terrain-00.fbrb",
                "Dist/win32/levels/sp_common/level-00.fbrb",
                "Dist/win32/levels/mp_common/level-00.fbrb",
            ),
        )

    def test_builds_bf1943_level_archive_paths(self):
        expected = (
            "Dist/xenon/levels/coral_sea/reduced-level-00.fbrb",
            "Dist/xenon/levels/coral_sea/terrain-00.fbrb",
            "Dist/xenon/levels/levels-common-00.fbrb",
        )
        self.assertEqual(
            xbox360_level_archive_paths("CORAL_SEA"),
            expected,
        )
        self.assertEqual(
            level_archive_paths("XBOX360_STFS", "CORAL_SEA"),
            expected,
        )

    def test_indexes_bf1943_level_archive_family(self):
        catalog = mock.Mock()
        catalog.archive_by_path.side_effect = (
            {"indexed": 0},
            {"indexed": 1},
            {"indexed": 0},
        )
        repository = SimpleNamespace(
            kind="XBOX360_STFS",
            catalog=catalog,
            index_archive_path=mock.Mock(),
            materialize_archive_payload=mock.Mock(),
        )
        progress = mock.Mock()

        paths = index_level_archives(repository, "CORAL_SEA", progress)

        self.assertEqual(paths, xbox360_level_archive_paths("CORAL_SEA"))
        self.assertEqual(
            repository.index_archive_path.call_args_list,
            [mock.call(paths[0]), mock.call(paths[2])],
        )
        self.assertEqual(
            repository.materialize_archive_payload.call_args_list,
            [mock.call(path) for path in paths],
        )
        self.assertEqual(
            progress.call_args_list,
            [
                mock.call(index, len(paths), path)
                for index, path in enumerate(paths, 1)
            ],
        )


class CatalogTests(unittest.TestCase):
    def test_discovers_xbox_shader_database_candidates_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            catalog = AssetCatalog(Path(temporary) / "catalog.sqlite3")
            try:
                archive_id = catalog.upsert_archive(
                    ArchiveDescriptor(
                        "Dist/xenon/levels/test/reduced-level-00.fbrb",
                        4096,
                        "xbox360",
                        {"path": "test.fbrb"},
                    )
                )
                catalog.replace_archive_assets(
                    archive_id,
                    (
                        FbrbEntry(
                            "Levels/Test/Shaders/Single.res",
                            "Dx9ShaderDatabase",
                            0,
                            16,
                            16,
                            1,
                        ),
                    ),
                    payload_offset=0,
                    payload_compressed=False,
                    payload_size=4096,
                )
                self.assertEqual(catalog.shader_database_assets(), [])
                candidates = catalog.shader_database_assets(
                    include_dx9=True,
                )
                self.assertEqual(len(candidates), 1)
                self.assertEqual(
                    candidates[0]["asset_type"],
                    "Dx9ShaderDatabase",
                )
            finally:
                catalog.close()

    def test_indexes_shader_material_sources_and_invalidates_on_reindex(self):
        with tempfile.TemporaryDirectory() as temporary:
            catalog = AssetCatalog(Path(temporary) / "catalog.sqlite3")
            try:
                archive_ids = []
                for index, name in enumerate(("First", "Second")):
                    archive_id = catalog.upsert_archive(
                        ArchiveDescriptor(
                            f"Dist/{name}.fbrb",
                            4096,
                            "pc",
                            {"path": f"Dist/{name}.fbrb"},
                        )
                    )
                    archive_ids.append(archive_id)
                    catalog.replace_archive_assets(
                        archive_id,
                        (
                            FbrbEntry(
                                (
                                    f"Levels/{name}/shaders/"
                                    "Dx11_Single.res"
                                ),
                                "ShaderDatabase",
                                index * 16,
                                16,
                                16,
                                1,
                            ),
                        ),
                        payload_offset=0,
                        payload_compressed=False,
                        payload_size=4096,
                    )
                databases = catalog.shader_database_assets()
                self.assertEqual(len(databases), 2)
                catalog.record_shader_database(
                    int(databases[0]["id"]),
                    ("Objects/Test/Shaders/Main",),
                )
                catalog.record_shader_database(
                    int(databases[1]["id"]),
                    (
                        "Objects/Test/Shaders/Main",
                        "Objects/Test/Shaders/Glass",
                    ),
                )
                sources = catalog.shader_database_sources(
                    (
                        "objects/test/shaders/main",
                        "objects/test/shaders/glass",
                    )
                )
                self.assertEqual(
                    [
                        (
                            row["archive_path"],
                            row["matching_materials"],
                        )
                        for row in sources
                    ],
                    [
                        ("Dist/Second.fbrb", 2),
                        ("Dist/First.fbrb", 1),
                    ],
                )
                partition_sources = catalog.shader_database_sources(
                    (),
                    ("objects/test/shaders/glass",),
                )
                self.assertEqual(
                    [
                        row["archive_path"]
                        for row in partition_sources
                    ],
                    ["Dist/Second.fbrb"],
                )
                self.assertTrue(
                    catalog.shader_database_recorded(
                        int(databases[1]["id"])
                    )
                )
                self.assertEqual(
                    catalog.shader_database_material_keys(
                        int(databases[1]["id"])
                    ),
                    (
                        "objects/test/shaders/glass",
                        "objects/test/shaders/main",
                    ),
                )

                catalog.replace_archive_assets(
                    archive_ids[1],
                    (),
                    payload_offset=0,
                    payload_compressed=False,
                    payload_size=0,
                )
                self.assertEqual(
                    [
                        row["archive_path"]
                        for row in catalog.shader_database_sources(
                            ("objects/test/shaders/main",)
                        )
                    ],
                    ["Dist/First.fbrb"],
                )
            finally:
                catalog.close()

    def test_content_groups_combine_with_type_and_word_filters(self):
        with tempfile.TemporaryDirectory() as temporary:
            catalog = AssetCatalog(Path(temporary) / "catalog.sqlite3")
            try:
                archive_id = catalog.upsert_archive(
                    ArchiveDescriptor(
                        "Dist/Test.fbrb",
                        4096,
                        "pc",
                        {"path": "Dist/Test.fbrb"},
                    )
                )
                definitions = (
                    (
                        "Characters/Beach/3P/Jap_Infantry/"
                        "Jap_Infantry_Mesh.res",
                        "SkinnedMeshSet",
                    ),
                    (
                        "Objects/Soldiers/Beach/SP_JP/"
                        "SP_JP_Rifleman.dbx",
                        "<non-resource>",
                    ),
                    (
                        "Objects/Vehicles/Air/AH60/AH60_Mesh.res",
                        "CompositeMeshSet",
                    ),
                    (
                        "Objects/Weapons/Rifles/M16/M16_Mesh.res",
                        "RigidMeshSet",
                    ),
                    (
                        "Objects/Buildings/Barn/Barn_Mesh.res",
                        "CompositeMeshSet",
                    ),
                    (
                        "Objects/Street_Objects/Crate/Crate_Mesh.res",
                        "RigidMeshSet",
                    ),
                    (
                        "Vegetation/Trees/Pine/Pine_Mesh.res",
                        "TreeMeshSet",
                    ),
                    ("Levels/MP_001/Level.dbx", "<non-resource>"),
                    ("FX/Explosions/Dust.res", "ITexture"),
                    ("Menu/Movies/Intro.res", "BinkMemory"),
                    ("Sound/Ambience/Wind.res", "Wave"),
                    ("Systems/GameSettings.dbx", "<non-resource>"),
                )
                catalog.replace_archive_assets(
                    archive_id,
                    tuple(
                        FbrbEntry(
                            path,
                            asset_type,
                            index * 16,
                            16,
                            16,
                            1,
                        )
                        for index, (path, asset_type) in enumerate(
                            definitions
                        )
                    ),
                    payload_offset=0,
                    payload_compressed=False,
                    payload_size=4096,
                )

                expected_groups = {
                    "CHARACTER": {definitions[0][0], definitions[1][0]},
                    "VEHICLE": {definitions[2][0]},
                    "WEAPON": {definitions[3][0]},
                    "BUILDING": {definitions[4][0]},
                    "PROP": {definitions[5][0]},
                    "VEGETATION": {definitions[6][0]},
                    "LEVEL": {definitions[7][0]},
                    "EFFECT": {definitions[8][0]},
                    "UI": {definitions[9][0]},
                    "AUDIO": {definitions[10][0]},
                    "OTHER": {definitions[11][0]},
                }
                grouped_paths = []
                for content_group, expected_paths in expected_groups.items():
                    rows = catalog.search_assets(
                        "ALL",
                        "",
                        100,
                        0,
                        content_group,
                    )
                    actual_paths = {row["path"] for row in rows}
                    self.assertEqual(actual_paths, expected_paths)
                    self.assertEqual(
                        catalog.asset_count(
                            "ALL",
                            "",
                            content_group,
                        ),
                        len(rows),
                    )
                    grouped_paths.extend(actual_paths)
                self.assertCountEqual(
                    grouped_paths,
                    [path for path, _asset_type in definitions],
                )
                self.assertEqual(
                    len(grouped_paths),
                    len(set(grouped_paths)),
                )

                character_meshes = catalog.search_assets(
                    "MESH",
                    "",
                    100,
                    0,
                    "CHARACTER",
                )
                self.assertEqual(
                    [row["path"] for row in character_meshes],
                    [definitions[0][0]],
                )
                self.assertEqual(
                    len(
                        catalog.search_assets(
                            "ALL",
                            "",
                            1,
                            0,
                            "CHARACTER",
                        )
                    ),
                    1,
                )
                self.assertEqual(
                    len(
                        catalog.search_assets(
                            "ALL",
                            "",
                            1,
                            1,
                            "CHARACTER",
                        )
                    ),
                    1,
                )
                self.assertEqual(
                    catalog.asset_count(
                        "LEVEL_DATA",
                        "Rifleman",
                        "CHARACTER",
                    ),
                    1,
                )
                self.assertEqual(
                    [
                        row["path"]
                        for row in catalog.search_assets(
                            "MESH",
                            "Mesh",
                            100,
                            0,
                            "PROP",
                        )
                    ],
                    [definitions[5][0]],
                )
                with self.assertRaises(ValueError):
                    catalog.asset_count(
                        "ALL",
                        "",
                        "NOT_A_CONTENT_GROUP",
                    )
            finally:
                catalog.close()

    def test_collapses_duplicate_paths_and_hides_lod_variants(self):
        with tempfile.TemporaryDirectory() as temporary:
            catalog = AssetCatalog(Path(temporary) / "catalog.sqlite3")
            try:
                first_archive_id = catalog.upsert_archive(
                    ArchiveDescriptor(
                        "Dist/First.fbrb",
                        4096,
                        "pc",
                        {"path": "Dist/First.fbrb"},
                    )
                )
                second_archive_id = catalog.upsert_archive(
                    ArchiveDescriptor(
                        "Dist/Second.fbrb",
                        4096,
                        "pc",
                        {"path": "Dist/Second.fbrb"},
                    )
                )
                catalog.replace_archive_assets(
                    first_archive_id,
                    (
                        FbrbEntry(
                            "Objects/Vehicles/Air/Mi28/Mi28_Mesh.res",
                            "CompositeMeshSet",
                            0,
                            16,
                            16,
                            1,
                        ),
                        FbrbEntry(
                            "Objects/Vehicles/Air/Mi28/"
                            "Mi28_Mesh_lod0_data.res",
                            "MeshData",
                            16,
                            16,
                            16,
                            1,
                        ),
                        FbrbEntry(
                            "Objects/Vehicles/Air/Mi28/"
                            "Mi28_Mesh_lod1_data.res",
                            "MeshData",
                            32,
                            16,
                            16,
                            1,
                        ),
                    ),
                    payload_offset=0,
                    payload_compressed=False,
                    payload_size=4096,
                )
                catalog.replace_archive_assets(
                    second_archive_id,
                    (
                        FbrbEntry(
                            "objects/vehicles/air/mi28/MI28_MESH.RES",
                            "CompositeMeshSet",
                            0,
                            16,
                            16,
                            1,
                        ),
                        FbrbEntry(
                            "objects/vehicles/air/mi28/"
                            "MI28_MESH_LOD0_DATA.RES",
                            "MeshData",
                            16,
                            16,
                            16,
                            1,
                        ),
                    ),
                    payload_offset=0,
                    payload_compressed=False,
                    payload_size=4096,
                )

                def search(**options):
                    return catalog.search_assets(
                        "MESH",
                        "Mi28",
                        100,
                        0,
                        "VEHICLE",
                        **options,
                    )

                def count(**options):
                    return catalog.asset_count(
                        "MESH",
                        "Mi28",
                        "VEHICLE",
                        **options,
                    )

                self.assertEqual(len(search()), 5)
                self.assertEqual(count(), 5)

                collapsed = search(collapse_virtual_paths=True)
                self.assertEqual(len(collapsed), 3)
                self.assertEqual(
                    count(collapse_virtual_paths=True),
                    len(collapsed),
                )
                self.assertEqual(
                    collapsed[0]["archive_path"],
                    "Dist/First.fbrb",
                )

                without_lods = search(hide_lod_variants=True)
                self.assertEqual(len(without_lods), 2)
                self.assertEqual(
                    count(hide_lod_variants=True),
                    len(without_lods),
                )
                self.assertTrue(
                    all(row["asset_type"] != "MeshData" for row in without_lods)
                )

                visible = search(
                    collapse_virtual_paths=True,
                    hide_lod_variants=True,
                )
                self.assertEqual(
                    [row["path"] for row in visible],
                    ["Objects/Vehicles/Air/Mi28/Mi28_Mesh.res"],
                )
                self.assertEqual(
                    count(
                        collapse_virtual_paths=True,
                        hide_lod_variants=True,
                    ),
                    len(visible),
                )
            finally:
                catalog.close()


class RepositoryTests(unittest.TestCase):
    def test_shader_candidates_skip_scanned_nonmatches(self):
        databases = (
            {
                "id": 1,
                "archive_path": "Dist/Preferred.fbrb",
                "shader_scanned": 1,
            },
            {
                "id": 2,
                "archive_path": "Dist/Matching.fbrb",
                "shader_scanned": 1,
            },
            {
                "id": 3,
                "archive_path": "Dist/Unknown.fbrb",
                "shader_scanned": 0,
            },
        )
        repository = object.__new__(AssetRepository)
        repository.catalog = SimpleNamespace(
            shader_database_assets=lambda: databases,
            shader_database_sources=lambda keys, partitions: (
                {
                    **databases[1],
                    "matching_materials": 1,
                },
            ),
        )

        candidates = repository.shader_database_candidates(
            ("Objects/Test/Shaders/Main",),
            ("Dist/Preferred.fbrb",),
            shader_partitions=("objects/test/shaders/main",),
        )

        self.assertEqual(
            [candidate["id"] for candidate in candidates],
            [2, 3],
        )

    def test_indexes_all_pending_archives_and_records_failures(self):
        repository = object.__new__(AssetRepository)
        repository.catalog = SimpleNamespace(
            unindexed_archives=lambda: (
                {"id": 1, "path": "Dist/Good.fbrb"},
                {"id": 2, "path": "Dist/Bad.fbrb"},
            ),
            set_archive_error=mock.Mock(),
        )
        indexed = []

        def index_archive(archive_id):
            indexed.append(archive_id)
            if archive_id == 2:
                raise OSError("synthetic extraction failure")
            return 1

        repository.index_archive = index_archive
        progress = []
        failures = repository.index_all_archives(
            lambda current, total, path: progress.append(
                (current, total, path)
            )
        )

        self.assertEqual(indexed, [1, 2])
        self.assertEqual(
            failures,
            [("Dist/Bad.fbrb", "synthetic extraction failure")],
        )
        repository.catalog.set_archive_error.assert_called_once_with(
            2,
            "synthetic extraction failure",
        )
        self.assertEqual(
            progress,
            [
                (1, 2, "Dist/Good.fbrb"),
                (2, 2, "Dist/Bad.fbrb"),
            ],
        )

    def test_archive_indexing_cancels_between_archives(self):
        repository = object.__new__(AssetRepository)
        repository.catalog = SimpleNamespace(
            unindexed_archives=lambda: (
                {"id": 1, "path": "Dist/First.fbrb"},
                {"id": 2, "path": "Dist/Second.fbrb"},
            ),
            set_archive_error=mock.Mock(),
        )
        indexed = []
        cancelled = [False]

        def progress(current, total, path):
            cancelled[0] = True

        repository.index_archive = lambda archive_id: indexed.append(
            archive_id
        )
        failures = repository.index_all_archives(
            progress,
            lambda: cancelled[0],
        )

        self.assertEqual(failures, [])
        self.assertEqual(indexed, [1])

    def test_indexes_only_requested_archive_paths(self):
        repository = object.__new__(AssetRepository)
        repository.catalog = SimpleNamespace(
            archive_by_path=lambda path: {
                "id": 1 if path == "Dist/Level.fbrb" else 2
            },
            set_archive_error=mock.Mock(),
        )
        indexed = []

        def index_archive_path(path):
            indexed.append(path)
            if path == "Dist/Common.fbrb":
                raise OSError("synthetic targeted failure")

        repository.index_archive_path = index_archive_path
        progress = []
        failures = repository.index_archive_paths(
            ("Dist/Level.fbrb", "Dist/Common.fbrb"),
            lambda current, total, path: progress.append(
                (current, total, path)
            ),
        )

        self.assertEqual(
            indexed,
            ["Dist/Level.fbrb", "Dist/Common.fbrb"],
        )
        self.assertEqual(
            failures,
            [("Dist/Common.fbrb", "synthetic targeted failure")],
        )
        repository.catalog.set_archive_error.assert_called_once_with(
            2,
            "synthetic targeted failure",
        )
        self.assertEqual(
            progress,
            [
                (1, 2, "Dist/Level.fbrb"),
                (2, 2, "Dist/Common.fbrb"),
            ],
        )

    def test_xdvdfs_to_fbrb_to_mesh_end_to_end(self):
        meshdata = make_meshdata(">")
        archive = make_fbrb("Objects/Test/Test_Mesh_lod0_data.res", "MeshData", meshdata)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "disc.iso"
            make_xdvdfs_image(image, archive)
            repository = AssetRepository(str(image), root / "cache")
            try:
                failures = repository.scan()
                self.assertEqual(failures, [])
                self.assertIsNone(
                    repository.materialize_archive_payload("test.fbrb")
                )
                rows = repository.catalog.search_assets("MESH", "Test", 10, 0)
                self.assertEqual(len(rows), 1)
                asset, raw = repository.read_asset(rows[0]["id"])
                parsed = parse_meshdata(raw, asset["platform"])
                self.assertEqual(parsed.subsets[0].faces, ((0, 1, 2),))
                exact = repository.catalog.find_asset(
                    "objects/test/test_mesh_lod0_data.res",
                    ("test.fbrb",),
                )
                self.assertIsNotNone(exact)
                self.assertIsNone(
                    repository.catalog.find_asset(
                        "Objects/Test/Test_Mesh_lod0_data"
                    )
                )
                mesh_row = repository.catalog.find_mesh_data(
                    "Objects/Test/Test_Mesh"
                )
                self.assertEqual(
                    mesh_row["path"],
                    "Objects/Test/Test_Mesh_lod0_data.res",
                )
                path_asset, path_raw = repository.read_asset_path(
                    "OBJECTS/TEST/TEST_MESH_LOD0_DATA.RES"
                )
                self.assertEqual(path_asset["id"], asset["id"])
                self.assertEqual(path_raw, raw)
            finally:
                repository.close()

    def test_stfs_to_fbrb_to_mesh_end_to_end(self):
        meshdata = make_meshdata(">")
        archive = (
            make_fbrb(
                "Objects/Test/Test_Mesh_lod0_data.res",
                "MeshData",
                meshdata,
            )
            + b"\xA5" * 5000
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_path = root / "5841097E"
            make_stfs_package(package_path, archive)

            package = StfsPackage(package_path)
            entry = package.find("DIST/XENON/TEST.FBRB")
            self.assertIsNotNone(entry)
            with package.open_file(entry) as stream:
                self.assertEqual(stream.read(), archive)

            repository = AssetRepository(str(package_path), root / "cache")
            try:
                self.assertEqual(repository.kind, "XBOX360_STFS")
                self.assertEqual(repository.scan(), [])
                rows = repository.catalog.search_assets("MESH", "Test", 10, 0)
                self.assertEqual(len(rows), 1)
                asset, raw = repository.read_asset(rows[0]["id"])
                parsed = parse_meshdata(raw, asset["platform"])
                self.assertEqual(parsed.subsets[0].faces, ((0, 1, 2),))
                self.assertEqual(asset["archive_path"], "dist/xenon/test.fbrb")
            finally:
                repository.close()

    def test_zipped_xdvdfs_to_fbrb_to_mesh_end_to_end_and_reuses_cache(self):
        meshdata = make_meshdata(">")
        archive = make_fbrb(
            "Objects/Test/Test_Mesh_lod0_data.res",
            "MeshData",
            meshdata,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "disc.iso"
            make_xdvdfs_image(image, archive)
            zip_path = root / "xbox-bc2.zip"
            with zipfile.ZipFile(
                zip_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as source_zip:
                source_zip.write(image, "nested/disc.iso")
            expected_source_size = image.stat().st_size
            image.unlink()

            progress = []
            repository = AssetRepository(
                str(zip_path),
                root / "cache",
                source_progress=lambda current, total, label: progress.append(
                    (current, total, label)
                ),
            )
            try:
                self.assertEqual(repository.kind, "XBOX360_ISO")
                self.assertEqual(
                    progress[0],
                    (0, expected_source_size, "nested/disc.iso"),
                )
                self.assertEqual(
                    progress[-1],
                    (
                        expected_source_size,
                        expected_source_size,
                        "nested/disc.iso",
                    ),
                )
                self.assertEqual(repository.scan(), [])
                cached_source = Path(repository.source.image_path)
                self.assertTrue(cached_source.is_file())
                self.assertEqual(
                    cached_source.parent,
                    repository.source_cache / "sources",
                )
                rows = repository.catalog.search_assets("MESH", "Test", 10, 0)
                self.assertEqual(len(rows), 1)
            finally:
                repository.close()

            with mock.patch(
                "bc2_frostbite_importer.core.zip_source.zipfile.ZipFile.open",
                side_effect=AssertionError(
                    "cached ZIP source member was unexpectedly extracted again"
                ),
            ):
                repository = AssetRepository(str(zip_path), root / "cache")
                try:
                    self.assertFalse(repository.needs_scan())
                    asset, raw = repository.read_asset_path(
                        "Objects/Test/Test_Mesh_lod0_data.res"
                    )
                    parsed = parse_meshdata(raw, asset["platform"])
                    self.assertEqual(parsed.subsets[0].faces, ((0, 1, 2),))
                finally:
                    repository.close()

    def test_zip_source_preparation_cancels_without_partial_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "disc.iso"
            make_xdvdfs_image(image, b"FbRB")
            zip_path = root / "xbox-bc2.zip"
            with zipfile.ZipFile(
                zip_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as source_zip:
                source_zip.write(image, "disc.iso")
            with self.assertRaisesRegex(ExtractionError, "cancelled"):
                AssetRepository(
                    str(zip_path),
                    root / "cache",
                    cancelled=lambda: True,
                )
            self.assertEqual(
                list((root / "cache").rglob("bc2-zip-*.tmp")),
                [],
            )
            self.assertEqual(list((root / "cache").rglob("*.iso")), [])

    def test_zipped_extensionless_stfs_to_fbrb_to_mesh_end_to_end(self):
        meshdata = make_meshdata(">")
        archive = make_fbrb(
            "Objects/Test/Test_Mesh_lod0_data.res",
            "MeshData",
            meshdata,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_path = root / "5841097E"
            make_stfs_package(package_path, archive)
            zip_path = root / "bf1943.zip"
            with zipfile.ZipFile(
                zip_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as source_zip:
                source_zip.write(
                    package_path,
                    "content/945CD965D6B2B2584A9C06CE16F38A075A6D390558",
                )
            package_path.unlink()

            repository = AssetRepository(str(zip_path), root / "cache")
            try:
                self.assertEqual(repository.kind, "XBOX360_STFS")
                self.assertEqual(repository.scan(), [])
                asset, raw = repository.read_asset_path(
                    "Objects/Test/Test_Mesh_lod0_data.res"
                )
                parsed = parse_meshdata(raw, asset["platform"])
                self.assertEqual(parsed.subsets[0].faces, ((0, 1, 2),))
            finally:
                repository.close()

    def test_zip_rejects_multiple_source_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zip_path = root / "ambiguous.zip"
            with zipfile.ZipFile(zip_path, "w") as source_zip:
                source_zip.writestr("disc-one.iso", b"first")
                source_zip.writestr("disc-two.iso", b"second")
            with self.assertRaisesRegex(
                UnsupportedSourceError,
                "exactly one identifiable source member",
            ):
                AssetRepository(str(zip_path), root / "cache")

    def test_stfs_rejects_fragmented_multiblock_file(self):
        archive = b"FbRB" + b"\0" * 5000
        with tempfile.TemporaryDirectory() as temporary:
            package_path = Path(temporary) / "fragmented"
            make_stfs_package(
                package_path,
                archive,
                consecutive=False,
            )
            package = StfsPackage(package_path)
            entry = package.find("dist/xenon/test.fbrb")
            self.assertIsNotNone(entry)
            with self.assertRaisesRegex(
                UnsupportedSourceError,
                "Fragmented STFS files",
            ):
                package.open_file(entry)

    def test_materialized_compressed_payload_supports_direct_reads(self):
        meshdata = make_meshdata(">")
        archive = make_fbrb(
            "Objects/Test/Test_Mesh_lod0_data.res",
            "MeshData",
            meshdata,
            compress_payload=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "disc.iso"
            make_xdvdfs_image(image, archive)
            repository = AssetRepository(str(image), root / "cache")
            try:
                self.assertEqual(repository.scan(), [])
                payload_path = repository.materialize_archive_payload(
                    "test.fbrb"
                )
                self.assertIsNotNone(payload_path)
                self.assertEqual(payload_path.read_bytes(), meshdata)
                with mock.patch(
                    "bc2_frostbite_importer.core.repository.gzip.GzipFile",
                    side_effect=AssertionError(
                        "cached read unexpectedly inflated the archive"
                    ),
                ):
                    asset, raw = repository.read_asset_path(
                        "Objects/Test/Test_Mesh_lod0_data.res"
                    )
                    extracted_asset, extracted = repository.extract_asset(
                        int(asset["id"]),
                        root / "extracted.meshdata",
                    )
                self.assertEqual(raw, meshdata)
                self.assertEqual(extracted_asset["id"], asset["id"])
                self.assertEqual(extracted.read_bytes(), meshdata)
            finally:
                repository.close()


if __name__ == "__main__":
    unittest.main()
