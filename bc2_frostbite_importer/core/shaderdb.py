# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
import struct
from dataclasses import dataclass

from .dxbc import (
    DxbcResourceBinding,
    rdef_texture_bindings,
    shader_resources,
    texture_coordinate_routes,
)
from .errors import CorruptDataError
from .io import read_exact


@dataclass(frozen=True)
class ShaderTextureBinding:
    index: int
    path: str
    parameters: tuple[str, ...] = ()
    dimension: int | None = None

    @property
    def slot(self) -> int:
        return self.index & 0xFF


@dataclass(frozen=True)
class ShaderVertexField:
    code: int
    offset: int
    size: int
    semantic_index: int
    semantic_components: tuple[int, ...]

    @property
    def is_uv(self) -> bool:
        return self.code == 6


@dataclass(frozen=True)
class ShaderVertexLayout:
    reference: bytes
    stride: int
    layout_type: int
    fields: tuple[ShaderVertexField, ...]

    @property
    def uv_offsets(self) -> tuple[int, ...]:
        return tuple(field.offset for field in self.fields if field.is_uv)

    def uv_offset(
        self,
        semantic_index: int,
        component: int,
    ) -> int | None:
        for field in self.fields:
            if (
                field.is_uv
                and field.semantic_index == semantic_index
                and component in field.semantic_components
            ):
                return field.offset
        return None


@dataclass(frozen=True)
class ShaderTextureRoute:
    texture_index: int
    uv_offset: int
    source_semantic_index: int
    source_components: tuple[int, int]
    scale: tuple[float | None, float | None]
    offset: tuple[float | None, float | None]


@dataclass(frozen=True)
class ShaderVariant:
    table_index: int
    vertex_shader_index: int
    pixel_shader_index: int
    parameter_group_index: int
    layout: ShaderVertexLayout | None
    texture_routes: tuple[ShaderTextureRoute, ...] = ()
    route_error: str | None = None


@dataclass(frozen=True)
class ShaderMaterial:
    key: str
    textures: tuple[ShaderTextureBinding, ...]
    variants: tuple[ShaderVariant, ...] = ()

    def compatible_variants(
        self,
        vertex_stride: int,
    ) -> tuple[ShaderVariant, ...]:
        return tuple(
            variant
            for variant in self.variants
            if (
                variant.layout is not None
                and variant.layout.stride == vertex_stride
            )
        )


class ShaderDatabase:
    def __init__(
        self,
        materials: tuple[ShaderMaterial, ...],
        trailing_bytes: int,
        layouts: tuple[ShaderVertexLayout, ...] = (),
    ):
        self.materials = materials
        self.trailing_bytes = trailing_bytes
        self.layouts = layouts
        self._materials_by_key = {
            material.key.casefold(): material for material in materials
        }

    def material(self, key: str) -> ShaderMaterial | None:
        return self._materials_by_key.get(key.casefold())

    def materials_for_shader_partition(
        self,
        shader_partition: str,
    ) -> tuple[ShaderMaterial, ...]:
        normalized = shader_partition.replace("\\", "/").strip("/").casefold()
        if not normalized:
            return ()
        suffix = "/" + normalized
        return tuple(
            material
            for material in self.materials
            if (
                material.key.replace("\\", "/").strip("/").casefold()
                == normalized
                or material.key.replace("\\", "/").casefold().endswith(suffix)
            )
        )


@dataclass(frozen=True)
class _ShaderProgram:
    data: bytes
    resources: tuple[DxbcResourceBinding, ...]


@dataclass(frozen=True)
class _Block4Item:
    reference: bytes
    vertex_shader_index: int
    pixel_shader_index: int
    parameter_group_index: int


@dataclass(frozen=True)
class _Block5Child:
    shader_reference: bytes
    system_reference: bytes
    layout_reference: bytes


_VERTEX_FIELD_WIDTHS = {
    263: 6,
    258: 8,
    259: 12,
    1032: 8,
    1288: 8,
    1797: 2,
    6: 4,
    524: 4,
    780: 4,
    264: 8,
    8: 8,
    4: 16,
    12: 4,
    1544: 8,
    21: 8,
    3: 12,
    15: 4,
    2: 8,
    1: 4,
}


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


def _vertex_layout(
    reference: bytes,
    stride: int,
    layout_type: int,
    codes: tuple[int, ...],
) -> ShaderVertexLayout:
    if not codes and layout_type != 0xFFFFFF00:
        return ShaderVertexLayout(
            reference=reference,
            stride=stride,
            layout_type=layout_type,
            fields=(),
        )
    fields = []
    offset = 0
    semantic_index = 0
    code_index = 0
    while code_index < len(codes):
        code = codes[code_index]
        size = _VERTEX_FIELD_WIDTHS.get(code)
        if size is None:
            raise CorruptDataError(
                f"Unsupported shader vertex field code: {code}"
            )
        if (
            code == 263
            and code_index + 1 < len(codes)
            and codes[code_index + 1] == 1797
        ):
            fields.append(
                ShaderVertexField(
                    code=code,
                    offset=offset,
                    size=size,
                    semantic_index=semantic_index,
                    semantic_components=(0, 1, 2),
                )
            )
            offset += size
            scalar_size = _VERTEX_FIELD_WIDTHS[1797]
            fields.append(
                ShaderVertexField(
                    code=1797,
                    offset=offset,
                    size=scalar_size,
                    semantic_index=semantic_index,
                    semantic_components=(3,),
                )
            )
            offset += scalar_size
            semantic_index += 1
            code_index += 2
            continue
        if code == 6:
            fields.append(
                ShaderVertexField(
                    code=code,
                    offset=offset,
                    size=size,
                    semantic_index=semantic_index,
                    semantic_components=(0, 1),
                )
            )
            offset += size
            code_index += 1
            if code_index < len(codes) and codes[code_index] == 6:
                fields.append(
                    ShaderVertexField(
                        code=6,
                        offset=offset,
                        size=size,
                        semantic_index=semantic_index,
                        semantic_components=(2, 3),
                    )
                )
                offset += size
                code_index += 1
            semantic_index += 1
            continue
        components = (
            (0, 1, 2)
            if code == 259
            else (0, 1)
            if code in {2, 258}
            else (0,)
            if code == 1797
            else (0, 1, 2, 3)
        )
        fields.append(
            ShaderVertexField(
                code=code,
                offset=offset,
                size=size,
                semantic_index=semantic_index,
                semantic_components=components,
            )
        )
        offset += size
        semantic_index += 1
        code_index += 1
    if offset > stride:
        raise CorruptDataError(
            "Shader vertex fields total "
            f"{offset} bytes, exceeding the declared stride of {stride}"
        )
    return ShaderVertexLayout(
        reference=reference,
        stride=stride,
        layout_type=layout_type,
        fields=tuple(fields),
    )


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
    names: dict[int, list[str]] = {}
    for binding in rdef_texture_bindings(data):
        if (
            binding.name
            and binding.name not in names.setdefault(binding.index, [])
        ):
            names[binding.index].append(binding.name)
    return {index: tuple(values) for index, values in names.items()}


def _shader(
    stream: io.BytesIO,
    metadata_size: int,
) -> _ShaderProgram:
    _skip(stream, 4 + metadata_size + 4, "shader record header")
    start = stream.tell()
    header = read_exact(stream, 4 + 16 + 4 + 4, "DXBC shader header")
    total_size = struct.unpack_from("<I", header, 24)[0]
    chunk_count = _count(stream, "DXBC shader chunk count", maximum=1_000)
    _skip(stream, chunk_count * 4, "DXBC shader chunk offsets")
    for _ in range(chunk_count):
        read_exact(stream, 4, "DXBC shader chunk name")
        chunk_size = _count(
            stream, "DXBC shader chunk size", maximum=1 << 30
        )
        _skip(stream, chunk_size, "DXBC shader chunk")
    if total_size < 32 or stream.tell() != start + total_size:
        raise CorruptDataError("DXBC shader record size does not match its data")
    data = stream.getvalue()[start : start + total_size]
    return _ShaderProgram(data=data, resources=shader_resources(data))


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
    list[_ShaderProgram],
    list[_ShaderProgram],
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


def _block4(stream: io.BytesIO) -> list[_Block4Item]:
    _skip(stream, 12, "shader block-4 header")
    items = []
    for _ in range(_count(stream, "shader block-4 item count")):
        reference = read_exact(stream, 16, "shader block-4 reference")
        values = struct.unpack(
            "<8i", read_exact(stream, 32, "shader block-4 item")
        )
        items.append(
            _Block4Item(
                reference=reference,
                vertex_shader_index=values[4],
                pixel_shader_index=values[5],
                parameter_group_index=values[7],
            )
        )
    return items


def _block5(
    stream: io.BytesIO,
) -> tuple[list[_Block5Child], dict[bytes, ShaderVertexLayout]]:
    children = []
    for _ in range(_count(stream, "shader block-5 group-1 count")):
        references = read_exact(
            stream,
            48,
            "shader block-5 group-1 references",
        )
        _skip(stream, 10, "shader block-5 group-1 metadata")
        field_count = _count(
            stream, "shader block-5 field count", maximum=1_000_000
        )
        _skip(
            stream,
            field_count * 4 + 2,
            "shader block-5 group-1 fields",
        )
        children.append(
            _Block5Child(
                shader_reference=references[:16],
                system_reference=references[16:32],
                layout_reference=references[32:48],
            )
        )
    group2_count = _count(stream, "shader block-5 group-2 count")
    layouts = {}
    for _ in range(group2_count):
        raw = read_exact(stream, 89, "shader block-5 group-2 item")
        reference = raw[:16]
        field_count = raw[80]
        layout_type = struct.unpack_from("<I", raw, 81)[0]
        stride = raw[85]
        if field_count > 16:
            raise CorruptDataError(
                f"Shader vertex layout has too many fields: {field_count}"
            )
        codes = (
            tuple(
                struct.unpack_from("<H", raw, 16 + index * 4)[0]
                for index in range(field_count)
            )
            if layout_type == 0xFFFFFF00
            else ()
        )
        if reference in layouts:
            raise CorruptDataError("Shader vertex-layout reference is duplicated")
        layouts[reference] = _vertex_layout(
            reference,
            stride,
            layout_type,
            codes,
        )
    return children, layouts


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
    vertex_shaders, pixel_shaders = _shader_groups(stream)
    block4 = _block4(stream)
    block5_children, layouts = _block5(stream)
    block6 = _block6(stream)
    block7 = _block7(stream)

    variant_cache: dict[
        int,
        tuple[
            ShaderVariant,
            dict[int, tuple[DxbcResourceBinding, ...]],
        ],
    ] = {}

    def resolve_variant(block4_index: int):
        cached = variant_cache.get(block4_index)
        if cached is not None:
            return cached
        if block4_index >= len(block4):
            raise CorruptDataError(
                f"Shader block-4 index is out of range: {block4_index}"
            )
        item = block4[block4_index]
        parameter_group_index = item.parameter_group_index
        if not 0 <= parameter_group_index < len(parameter_groups):
            raise CorruptDataError(
                "Shader parameter-group index is out of range: "
                f"{parameter_group_index}"
            )
        vertex_shader = None
        if item.vertex_shader_index >= 0 and vertex_shaders:
            if item.vertex_shader_index >= len(vertex_shaders):
                raise CorruptDataError(
                    "Vertex shader index is out of range: "
                    f"{item.vertex_shader_index}"
                )
            vertex_shader = vertex_shaders[item.vertex_shader_index]
        pixel_shader = None
        if item.pixel_shader_index >= 0 and pixel_shaders:
            if item.pixel_shader_index >= len(pixel_shaders):
                raise CorruptDataError(
                    "Pixel shader index is out of range: "
                    f"{item.pixel_shader_index}"
                )
            pixel_shader = pixel_shaders[item.pixel_shader_index]
        resources: dict[int, list[DxbcResourceBinding]] = {}
        if pixel_shader is not None:
            for resource in pixel_shader.resources:
                resources.setdefault(resource.index, []).append(resource)
        child = (
            block5_children[block4_index]
            if block4_index < len(block5_children)
            else None
        )
        layout = (
            layouts.get(child.layout_reference)
            if child is not None
            else None
        )
        routes = ()
        route_error = None
        if (
            vertex_shader is not None
            and pixel_shader is not None
            and layout is not None
            and layout.fields
        ):
            try:
                routes = tuple(
                    ShaderTextureRoute(
                        texture_index=route.texture_index,
                        uv_offset=route.uv_offset,
                        source_semantic_index=route.source_semantic_index,
                        source_components=route.source_components,
                        scale=route.scale,
                        offset=route.offset,
                    )
                    for route in texture_coordinate_routes(
                        vertex_shader.data,
                        pixel_shader.data,
                        layout.uv_offset,
                    )
                )
            except CorruptDataError as exc:
                route_error = str(exc)
        resolved = (
            ShaderVariant(
                table_index=block4_index,
                vertex_shader_index=item.vertex_shader_index,
                pixel_shader_index=item.pixel_shader_index,
                parameter_group_index=parameter_group_index,
                layout=layout,
                texture_routes=routes,
                route_error=route_error,
            ),
            {
                index: tuple(values)
                for index, values in resources.items()
            },
        )
        variant_cache[block4_index] = resolved
        return resolved

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
        texture_dimensions: dict[tuple[int, str], set[int]] = {}
        variants = []
        for block4_index in block7.get(reference, ()):
            variant, shader_resources_by_index = resolve_variant(
                block4_index
            )
            variants.append(variant)
            parameter_group_index = variant.parameter_group_index
            for texture in parameter_groups[parameter_group_index]:
                identity = (texture.index, texture.path.casefold())
                if identity not in texture_parameters:
                    textures.append(texture)
                    texture_parameters[identity] = []
                parameters = texture_parameters[identity]
                dimensions = texture_dimensions.setdefault(identity, set())
                for resource in shader_resources_by_index.get(
                    texture.slot,
                    (),
                ):
                    if resource.name and resource.name not in parameters:
                        parameters.append(resource.name)
                    dimensions.add(resource.dimension)
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
                        dimension=(
                            next(
                                iter(
                                    texture_dimensions[
                                        (
                                            texture.index,
                                            texture.path.casefold(),
                                        )
                                    ]
                                )
                            )
                            if len(
                                texture_dimensions[
                                    (
                                        texture.index,
                                        texture.path.casefold(),
                                    )
                                ]
                            )
                            == 1
                            else None
                        ),
                    )
                    for texture in textures
                ),
                variants=tuple(variants),
            )
        )

    return ShaderDatabase(
        materials=tuple(materials),
        trailing_bytes=len(data) - stream.tell(),
        layouts=tuple(layouts.values()),
    )
