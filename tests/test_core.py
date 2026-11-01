# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import gzip
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from bc2_frostbite_importer.core import texture
from bc2_frostbite_importer.core.dbx import (
    DbxDocument,
    DbxElement,
    dbx_to_xml,
    parse_dbx,
)
from bc2_frostbite_importer.core.errors import CorruptDataError
from bc2_frostbite_importer.core.level import FrostbiteTransform, LevelResolver
from bc2_frostbite_importer.core.mesh import (
    _position_format,
    parse_meshdata,
)
from bc2_frostbite_importer.core.repository import AssetRepository
from bc2_frostbite_importer.core.shaderdb import (
    _rdef_texture_parameters,
    parse_shader_database,
)
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


def make_heightfield(header_endian: str, sample_endian: str) -> bytes:
    resolution = 4
    data = bytearray(44 + resolution * resolution * 2)
    struct.pack_into(
        header_endian + "6f",
        data,
        0,
        -2.0,
        0.0,
        -2.0,
        2.0,
        10.0,
        2.0,
    )
    struct.pack_into(header_endian + "I", data, 24, resolution)
    for index in range(resolution * resolution):
        struct.pack_into(
            sample_endian + "H",
            data,
            44 + index * 2,
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
    data.extend(struct.pack("<ii", 0, 0))
    data.extend(struct.pack("<i", 1))
    data.extend(b"Objects/Test/Mesh/Shaders/Surface\0")
    data.extend(material_reference)
    data.extend(struct.pack("<ii", 0, 0))
    data.extend(struct.pack("<i", 1))
    data.extend(material_reference)
    data.extend(struct.pack("<i", 1))
    data.extend(struct.pack("<h", 0))
    return bytes(data)


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

    def test_stride_48_uses_primary_and_secondary_uv_channels(self):
        metadata = bytearray(struct.pack("<IHB", 1, 0, 1))
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
        for position, packed_vector, primary_uv, secondary_uv in values:
            vertex = bytearray(48)
            struct.pack_into("<eee", vertex, 0, *position)
            struct.pack_into("<ee", vertex, 8, *packed_vector)
            struct.pack_into("<ee", vertex, 24, *primary_uv)
            struct.pack_into("<ee", vertex, 28, *secondary_uv)
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
        self.assertEqual(subset.uv_offsets, (24, 28))
        self.assertEqual(subset.uvs[1], (1.0, 0.0))
        self.assertAlmostEqual(subset.secondary_uvs[2][1], 0.8, places=3)

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


class TextureTests(unittest.TestCase):
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
            ("xbox360", ">", "<"),
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

    def test_heightfield_uses_requested_even_sample_count(self):
        terrain = parse_heightfield(
            make_heightfield("<", "<"),
            target_resolution=3,
            platform="pc",
        )
        self.assertEqual(len(terrain.positions), 9)
        self.assertEqual(len(terrain.faces), 8)
        self.assertEqual(tuple(terrain.positions[-1][:2]), (2.0, 2.0))

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


class RepositoryTests(unittest.TestCase):
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
