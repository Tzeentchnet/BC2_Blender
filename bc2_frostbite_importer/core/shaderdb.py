# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
import struct
from dataclasses import dataclass

from .errors import CorruptDataError
from .io import read_exact


@dataclass(frozen=True)
class ShaderTextureBinding:
    index: int
    path: str
    parameters: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShaderMaterial:
    key: str
    textures: tuple[ShaderTextureBinding, ...]


class ShaderDatabase:
    def __init__(
        self,
        materials: tuple[ShaderMaterial, ...],
        trailing_bytes: int,
    ):
        self.materials = materials
        self.trailing_bytes = trailing_bytes
        self._materials_by_key = {
            material.key.casefold(): material for material in materials
        }

    def material(self, key: str) -> ShaderMaterial | None:
        return self._materials_by_key.get(key.casefold())


def _i32(stream: io.BytesIO, context: str) -> int:
    return struct.unpack("<i", read_exact(stream, 4, context))[0]


def _i16(stream: io.BytesIO, context: str) -> int:
    return struct.unpack("<h", read_exact(stream, 2, context))[0]


def _count(
    stream: io.BytesIO,
    context: str,
    maximum: int = 1_000_000,
) -> int:
    value = _i32(stream, context)
    if value < 0 or value > maximum:
        raise CorruptDataError(f"Unsafe {context}: {value:,}")
    return value


def _skip(stream: io.BytesIO, size: int, context: str):
    if size < 0:
        raise CorruptDataError(f"Negative {context} size: {size:,}")
    read_exact(stream, size, context)


def _cstring(
    stream: io.BytesIO,
    context: str,
    maximum: int = 16_384,
) -> str:
    value = bytearray()
    for _ in range(maximum):
        byte = read_exact(stream, 1, context)
        if byte == b"\0":
            return value.decode("utf-8", "replace")
        value.extend(byte)
    raise CorruptDataError(f"{context} exceeds {maximum:,} bytes")


def _param_groups(
    stream: io.BytesIO,
) -> list[tuple[ShaderTextureBinding, ...]]:
    groups = []
    for group_index in range(_count(stream, "shader parameter-group count")):
        start = stream.tell()
        size = _i32(stream, "shader parameter-group size")
        if size < 32:
            raise CorruptDataError(
                f"Shader parameter group {group_index} is too short: {size}"
            )
        _skip(stream, 20, "shader parameter-group header")
        counts = read_exact(stream, 6, "shader parameter-group counts")
        texture_count, property_count_56, property_count_40 = counts[3:6]
        _skip(stream, 2, "shader parameter-group footer")
        textures = []
        for _ in range(texture_count):
            index = _i32(stream, "shader texture binding index")
            raw_name = read_exact(stream, 136, "shader texture binding name")
            name = raw_name.split(b"\0", 1)[0].decode("utf-8", "replace")
            if name:
                textures.append(ShaderTextureBinding(index=index, path=name))
        _skip(
            stream,
            property_count_56 * 56,
            "shader 56-byte properties",
        )
        _skip(
            stream,
            property_count_40 * 40,
            "shader 40-byte properties",
        )
        end = start + size
        if stream.tell() > end:
            raise CorruptDataError(
                f"Shader parameter group {group_index} exceeds its size"
            )
        _skip(
            stream,
            end - stream.tell(),
            "shader parameter-group metadata",
        )
        groups.append(tuple(textures))
    return groups


def _skip_block2_group(stream: io.BytesIO):
    for _ in range(_count(stream, "shader block-2 group count")):
        item_count = _i16(stream, "shader block-2 item count")
        if item_count < 0:
            raise CorruptDataError(
                f"Negative shader block-2 item count: {item_count}"
            )
        _skip(stream, 2 + item_count * 4, "shader block-2 item")


def _skip_block2(stream: io.BytesIO):
    _skip_block2_group(stream)
    _skip_block2_group(stream)


def _rdef_texture_parameters(
    data: bytes,
) -> dict[int, tuple[str, ...]]:
    if len(data) < 16:
        raise CorruptDataError("DXBC RDEF chunk is too short")
    resource_count, resource_offset = struct.unpack_from("<II", data, 8)
    if resource_count > 100_000:
        raise CorruptDataError(
            f"Unsafe DXBC resource-binding count: {resource_count:,}"
        )
    resource_size = 32
    resource_end = resource_offset + resource_count * resource_size
    if resource_offset < 0 or resource_end > len(data):
        raise CorruptDataError("DXBC resource bindings exceed the RDEF chunk")

    names: dict[int, list[str]] = {}
    for resource_index in range(resource_count):
        offset = resource_offset + resource_index * resource_size
        values = struct.unpack_from("<8I", data, offset)
        name_offset, input_type, bind_point = values[0], values[1], values[5]
        if input_type != 2:
            continue
        if name_offset >= len(data):
            raise CorruptDataError("DXBC resource name exceeds the RDEF chunk")
        end = data.find(b"\0", name_offset)
        if end < 0:
            raise CorruptDataError("DXBC resource name is not terminated")
        name = data[name_offset:end].decode("utf-8", "replace")
        if name and name not in names.setdefault(bind_point, []):
            names[bind_point].append(name)
    return {index: tuple(values) for index, values in names.items()}


def _shader(
    stream: io.BytesIO,
    metadata_size: int,
) -> dict[int, tuple[str, ...]]:
    _skip(stream, 4 + metadata_size + 4, "shader record header")
    _skip(stream, 4 + 16 + 4 + 4, "DXBC shader header")
    chunk_count = _count(stream, "DXBC shader chunk count", maximum=1_000)
    _skip(stream, chunk_count * 4, "DXBC shader chunk offsets")
    parameters: dict[int, list[str]] = {}
    for _ in range(chunk_count):
        chunk_name = read_exact(stream, 4, "DXBC shader chunk name")
        chunk_size = _count(
            stream, "DXBC shader chunk size", maximum=1 << 30
        )
        chunk = read_exact(stream, chunk_size, "DXBC shader chunk")
        if chunk_name == b"RDEF":
            for index, names in _rdef_texture_parameters(chunk).items():
                target = parameters.setdefault(index, [])
                for name in names:
                    if name not in target:
                        target.append(name)
    return {index: tuple(values) for index, values in parameters.items()}


def _skip_shader_declaration(stream: io.BytesIO):
    _skip(stream, 12 + 4, "shader declaration metadata")
    _skip(stream, 4 + 16 + 4 + 4 + 4 + 4, "shader declaration header")
    _skip(stream, 4, "shader declaration chunk name")
    chunk_size = _count(
        stream, "shader declaration chunk size", maximum=1 << 30
    )
    _skip(stream, chunk_size, "shader declaration chunk")
    property_count = _count(
        stream, "shader declaration property count", maximum=100_000
    )
    _skip(
        stream,
        property_count * 28,
        "shader declaration property values",
    )
    _count(
        stream,
        "shader declaration property-name count",
        maximum=100_000,
    )
    for _ in range(property_count):
        _cstring(stream, "shader declaration property name")


def _shader_groups(
    stream: io.BytesIO,
) -> tuple[
    list[dict[int, tuple[str, ...]]],
    list[dict[int, tuple[str, ...]]],
]:
    vertex_shaders = []
    group1_count = _count(stream, "vertex shader count", maximum=100_000)
    for _ in range(group1_count):
        vertex_shaders.append(_shader(stream, 12))
        _skip_shader_declaration(stream)
    pixel_shaders = []
    group2_count = _count(stream, "pixel shader count", maximum=100_000)
    if group2_count:
        pixel_shaders.append(_shader(stream, 12))
        for _ in range(group2_count - 1):
            pixel_shaders.append(_shader(stream, 24))
    return vertex_shaders, pixel_shaders


def _block4(stream: io.BytesIO) -> list[tuple[bytes, int, int]]:
    _skip(stream, 12, "shader block-4 header")
    items = []
    for _ in range(_count(stream, "shader block-4 item count")):
        reference = read_exact(stream, 16, "shader block-4 reference")
        values = struct.unpack(
            "<8i", read_exact(stream, 32, "shader block-4 item")
        )
        items.append((reference, values[5], values[7]))
    return items


def _skip_block5(stream: io.BytesIO):
    for _ in range(_count(stream, "shader block-5 group-1 count")):
        _skip(stream, 48 + 10, "shader block-5 group-1 references")
        field_count = _count(
            stream, "shader block-5 field count", maximum=1_000_000
        )
        _skip(
            stream,
            field_count * 4 + 2,
            "shader block-5 group-1 fields",
        )
    group2_count = _count(stream, "shader block-5 group-2 count")
    _skip(stream, group2_count * 89, "shader block-5 group-2 items")


def _block6(stream: io.BytesIO) -> list[tuple[str, bytes]]:
    items = []
    for group_index in range(3):
        count = _count(stream, f"shader block-6 group-{group_index + 1} count")
        for _ in range(count):
            name = _cstring(stream, "shader material key")
            reference = read_exact(stream, 16, "shader block-6 reference")
            items.append((name, reference))
    return items


def _block7(stream: io.BytesIO) -> dict[bytes, tuple[int, ...]]:
    items = {}
    for _ in range(_count(stream, "shader block-7 item count")):
        reference = read_exact(stream, 16, "shader block-7 reference")
        index_count = _count(
            stream, "shader block-7 index count", maximum=1_000_000
        )
        indices = tuple(
            _i16(stream, "shader block-7 index") for _ in range(index_count)
        )
        if any(index < 0 for index in indices):
            raise CorruptDataError("Shader block-7 contains a negative index")
        if reference in items:
            raise CorruptDataError("Shader block-7 reference is duplicated")
        items[reference] = indices
    return items


def parse_shader_database(data: bytes) -> ShaderDatabase:
    if len(data) < 32:
        raise CorruptDataError("Shader database is too short")
    stream = io.BytesIO(data)
    _skip(stream, 5, "shader database header")
    parameter_groups = _param_groups(stream)
    _skip_block2(stream)
    _, pixel_shaders = _shader_groups(stream)
    block4 = _block4(stream)
    _skip_block5(stream)
    block6 = _block6(stream)
    block7 = _block7(stream)

    materials = []
    seen_keys = set()
    for key, reference in block6:
        normalized_key = key.casefold()
        if normalized_key in seen_keys:
            raise CorruptDataError(f"Duplicate shader material key: {key}")
        seen_keys.add(normalized_key)
        textures = []
        texture_parameters: dict[
            tuple[int, str], list[str]
        ] = {}
        for block4_index in block7.get(reference, ()):
            if block4_index >= len(block4):
                raise CorruptDataError(
                    f"Shader block-4 index is out of range: {block4_index}"
                )
            _, pixel_shader_index, parameter_group_index = block4[block4_index]
            if not 0 <= parameter_group_index < len(parameter_groups):
                raise CorruptDataError(
                    "Shader parameter-group index is out of range: "
                    f"{parameter_group_index}"
                )
            if pixel_shader_index < 0 or not pixel_shaders:
                shader_parameters = {}
            elif pixel_shader_index >= len(pixel_shaders):
                raise CorruptDataError(
                    "Pixel shader index is out of range: "
                    f"{pixel_shader_index}"
                )
            else:
                shader_parameters = pixel_shaders[pixel_shader_index]
            for texture in parameter_groups[parameter_group_index]:
                identity = (texture.index, texture.path.casefold())
                if identity not in texture_parameters:
                    textures.append(texture)
                    texture_parameters[identity] = []
                parameters = texture_parameters[identity]
                for name in shader_parameters.get(texture.index, ()):
                    if name not in parameters:
                        parameters.append(name)
        materials.append(
            ShaderMaterial(
                key=key,
                textures=tuple(
                    ShaderTextureBinding(
                        index=texture.index,
                        path=texture.path,
                        parameters=tuple(
                            texture_parameters[
                                (texture.index, texture.path.casefold())
                            ]
                        ),
                    )
                    for texture in textures
                ),
            )
        )

    return ShaderDatabase(
        materials=tuple(materials),
        trailing_bytes=len(data) - stream.tell(),
    )
