# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable

from .errors import CorruptDataError


_OPCODE_ADD = 0
_OPCODE_DIV = 14
_OPCODE_EXP = 25
_OPCODE_FRC = 26
_OPCODE_FTOI = 27
_OPCODE_FTOU = 28
_OPCODE_MAD = 50
_OPCODE_CUSTOM_DATA = 53
_OPCODE_ELSE = 18
_OPCODE_ENDIF = 21
_OPCODE_IF = 31
_OPCODE_ITOF = 43
_OPCODE_LOG = 47
_OPCODE_MIN = 51
_OPCODE_MAX = 52
_OPCODE_MOV = 54
_OPCODE_MOVC = 55
_OPCODE_MUL = 56
_OPCODE_ROUND_NE = 64
_OPCODE_ROUND_NI = 65
_OPCODE_ROUND_PI = 66
_OPCODE_ROUND_Z = 67
_OPCODE_RSQ = 68
_OPCODE_SAMPLE = 69
_OPCODE_SAMPLE_C = 70
_OPCODE_SAMPLE_C_LZ = 71
_OPCODE_SAMPLE_L = 72
_OPCODE_SAMPLE_D = 73
_OPCODE_SAMPLE_B = 74
_OPCODE_SQRT = 75

_OPERAND_TEMP = 0
_OPERAND_INPUT = 1
_OPERAND_OUTPUT = 2
_OPERAND_IMMEDIATE32 = 4
_OPERAND_RESOURCE = 7

_COMPONENTS = (0, 1, 2, 3)


@dataclass(frozen=True)
class DxbcResourceBinding:
    index: int
    name: str
    dimension: int


@dataclass(frozen=True)
class DxbcCoordinateRoute:
    texture_index: int
    uv_offset: int
    source_semantic_index: int
    source_components: tuple[int, int]
    scale: tuple[float | None, float | None]
    offset: tuple[float | None, float | None]


@dataclass(frozen=True)
class _SignatureParameter:
    semantic: str
    semantic_index: int
    register: int
    mask: int


@dataclass(frozen=True)
class _Expression:
    register: int | None
    component: int | None
    scale: float | None
    offset: float | None

    @classmethod
    def source(cls, register: int, component: int):
        return cls(register, component, 1.0, 0.0)

    @classmethod
    def constant(cls, value: float):
        return cls(None, None, 0.0, value)

    @classmethod
    def unknown_constant(cls):
        return cls(None, None, None, None)

    @property
    def is_constant(self) -> bool:
        return self.register is None


@dataclass(frozen=True)
class _Operand:
    operand_type: int
    indices: tuple[int | None, ...]
    components: tuple[int, ...]
    values: tuple[float, ...] = ()
    modifier: int | None = 0

    @property
    def register(self) -> int | None:
        return self.indices[0] if self.indices else None

    def component(self, destination_component: int) -> int:
        if not self.components:
            return 0
        if len(self.components) == 1:
            return self.components[0]
        if destination_component in self.components and len(self.components) < 4:
            return destination_component
        if destination_component >= len(self.components):
            return self.components[-1]
        return self.components[destination_component]


@dataclass(frozen=True)
class _Instruction:
    opcode: int
    operands: tuple[_Operand, ...]
    saturate: bool = False


def _chunks(data: bytes) -> dict[bytes, bytes]:
    if len(data) < 32 or data[:4] != b"DXBC":
        raise CorruptDataError("DXBC container header is invalid")
    total_size, chunk_count = struct.unpack_from("<II", data, 24)
    if total_size > len(data) or chunk_count > 1_000:
        raise CorruptDataError("DXBC container size is invalid")
    table_end = 32 + chunk_count * 4
    if table_end > total_size:
        raise CorruptDataError("DXBC chunk table exceeds the container")
    result = {}
    for chunk_offset in struct.unpack_from(
        f"<{chunk_count}I", data, 32
    ):
        if chunk_offset + 8 > total_size:
            raise CorruptDataError("DXBC chunk offset exceeds the container")
        name = data[chunk_offset : chunk_offset + 4]
        size = struct.unpack_from("<I", data, chunk_offset + 4)[0]
        start = chunk_offset + 8
        end = start + size
        if end > total_size:
            raise CorruptDataError("DXBC chunk exceeds the container")
        result[name] = data[start:end]
    return result


def rdef_texture_bindings(data: bytes) -> tuple[DxbcResourceBinding, ...]:
    if len(data) < 16:
        raise CorruptDataError("DXBC RDEF chunk is too short")
    resource_count, resource_offset = struct.unpack_from("<II", data, 8)
    if resource_count > 100_000:
        raise CorruptDataError(
            f"Unsafe DXBC resource-binding count: {resource_count:,}"
        )
    resource_end = resource_offset + resource_count * 32
    if resource_end > len(data):
        raise CorruptDataError("DXBC resource bindings exceed the RDEF chunk")

    bindings = []
    for resource_index in range(resource_count):
        offset = resource_offset + resource_index * 32
        values = struct.unpack_from("<8I", data, offset)
        name_offset, input_type = values[:2]
        if input_type != 2:
            continue
        if name_offset >= len(data):
            raise CorruptDataError("DXBC resource name exceeds the RDEF chunk")
        end = data.find(b"\0", name_offset)
        if end < 0:
            raise CorruptDataError("DXBC resource name is not terminated")
        bindings.append(
            DxbcResourceBinding(
                index=values[5],
                name=data[name_offset:end].decode("utf-8", "replace"),
                dimension=values[3],
            )
        )
    return tuple(bindings)


def shader_resources(data: bytes) -> tuple[DxbcResourceBinding, ...]:
    chunk = _chunks(data).get(b"RDEF")
    return rdef_texture_bindings(chunk) if chunk is not None else ()


def _signature(data: bytes, chunk_name: bytes) -> tuple[_SignatureParameter, ...]:
    chunk = _chunks(data).get(chunk_name)
    if chunk is None:
        return ()
    if len(chunk) < 8:
        raise CorruptDataError("DXBC signature chunk is too short")
    count = struct.unpack_from("<I", chunk, 0)[0]
    end = 8 + count * 24
    if count > 1_000 or end > len(chunk):
        raise CorruptDataError("DXBC signature entries exceed the chunk")
    result = []
    for index in range(count):
        offset = 8 + index * 24
        name_offset, semantic_index, _, _, register = struct.unpack_from(
            "<5I", chunk, offset
        )
        if name_offset >= len(chunk):
            raise CorruptDataError("DXBC signature name exceeds the chunk")
        name_end = chunk.find(b"\0", name_offset)
        if name_end < 0:
            raise CorruptDataError("DXBC signature name is not terminated")
        result.append(
            _SignatureParameter(
                semantic=chunk[name_offset:name_end].decode(
                    "ascii", "replace"
                ),
                semantic_index=semantic_index,
                register=register,
                mask=chunk[offset + 20],
            )
        )
    return tuple(result)


def _operand_components(token: int) -> tuple[int, ...]:
    component_count = token & 0x3
    if component_count == 0:
        return ()
    if component_count == 1:
        return (0,)
    if component_count != 2:
        raise CorruptDataError("Unsupported DXBC operand component count")
    selection_mode = (token >> 2) & 0x3
    if selection_mode == 0:
        mask = (token >> 4) & 0xF
        return tuple(component for component in _COMPONENTS if mask & (1 << component))
    if selection_mode == 1:
        return tuple((token >> (4 + component * 2)) & 0x3 for component in _COMPONENTS)
    if selection_mode == 2:
        return ((token >> 4) & 0x3,)
    raise CorruptDataError("Unsupported DXBC operand component selection")


def _parse_operand(
    tokens: tuple[int, ...],
    position: int,
    limit: int,
) -> tuple[_Operand, int]:
    if position >= limit:
        raise CorruptDataError("DXBC instruction is missing an operand")
    token = tokens[position]
    position += 1
    extended = bool(token & 0x80000000)
    modifier: int | None = 0
    while extended:
        if position >= limit:
            raise CorruptDataError("DXBC extended operand is truncated")
        extended_token = tokens[position]
        position += 1
        if (extended_token & 0x3F) == 0:
            modifier = (extended_token >> 6) & 0xFF
        else:
            modifier = None
        extended = bool(extended_token & 0x80000000)

    operand_type = (token >> 12) & 0xFF
    components = _operand_components(token)
    if (
        operand_type == _OPERAND_IMMEDIATE32
        and (token & 0x3) == 2
        and not components
    ):
        components = _COMPONENTS
    index_dimension = (token >> 20) & 0x3
    indices = []
    for dimension in range(index_dimension):
        representation = (token >> (22 + dimension * 3)) & 0x7
        if representation in {0, 3}:
            if position >= limit:
                raise CorruptDataError("DXBC immediate operand index is truncated")
            indices.append(tokens[position])
            position += 1
        elif representation in {1, 4}:
            if position + 2 > limit:
                raise CorruptDataError("DXBC 64-bit operand index is truncated")
            indices.append(tokens[position] | (tokens[position + 1] << 32))
            position += 2
        elif representation == 2:
            indices.append(None)
        else:
            raise CorruptDataError(
                "Unsupported DXBC operand index representation: "
                f"{representation}"
            )
        if representation in {2, 3, 4}:
            _, position = _parse_operand(tokens, position, limit)

    values = ()
    if operand_type == _OPERAND_IMMEDIATE32:
        value_count = 4 if (token & 0x3) == 2 else 1
        if position + value_count > limit:
            raise CorruptDataError("DXBC immediate operand value is truncated")
        values = tuple(
            struct.unpack("<f", struct.pack("<I", value))[0]
            for value in tokens[position : position + value_count]
        )
        position += value_count
    return (
        _Operand(
            operand_type=operand_type,
            indices=tuple(indices),
            components=components,
            values=values,
            modifier=modifier,
        ),
        position,
    )


_OPERAND_COUNTS = {
    _OPCODE_ADD: 3,
    _OPCODE_DIV: 3,
    _OPCODE_ELSE: 0,
    _OPCODE_ENDIF: 0,
    _OPCODE_EXP: 2,
    _OPCODE_FRC: 2,
    _OPCODE_FTOI: 2,
    _OPCODE_FTOU: 2,
    _OPCODE_IF: 1,
    _OPCODE_ITOF: 2,
    _OPCODE_LOG: 2,
    _OPCODE_MAD: 4,
    _OPCODE_MIN: 3,
    _OPCODE_MAX: 3,
    _OPCODE_MOV: 2,
    _OPCODE_MOVC: 4,
    _OPCODE_MUL: 3,
    _OPCODE_ROUND_NE: 2,
    _OPCODE_ROUND_NI: 2,
    _OPCODE_ROUND_PI: 2,
    _OPCODE_ROUND_Z: 2,
    _OPCODE_RSQ: 2,
    _OPCODE_SAMPLE: 4,
    _OPCODE_SAMPLE_C: 5,
    _OPCODE_SAMPLE_C_LZ: 5,
    _OPCODE_SAMPLE_L: 5,
    _OPCODE_SAMPLE_D: 6,
    _OPCODE_SAMPLE_B: 5,
    _OPCODE_SQRT: 2,
}


def _instructions(data: bytes) -> tuple[_Instruction, ...]:
    chunks = _chunks(data)
    chunk = chunks.get(b"SHEX") or chunks.get(b"SHDR")
    if chunk is None:
        return ()
    if len(chunk) < 8 or len(chunk) % 4:
        raise CorruptDataError("DXBC instruction chunk size is invalid")
    tokens = struct.unpack(f"<{len(chunk) // 4}I", chunk)
    declared_length = tokens[1]
    if declared_length > len(tokens) or declared_length < 2:
        raise CorruptDataError("DXBC program length is invalid")
    result = []
    position = 2
    while position < declared_length:
        token = tokens[position]
        opcode = token & 0x7FF
        if opcode == _OPCODE_CUSTOM_DATA:
            if position + 1 >= declared_length:
                raise CorruptDataError("DXBC custom-data block is truncated")
            length = tokens[position + 1]
        else:
            length = (token >> 24) & 0x7F
        if length <= 0 or position + length > declared_length:
            raise CorruptDataError("DXBC instruction length is invalid")
        operand_count = _OPERAND_COUNTS.get(opcode)
        if operand_count is not None:
            operand_position = position + 1
            extended = bool(token & 0x80000000)
            while extended:
                if operand_position >= position + length:
                    raise CorruptDataError("DXBC extended opcode is truncated")
                extended_token = tokens[operand_position]
                operand_position += 1
                extended = bool(extended_token & 0x80000000)
            operands = []
            try:
                for _ in range(operand_count):
                    operand, operand_position = _parse_operand(
                        tokens,
                        operand_position,
                        position + length,
                    )
                    operands.append(operand)
            except CorruptDataError as exc:
                raise CorruptDataError(
                    f"DXBC opcode {opcode} operands are unsupported: {exc}"
                ) from exc
            result.append(
                _Instruction(
                    opcode,
                    tuple(operands),
                    bool(token & 0x00002000),
                )
            )
        else:
            result.append(_Instruction(opcode, ()))
        position += length
    return tuple(result)


def _constant_value(expression: _Expression) -> float | None:
    if not expression.is_constant:
        return None
    return expression.offset


def _add(first: _Expression, second: _Expression) -> _Expression | None:
    if first.is_constant and second.is_constant:
        if first.offset is None or second.offset is None:
            return _Expression.unknown_constant()
        return _Expression.constant(first.offset + second.offset)
    if first.is_constant:
        first, second = second, first
    if second.is_constant:
        return _Expression(
            first.register,
            first.component,
            first.scale,
            (
                first.offset + second.offset
                if first.offset is not None and second.offset is not None
                else None
            ),
        )
    if (
        first.register,
        first.component,
    ) != (
        second.register,
        second.component,
    ):
        return None
    return _Expression(
        first.register,
        first.component,
        (
            first.scale + second.scale
            if first.scale is not None and second.scale is not None
            else None
        ),
        (
            first.offset + second.offset
            if first.offset is not None and second.offset is not None
            else None
        ),
    )


def _multiply(first: _Expression, second: _Expression) -> _Expression | None:
    if first.is_constant and second.is_constant:
        if first.offset is None or second.offset is None:
            return _Expression.unknown_constant()
        return _Expression.constant(first.offset * second.offset)
    if first.is_constant:
        first, second = second, first
    factor = _constant_value(second)
    if not second.is_constant:
        return None
    return _Expression(
        first.register,
        first.component,
        first.scale * factor
        if first.scale is not None and factor is not None
        else None,
        first.offset * factor
        if first.offset is not None and factor is not None
        else None,
    )


def _common_origin(
    *expressions: _Expression,
) -> _Expression | None:
    sources = tuple(
        expression for expression in expressions if not expression.is_constant
    )
    if not sources:
        return _Expression.unknown_constant()
    identity = {
        (expression.register, expression.component)
        for expression in sources
    }
    return _uncertain(sources[0]) if len(identity) == 1 else None


def _source_expression(
    operand: _Operand,
    component: int,
    state: dict[tuple[int, int, int], _Expression],
) -> _Expression | None:
    source_component = operand.component(component)
    if operand.operand_type == _OPERAND_IMMEDIATE32:
        if not operand.values:
            return None
        value_index = source_component if len(operand.values) > 1 else 0
        expression = _Expression.constant(operand.values[value_index])
    else:
        register = operand.register
        if register is None:
            expression = _Expression.unknown_constant()
        elif operand.operand_type == _OPERAND_INPUT:
            expression = _Expression.source(register, source_component)
        elif operand.operand_type in {_OPERAND_TEMP, _OPERAND_OUTPUT}:
            expression = state.get(
                (operand.operand_type, register, source_component)
            )
        else:
            expression = _Expression.unknown_constant()
    if expression is None or operand.modifier == 0:
        return expression
    if operand.modifier == 1:
        return _Expression(
            expression.register,
            expression.component,
            -expression.scale if expression.scale is not None else None,
            -expression.offset if expression.offset is not None else None,
        )
    if operand.modifier in {2, 3} and expression.is_constant:
        if expression.offset is None:
            return expression
        value = abs(expression.offset)
        return _Expression.constant(
            -value if operand.modifier == 3 else value
        )
    return _uncertain(expression)


def _destination_components(operand: _Operand) -> tuple[int, ...]:
    return operand.components or _COMPONENTS


def _uncertain(expression: _Expression) -> _Expression:
    if expression.is_constant:
        return _Expression.unknown_constant()
    return _Expression(
        expression.register,
        expression.component,
        None,
        None,
    )


def _merge_expression(
    first: _Expression | None,
    second: _Expression | None,
) -> _Expression | None:
    if first == second:
        return first
    if first is None:
        return _uncertain(second) if second is not None else None
    if second is None:
        return _uncertain(first)
    if first.is_constant and second.is_constant:
        return _Expression.unknown_constant()
    if first.is_constant:
        return _uncertain(second)
    if second.is_constant:
        return _uncertain(first)
    if (
        first.register,
        first.component,
    ) != (
        second.register,
        second.component,
    ):
        return None
    return _Expression(
        first.register,
        first.component,
        first.scale if first.scale == second.scale else None,
        first.offset if first.offset == second.offset else None,
    )


def _merge_states(
    first: dict[tuple[int, int, int], _Expression],
    second: dict[tuple[int, int, int], _Expression],
) -> dict[tuple[int, int, int], _Expression]:
    merged = {}
    for key in first.keys() | second.keys():
        expression = _merge_expression(first.get(key), second.get(key))
        if expression is not None:
            merged[key] = expression
    return merged


def _execute(
    data: bytes,
) -> tuple[
    dict[tuple[int, int, int], _Expression],
    tuple[tuple[int, _Expression | None, _Expression | None], ...],
]:
    state: dict[tuple[int, int, int], _Expression] = {}
    samples = []
    branches: list[
        tuple[
            dict[tuple[int, int, int], _Expression],
            dict[tuple[int, int, int], _Expression] | None,
        ]
    ] = []
    for instruction in _instructions(data):
        operands = instruction.operands
        if instruction.opcode == _OPCODE_IF:
            branches.append((dict(state), None))
            continue
        if instruction.opcode == _OPCODE_ELSE:
            if not branches:
                raise CorruptDataError("DXBC else has no matching if")
            entry_state, true_state = branches[-1]
            if true_state is not None:
                raise CorruptDataError("DXBC if contains multiple else blocks")
            branches[-1] = (entry_state, dict(state))
            state = dict(entry_state)
            continue
        if instruction.opcode == _OPCODE_ENDIF:
            if not branches:
                raise CorruptDataError("DXBC endif has no matching if")
            entry_state, true_state = branches.pop()
            if true_state is None:
                true_state = dict(state)
                false_state = entry_state
            else:
                false_state = dict(state)
            state = _merge_states(true_state, false_state)
            continue
        if not operands:
            state = {
                key: expression
                for key, expression in state.items()
                if key[0] != _OPERAND_TEMP
            }
            continue
        if instruction.opcode in {
            _OPCODE_SAMPLE,
            _OPCODE_SAMPLE_C,
            _OPCODE_SAMPLE_C_LZ,
            _OPCODE_SAMPLE_L,
            _OPCODE_SAMPLE_D,
            _OPCODE_SAMPLE_B,
        }:
            coordinate = operands[1]
            resource = operands[2]
            if resource.register is not None:
                samples.append(
                    (
                        resource.register,
                        _source_expression(coordinate, 0, state),
                        _source_expression(coordinate, 1, state),
                    )
                )
            continue
        destination = operands[0]
        if (
            destination.operand_type not in {_OPERAND_TEMP, _OPERAND_OUTPUT}
            or destination.register is None
        ):
            continue
        for component in _destination_components(destination):
            sources = [
                _source_expression(operand, component, state)
                for operand in operands[1:]
            ]
            expression = None
            if all(source is not None for source in sources):
                if instruction.opcode == _OPCODE_MOV:
                    expression = sources[0]
                elif instruction.opcode == _OPCODE_ADD:
                    expression = _add(sources[0], sources[1])
                elif instruction.opcode == _OPCODE_MUL:
                    expression = _multiply(sources[0], sources[1])
                elif instruction.opcode == _OPCODE_MAD:
                    product = _multiply(sources[0], sources[1])
                    if product is not None:
                        expression = _add(product, sources[2])
                elif instruction.opcode == _OPCODE_DIV:
                    denominator = _constant_value(sources[1])
                    if denominator not in {None, 0.0}:
                        expression = _multiply(
                            sources[0],
                            _Expression.constant(1.0 / denominator),
                        )
                    if expression is None:
                        expression = _common_origin(*sources)
                elif instruction.opcode in {
                    _OPCODE_EXP,
                    _OPCODE_FRC,
                    _OPCODE_FTOI,
                    _OPCODE_FTOU,
                    _OPCODE_ITOF,
                    _OPCODE_LOG,
                    _OPCODE_ROUND_NE,
                    _OPCODE_ROUND_NI,
                    _OPCODE_ROUND_PI,
                    _OPCODE_ROUND_Z,
                    _OPCODE_RSQ,
                    _OPCODE_SQRT,
                }:
                    expression = _uncertain(sources[0])
                elif instruction.opcode in {_OPCODE_MIN, _OPCODE_MAX}:
                    expression = _common_origin(*sources)
                elif instruction.opcode == _OPCODE_MOVC:
                    expression = _merge_expression(
                        sources[1],
                        sources[2],
                    )
            key = (
                destination.operand_type,
                destination.register,
                component,
            )
            if expression is None:
                state.pop(key, None)
            else:
                if instruction.saturate:
                    expression = _uncertain(expression)
                state[key] = expression
    if branches:
        raise CorruptDataError("DXBC if block is not terminated")
    return state, tuple(samples)


def _compose(
    pixel: _Expression,
    vertex: _Expression,
) -> tuple[float | None, float | None]:
    scale = (
        pixel.scale * vertex.scale
        if pixel.scale is not None and vertex.scale is not None
        else None
    )
    offset = (
        pixel.scale * vertex.offset + pixel.offset
        if (
            pixel.scale is not None
            and vertex.offset is not None
            and pixel.offset is not None
        )
        else None
    )
    return scale, offset


def texture_coordinate_routes(
    vertex_shader: bytes,
    pixel_shader: bytes,
    input_uv_offset: Callable[[int, int], int | None],
) -> tuple[DxbcCoordinateRoute, ...]:
    vertex_inputs = {
        parameter.register: parameter
        for parameter in _signature(vertex_shader, b"ISGN")
        if parameter.semantic.casefold() == "texcoord"
    }
    vertex_state, _ = _execute(vertex_shader)
    _, samples = _execute(pixel_shader)
    routes = []
    seen = set()
    for texture_index, pixel_x, pixel_y in samples:
        if (
            pixel_x is None
            or pixel_y is None
            or pixel_x.register is None
            or pixel_y.register is None
            or pixel_x.component is None
            or pixel_y.component is None
        ):
            continue
        vertex_x = vertex_state.get(
            (_OPERAND_OUTPUT, pixel_x.register, pixel_x.component)
        )
        vertex_y = vertex_state.get(
            (_OPERAND_OUTPUT, pixel_y.register, pixel_y.component)
        )
        if (
            vertex_x is None
            or vertex_y is None
            or vertex_x.register is None
            or vertex_y.register is None
            or vertex_x.component is None
            or vertex_y.component is None
        ):
            continue
        parameter_x = vertex_inputs.get(vertex_x.register)
        parameter_y = vertex_inputs.get(vertex_y.register)
        if parameter_x is None or parameter_y is None:
            continue
        uv_offset_x = input_uv_offset(
            parameter_x.semantic_index,
            vertex_x.component,
        )
        uv_offset_y = input_uv_offset(
            parameter_y.semantic_index,
            vertex_y.component,
        )
        if uv_offset_x is None or uv_offset_x != uv_offset_y:
            continue
        scale_x, offset_x = _compose(pixel_x, vertex_x)
        scale_y, offset_y = _compose(pixel_y, vertex_y)
        identity = (
            texture_index,
            uv_offset_x,
            vertex_x.component,
            vertex_y.component,
            scale_x,
            scale_y,
            offset_x,
            offset_y,
        )
        if identity in seen:
            continue
        seen.add(identity)
        routes.append(
            DxbcCoordinateRoute(
                texture_index=texture_index,
                uv_offset=uv_offset_x,
                source_semantic_index=parameter_x.semantic_index,
                source_components=(
                    vertex_x.component,
                    vertex_y.component,
                ),
                scale=(scale_x, scale_y),
                offset=(offset_x, offset_y),
            )
        )
    return tuple(routes)
