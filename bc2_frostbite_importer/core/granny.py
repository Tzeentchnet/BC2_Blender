# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import bisect
import math
import struct
from dataclasses import dataclass

from .errors import CorruptDataError, UnsupportedFormatError


_PC_MAGIC = bytes.fromhex(
    "29de6cc0baa4532b25f5b7a5f666e2"
    "eec80100000000000000000000000000"
)
_MAX_STRINGS = 1_000_000
_MAX_SECTIONS = 64
_MAX_BONES = 4_096
_MAX_TRACKS = 100_000


@dataclass(frozen=True)
class GrannyBone:
    name: str
    parent_index: int
    flags: int
    translation: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    scale_shear: tuple[float, ...]
    inverse_world_transform: tuple[float, ...]


@dataclass(frozen=True)
class GrannySkeleton:
    bones: tuple[GrannyBone, ...]


@dataclass(frozen=True)
class GrannyCurve:
    codec: str
    degree: int
    knots: tuple[float, ...]
    controls: tuple[tuple[float, ...], ...]

    @property
    def dimension(self) -> int:
        return len(self.controls[0])

    def evaluate(self, time: float) -> tuple[float, ...]:
        if len(self.controls) == 1 or self.degree == 0:
            return self.controls[0]
        if time <= self.knots[0]:
            return self.controls[0]
        if time >= self.knots[-1]:
            return self.controls[-1]
        knot_index = bisect.bisect_right(self.knots, time)
        knot_index = min(knot_index, len(self.knots) - 1)
        if self.degree == 1 or len(self.knots) < 4:
            index = min(max(knot_index, 1), len(self.knots) - 1)
            first_time = self.knots[index - 1]
            second_time = self.knots[index]
            width = second_time - first_time
            blend = 0.0 if abs(width) < 1e-12 else (time - first_time) / width
            blend = max(0.0, min(1.0, blend))
            first = self.controls[index - 1]
            second = self.controls[index]
            return tuple(
                (1.0 - blend) * first[axis] + blend * second[axis]
                for axis in range(self.dimension)
            )
        if self.degree == 2:
            center = min(max(knot_index, 2), len(self.knots) - 2)
            if center < 2 or center + 1 >= len(self.knots):
                index = min(max(knot_index, 1), len(self.knots) - 1)
                first_time = self.knots[index - 1]
                second_time = self.knots[index]
                width = second_time - first_time
                blend = (
                    0.0
                    if abs(width) < 1e-12
                    else (time - first_time) / width
                )
                blend = max(0.0, min(1.0, blend))
                first = self.controls[index - 1]
                second = self.controls[index]
                return tuple(
                    (1.0 - blend) * first[axis] + blend * second[axis]
                    for axis in range(self.dimension)
                )

            knot_2, knot_1, knot, knot1 = self.knots[
                center - 2 : center + 2
            ]

            def divide(numerator: float, denominator: float) -> float:
                return (
                    0.0
                    if abs(denominator) < 1e-12
                    else numerator / denominator
                )

            lower = divide(time - knot_1, knot - knot_1)
            upper_1 = divide(time - knot_2, knot - knot_2)
            upper_2 = divide(time - knot_1, knot1 - knot_1)
            combined = (upper_1 + lower) - lower * upper_1
            current_weight = lower * upper_2
            previous_weight = combined - current_weight
            earlier_weight = 1.0 - combined
            earlier = self.controls[center - 2]
            previous = self.controls[center - 1]
            current = self.controls[center]
            return tuple(
                earlier_weight * earlier[axis]
                + previous_weight * previous[axis]
                + current_weight * current[axis]
                for axis in range(self.dimension)
            )
        if self.degree == 3:
            center = knot_index

            def knot(relative_index: int) -> float:
                index = min(
                    max(center + relative_index, 0),
                    len(self.knots) - 1,
                )
                return self.knots[index]

            def divide(numerator: float, denominator: float) -> float:
                return (
                    0.0
                    if abs(denominator) < 1e-12
                    else numerator / denominator
                )

            knot_3 = knot(-3)
            knot_2 = knot(-2)
            knot_1 = knot(-1)
            current_knot = knot(0)
            knot1 = knot(1)
            knot2 = knot(2)
            lower = divide(time - knot_1, current_knot - knot_1)
            middle_1 = divide(time - knot_2, current_knot - knot_2)
            middle_2 = divide(time - knot_1, knot1 - knot_1)
            upper_1 = divide(time - knot_3, current_knot - knot_3)
            upper_2 = divide(time - knot_2, knot1 - knot_2)
            upper_3 = divide(time - knot_1, knot2 - knot_1)

            inverse_lower = 1.0 - lower
            inverse_middle_1 = 1.0 - middle_1
            inverse_middle_2 = 1.0 - middle_2
            inverse_upper_1 = 1.0 - upper_1
            inverse_upper_2 = 1.0 - upper_2
            inverse_upper_3 = 1.0 - upper_3
            weights = (
                inverse_lower * inverse_middle_1 * inverse_upper_1,
                (
                    inverse_lower * inverse_middle_1 * upper_1
                    + inverse_lower * middle_1 * inverse_upper_2
                    + lower * inverse_middle_2 * inverse_upper_2
                ),
                (
                    inverse_lower * middle_1 * upper_2
                    + lower * inverse_middle_2 * upper_2
                    + lower * middle_2 * inverse_upper_3
                ),
                lower * middle_2 * upper_3,
            )
            control_indices = tuple(
                min(max(center + offset, 0), len(self.controls) - 1)
                for offset in (-3, -2, -1, 0)
            )
            return tuple(
                sum(
                    weight * self.controls[index][axis]
                    for weight, index in zip(weights, control_indices)
                )
                for axis in range(self.dimension)
            )
        return self.controls[min(knot_index, len(self.controls) - 1)]


@dataclass(frozen=True)
class GrannyTransformTrack:
    name: str
    flags: int
    rotation: GrannyCurve
    translation: GrannyCurve
    scale_shear: GrannyCurve

    def rotation_at(self, time: float) -> tuple[float, float, float, float]:
        value = self.rotation.evaluate(time)
        length = math.sqrt(sum(component * component for component in value))
        if length <= 1e-12:
            raise CorruptDataError(
                f"Animation track {self.name!r} has a zero quaternion"
            )
        return tuple(component / length for component in value)


@dataclass(frozen=True)
class GrannyAnimation:
    name: str
    duration: float
    time_step: float
    oversampling: float
    tracks: tuple[GrannyTransformTrack, ...]
    additive_bone_indices: tuple[int, ...] = ()


@dataclass(frozen=True)
class _Section:
    data: bytes
    relocations: dict[int, tuple[int, int]]


class _Reader:
    def __init__(self, data: bytes, label: str):
        self.data = data
        self.label = label

    def require(self, offset: int, size: int, field: str):
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise CorruptDataError(
                f"{self.label} {field} exceeds its containing buffer"
            )

    def unpack(self, fmt: str, offset: int, field: str):
        size = struct.calcsize(fmt)
        self.require(offset, size, field)
        return struct.unpack_from(fmt, self.data, offset)

    def slice(self, offset: int, size: int, field: str) -> bytes:
        self.require(offset, size, field)
        return self.data[offset : offset + size]


def _require_pc(platform: str | None):
    if platform is not None and platform.casefold() != "pc":
        raise UnsupportedFormatError(
            "Native Granny skeleton and animation decoding is currently "
            "validated for PC assets only"
        )


def _parse_strings(
    data: bytes,
    *,
    animation: bool,
) -> tuple[tuple[str, ...], int, tuple[int, ...]]:
    reader = _Reader(data, "Frostbite Granny wrapper")
    version = reader.unpack("<I", 0, "version")[0]
    if version != 10:
        raise UnsupportedFormatError(
            f"Unsupported Frostbite Granny wrapper version: {version}"
        )
    position = 4
    additive_bone_indices = ()
    if animation:
        additive_count = reader.unpack(
            "<I", position, "additive bone count"
        )[0]
        position += 4
        if additive_count > _MAX_BONES:
            raise CorruptDataError(
                "Unsafe Granny additive bone count: "
                f"{additive_count:,}"
            )
        if additive_count:
            additive_bone_indices = reader.unpack(
                "<" + "I" * additive_count,
                position,
                "additive bone indices",
            )
            position += additive_count * 4
            if (
                len(set(additive_bone_indices)) != additive_count
                or any(index >= _MAX_BONES for index in additive_bone_indices)
            ):
                raise CorruptDataError(
                    "Granny additive bone indices are invalid"
                )
            reserved = reader.unpack(
                "<I", position, "additive reserved field"
            )[0]
            position += 4
            if reserved != 0:
                raise UnsupportedFormatError(
                    "Unsupported Granny additive wrapper marker: "
                    f"{reserved}"
                )
    string_count = reader.unpack("<I", position, "string count")[0]
    position += 4
    if string_count > _MAX_STRINGS:
        raise CorruptDataError(
            f"Unsafe Granny wrapper string count: {string_count:,}"
        )
    strings = []
    for index in range(string_count):
        length = reader.unpack("<H", position, f"string {index} length")[0]
        position += 2
        raw = reader.slice(position, length, f"string {index}")
        position += length
        strings.append(raw.decode("latin-1"))
    return tuple(strings), position, additive_bone_indices


def _parse_container(data: bytes, search_start: int) -> tuple[_Section, ...]:
    magic_position = data.find(_PC_MAGIC, search_start)
    if magic_position < 0 or magic_position - search_start > 64:
        raise CorruptDataError("PC Granny v7 magic was not found after the strings")
    granny = data[magic_position:]
    reader = _Reader(granny, "Granny container")
    header = reader.unpack("<18I", 32, "header")
    version, file_size, _, section_offset, section_count = header[:5]
    if version != 7:
        raise UnsupportedFormatError(f"Unsupported Granny version: {version}")
    if file_size != len(granny):
        raise CorruptDataError(
            "Granny declared file size does not match the resource"
        )
    if not 1 <= section_count <= _MAX_SECTIONS:
        raise CorruptDataError(
            f"Unsafe Granny section count: {section_count}"
        )
    descriptor_start = 32 + section_offset
    descriptors = []
    for index in range(section_count):
        values = reader.unpack(
            "<11I",
            descriptor_start + index * 44,
            f"section {index} descriptor",
        )
        (
            compression,
            offset,
            compressed_size,
            uncompressed_size,
            _,
            _,
            _,
            relocation_offset,
            relocation_count,
            _,
            _,
        ) = values
        if compression != 0:
            raise UnsupportedFormatError(
                f"Unsupported Granny section compression {compression} "
                f"in section {index}"
            )
        if compressed_size != uncompressed_size:
            raise CorruptDataError(
                f"Uncompressed Granny section {index} has inconsistent sizes"
            )
        reader.require(offset, uncompressed_size, f"section {index} data")
        reader.require(
            relocation_offset,
            relocation_count * 12,
            f"section {index} relocations",
        )
        descriptors.append(
            (
                offset,
                uncompressed_size,
                relocation_offset,
                relocation_count,
            )
        )

    sections = []
    for section_index, (
        offset,
        size,
        relocation_offset,
        relocation_count,
    ) in enumerate(descriptors):
        section_data = reader.slice(offset, size, f"section {section_index}")
        relocations = {}
        for index in range(relocation_count):
            source, target_section, target_offset = reader.unpack(
                "<3I",
                relocation_offset + index * 12,
                f"section {section_index} relocation {index}",
            )
            if source in relocations:
                raise CorruptDataError(
                    f"Duplicate Granny relocation at section {section_index}:"
                    f"{source}"
                )
            if source + 4 > size:
                raise CorruptDataError(
                    f"Granny relocation source exceeds section {section_index}"
                )
            if target_section >= section_count:
                raise CorruptDataError(
                    f"Granny relocation targets missing section {target_section}"
                )
            target_size = descriptors[target_section][1]
            if target_offset > target_size:
                raise CorruptDataError(
                    f"Granny relocation target exceeds section {target_section}"
                )
            relocations[source] = (target_section, target_offset)
        sections.append(_Section(section_data, relocations))
    return tuple(sections)


def _finite(values: tuple[float, ...], field: str) -> tuple[float, ...]:
    if any(not math.isfinite(value) for value in values):
        raise CorruptDataError(f"Granny {field} contains a non-finite value")
    return values


def _target(
    sections: tuple[_Section, ...],
    section_index: int,
    source_offset: int,
    field: str,
) -> tuple[int, int]:
    relocation = sections[section_index].relocations.get(source_offset)
    if relocation is None:
        raise CorruptDataError(f"Granny {field} pointer has no relocation")
    return relocation


def parse_granny_model(
    data: bytes,
    platform: str | None = "pc",
) -> GrannySkeleton:
    _require_pc(platform)
    strings, strings_end, _ = _parse_strings(data, animation=False)
    sections = _parse_container(data, strings_end)
    if not sections:
        raise CorruptDataError("Granny model has no sections")
    section = sections[0]
    reader = _Reader(section.data, "Granny model section 0")
    bone_count = reader.unpack("<I", 172, "bone count")[0]
    if not 1 <= bone_count <= _MAX_BONES:
        raise CorruptDataError(f"Unsafe Granny bone count: {bone_count}")
    bone_section, bone_offset = _target(
        sections, 0, 176, "bone array"
    )
    if bone_section != 0:
        raise UnsupportedFormatError(
            "Granny skeleton bone arrays outside section 0 are unsupported"
        )
    reader.require(bone_offset, bone_count * 152, "bone records")

    bones = []
    names = set()
    for index in range(bone_count):
        offset = bone_offset + index * 152
        name_index, parent_index, flags = reader.unpack(
            "<IiI", offset, f"bone {index} header"
        )
        if name_index >= len(strings):
            raise CorruptDataError(
                f"Granny bone {index} name index is out of range"
            )
        name = strings[name_index]
        if not name or name in names:
            raise CorruptDataError(
                f"Granny bone name is empty or duplicated: {name!r}"
            )
        if parent_index < -1 or parent_index >= index:
            raise CorruptDataError(
                f"Granny bone {index} has invalid parent {parent_index}"
            )
        translation = _finite(
            reader.unpack("<3f", offset + 12, f"bone {index} translation"),
            f"bone {index} translation",
        )
        rotation = _finite(
            reader.unpack("<4f", offset + 24, f"bone {index} rotation"),
            f"bone {index} rotation",
        )
        rotation_length = math.sqrt(
            sum(component * component for component in rotation)
        )
        if rotation_length <= 1e-12:
            raise CorruptDataError(
                f"Granny bone {index} has a zero rotation quaternion"
            )
        rotation = tuple(
            component / rotation_length for component in rotation
        )
        scale_shear = _finite(
            reader.unpack("<9f", offset + 40, f"bone {index} scale/shear"),
            f"bone {index} scale/shear",
        )
        inverse_world = _finite(
            reader.unpack(
                "<16f",
                offset + 76,
                f"bone {index} inverse world transform",
            ),
            f"bone {index} inverse world transform",
        )
        bones.append(
            GrannyBone(
                name=name,
                parent_index=parent_index,
                flags=flags,
                translation=translation,
                rotation=rotation,
                scale_shear=scale_shear,
                inverse_world_transform=inverse_world,
            )
        )
        names.add(name)
    return GrannySkeleton(tuple(bones))


def _quaternion_scale_offset_table():
    half_root = math.sqrt(0.5)
    return (
        (half_root * 2.0, -half_root),
        (half_root, -half_root * 0.5),
        (half_root * 0.5, -half_root * 0.75),
        (half_root * 0.5, -half_root * 0.25),
        (half_root * 0.5, half_root * 0.25),
        (half_root * 0.25, -half_root * 0.25),
        (half_root * 0.25, -half_root * 0.125),
        (half_root * 0.25, 0.0),
        (-half_root * 2.0, half_root),
        (-half_root, half_root * 0.5),
        (-half_root * 0.5, half_root * 0.75),
        (-half_root * 0.5, half_root * 0.25),
        (-half_root * 0.5, -half_root * 0.25),
        (-half_root * 0.25, half_root * 0.25),
        (-half_root * 0.25, half_root * 0.125),
        (-half_root * 0.25, 0.0),
    )


def _curve_type_names(
    sections: tuple[_Section, ...],
    strings: tuple[str, ...],
) -> dict[int, str]:
    if len(sections) <= 6:
        raise CorruptDataError("Granny animation has no type-definition section")
    reader = _Reader(sections[6].data, "Granny type section")
    names = {}
    for offset in range(0, len(reader.data) - 31, 32):
        _, name_index = reader.unpack("<II", offset, "type member")
        if name_index >= len(strings):
            continue
        name = strings[name_index]
        if name.startswith("CurveDataHeader_"):
            names[offset] = name.removeprefix("CurveDataHeader_")
    return names


class _AnimationDecoder:
    def __init__(
        self,
        sections: tuple[_Section, ...],
        type_names: dict[int, str],
    ):
        self.sections = sections
        self.type_names = type_names

    def _reference(self, type_source: int) -> tuple[str, int, int]:
        type_section, type_offset = _target(
            self.sections, 0, type_source, "curve type"
        )
        if type_section != 6 or type_offset not in self.type_names:
            raise UnsupportedFormatError(
                "Animation curve references an unknown Granny type"
            )
        codec = self.type_names[type_offset]
        if codec == "DaIdentity":
            return codec, 0, 0
        data_section, data_offset = _target(
            self.sections, 0, type_source + 4, "curve data"
        )
        return codec, data_section, data_offset

    def _constant(
        self,
        codec: str,
        section_index: int,
        offset: int,
        dimension: int,
    ) -> GrannyCurve:
        reader = _Reader(
            self.sections[section_index].data,
            f"Granny {codec} curve",
        )
        controls = _finite(
            reader.unpack(
                "<" + "f" * dimension,
                offset + 4,
                "constant controls",
            ),
            f"{codec} controls",
        )
        return GrannyCurve(codec, 0, (0.0,), (controls,))

    def _uncompressed(
        self,
        codec: str,
        section_index: int,
        offset: int,
        expected_dimension: int,
    ) -> GrannyCurve:
        reader = _Reader(
            self.sections[section_index].data,
            f"Granny {codec} curve",
        )
        _, degree, _, _ = reader.unpack(
            "<4B", offset, "curve header"
        )
        if degree > 3:
            raise UnsupportedFormatError(
                f"Unsupported Granny curve degree {degree}"
            )
        dimension = expected_dimension
        knot_count = reader.unpack("<I", offset + 4, "knot count")[0]
        control_count = reader.unpack("<I", offset + 12, "control count")[0]
        if not knot_count or control_count != knot_count * dimension:
            raise CorruptDataError(f"{codec} has inconsistent array counts")
        knot_section, knot_offset = _target(
            self.sections,
            section_index,
            offset + 8,
            f"{codec} knots",
        )
        control_section, control_offset = _target(
            self.sections,
            section_index,
            offset + 16,
            f"{codec} controls",
        )
        knot_reader = _Reader(
            self.sections[knot_section].data,
            f"Granny {codec} knots",
        )
        control_reader = _Reader(
            self.sections[control_section].data,
            f"Granny {codec} controls",
        )
        knots = _finite(
            knot_reader.unpack(
                "<" + "f" * knot_count,
                knot_offset,
                "values",
            ),
            f"{codec} knots",
        )
        values = _finite(
            control_reader.unpack(
                "<" + "f" * control_count,
                control_offset,
                "values",
            ),
            f"{codec} controls",
        )
        controls = tuple(
            tuple(values[index : index + dimension])
            for index in range(0, control_count, dimension)
        )
        return _validated_curve(codec, degree, knots, controls)

    def rotation(self, type_source: int) -> GrannyCurve:
        codec, section_index, offset = self._reference(type_source)
        if codec == "DaIdentity":
            return GrannyCurve(
                codec,
                0,
                (0.0,),
                ((0.0, 0.0, 0.0, 1.0),),
            )
        if codec == "D4Constant32f":
            return self._constant(codec, section_index, offset, 4)
        if codec == "DaK32fC32f":
            return self._uncompressed(
                codec, section_index, offset, 4
            )
        if codec not in {"D4nK8uC7u", "D4nK16uC15u"}:
            raise UnsupportedFormatError(
                f"Unsupported Granny rotation codec: {codec}"
            )
        reader = _Reader(
            self.sections[section_index].data,
            f"Granny {codec} curve",
        )
        _, degree, scale_entries = reader.unpack(
            "<BBH", offset, "header"
        )
        if degree > 3:
            raise UnsupportedFormatError(
                f"Unsupported Granny curve degree {degree}"
            )
        one_over_knot_scale = reader.unpack(
            "<f", offset + 4, "knot scale"
        )[0]
        count = reader.unpack("<I", offset + 8, "value count")[0]
        if count == 0 or count % 4:
            raise CorruptDataError(f"{codec} has an invalid value count")
        value_section, value_offset = _target(
            self.sections,
            section_index,
            offset + 12,
            f"{codec} values",
        )
        value_reader = _Reader(
            self.sections[value_section].data,
            f"Granny {codec} values",
        )
        is_16_bit = codec == "D4nK16uC15u"
        value_format = "H" if is_16_bit else "B"
        values = value_reader.unpack(
            "<" + value_format * count,
            value_offset,
            "values",
        )
        knot_count = count // 4
        knot_scale = (
            1.0 / one_over_knot_scale
            if one_over_knot_scale != 0.0
            else 1.0
        )
        knots = tuple(
            knot_scale * value for value in values[:knot_count]
        )
        quantized_max = 32767.0 if is_16_bit else 127.0
        table = _quaternion_scale_offset_table()
        scales = []
        offsets = []
        entries = scale_entries
        for _ in range(4):
            table_index = entries & 0xF
            entries >>= 4
            scales.append(table[table_index][0] / quantized_max)
            offsets.append(table[table_index][1])
        sign_bit = 0x8000 if is_16_bit else 0x80
        value_mask = 0x7FFF if is_16_bit else 0x7F
        first_shift = 14 if is_16_bit else 6
        second_shift = 15 if is_16_bit else 7
        control_values = values[knot_count:]
        controls = []
        for index in range(knot_count):
            packed = control_values[index * 3 : index * 3 + 3]
            missing_negative = bool(packed[0] & sign_bit)
            missing_index = (
                ((packed[1] >> first_shift) & 0x2)
                | (packed[2] >> second_shift)
            )
            result = [0.0, 0.0, 0.0, 0.0]
            destination = missing_index
            sum_squared = 0.0
            for source in range(3):
                destination = (destination + 1) & 3
                value = (
                    offsets[destination]
                    + scales[destination] * (packed[source] & value_mask)
                )
                result[destination] = value
                sum_squared += value * value
            missing = math.sqrt(max(0.0, 1.0 - sum_squared))
            result[missing_index] = (
                -missing if missing_negative else missing
            )
            controls.append(tuple(result))
        return _validated_curve(codec, degree, knots, tuple(controls))

    def translation(self, type_source: int) -> GrannyCurve:
        codec, section_index, offset = self._reference(type_source)
        if codec == "DaIdentity":
            return GrannyCurve(codec, 0, (0.0,), ((0.0, 0.0, 0.0),))
        if codec == "D3Constant32f":
            return self._constant(codec, section_index, offset, 3)
        if codec == "DaK32fC32f":
            return self._uncompressed(
                codec, section_index, offset, 3
            )
        supported = {
            "D3I1K32fC32f",
            "D3I1K8uC8u",
            "D3I1K16uC16u",
            "D3K8uC8u",
            "D3K16uC16u",
        }
        if codec not in supported:
            raise UnsupportedFormatError(
                f"Unsupported Granny translation codec: {codec}"
            )
        reader = _Reader(
            self.sections[section_index].data,
            f"Granny {codec} curve",
        )
        _, degree, one_over_truncated = reader.unpack(
            "<BBH", offset, "header"
        )
        if degree > 3:
            raise UnsupportedFormatError(
                f"Unsupported Granny curve degree {degree}"
            )
        scales = _finite(
            reader.unpack("<3f", offset + 4, "control scales"),
            f"{codec} scales",
        )
        offsets = _finite(
            reader.unpack("<3f", offset + 16, "control offsets"),
            f"{codec} offsets",
        )
        count = reader.unpack("<I", offset + 28, "value count")[0]
        divisor = 2 if codec.startswith("D3I1") else 4
        if count == 0 or count % divisor:
            raise CorruptDataError(f"{codec} has an invalid value count")
        value_section, value_offset = _target(
            self.sections,
            section_index,
            offset + 32,
            f"{codec} values",
        )
        value_reader = _Reader(
            self.sections[value_section].data,
            f"Granny {codec} values",
        )
        is_float = codec == "D3I1K32fC32f"
        is_16_bit = "16u" in codec
        value_format = "f" if is_float else ("H" if is_16_bit else "B")
        values = value_reader.unpack(
            "<" + value_format * count,
            value_offset,
            "values",
        )
        if is_float:
            knot_scale = 1.0
        else:
            one_over_knot_scale = struct.unpack(
                "<f", struct.pack("<I", one_over_truncated << 16)
            )[0]
            knot_scale = (
                1.0 / one_over_knot_scale
                if one_over_knot_scale != 0.0
                else 1.0
            )
        knot_count = count // divisor
        knots = tuple(
            knot_scale * value for value in values[:knot_count]
        )
        packed_controls = values[knot_count:]
        if codec.startswith("D3I1"):
            controls = tuple(
                tuple(
                    offsets[axis] + scales[axis] * value
                    for axis in range(3)
                )
                for value in packed_controls
            )
        else:
            controls = tuple(
                tuple(
                    offsets[axis]
                    + scales[axis] * packed_controls[index * 3 + axis]
                    for axis in range(3)
                )
                for index in range(knot_count)
            )
        return _validated_curve(codec, degree, knots, controls)

    def scale_shear(self, type_source: int) -> GrannyCurve:
        codec, section_index, offset = self._reference(type_source)
        identity = (
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            1.0,
        )
        if codec == "DaIdentity":
            return GrannyCurve(codec, 0, (0.0,), (identity,))
        if codec != "DaConstant32f":
            raise UnsupportedFormatError(
                f"Unsupported Granny scale/shear codec: {codec}"
            )
        reader = _Reader(
            self.sections[section_index].data,
            f"Granny {codec} curve",
        )
        _, degree, _, _ = reader.unpack("<4B", offset, "curve header")
        if degree != 0:
            raise UnsupportedFormatError(
                f"Unsupported Granny constant curve degree {degree}"
            )
        control_count = reader.unpack(
            "<I", offset + 4, "control count"
        )[0]
        if control_count != 9:
            raise CorruptDataError(
                f"{codec} has {control_count} scale/shear controls, expected 9"
            )
        control_section, control_offset = _target(
            self.sections,
            section_index,
            offset + 8,
            f"{codec} controls",
        )
        control_reader = _Reader(
            self.sections[control_section].data,
            f"Granny {codec} controls",
        )
        controls = _finite(
            control_reader.unpack("<9f", control_offset, "values"),
            f"{codec} controls",
        )
        return GrannyCurve(codec, 0, (0.0,), (controls,))


def _validated_curve(
    codec: str,
    degree: int,
    knots: tuple[float, ...],
    controls: tuple[tuple[float, ...], ...],
) -> GrannyCurve:
    _finite(knots, f"{codec} knots")
    if not knots or len(knots) != len(controls):
        raise CorruptDataError(f"{codec} has inconsistent knot/control counts")
    if any(
        len(control) != len(controls[0])
        or any(not math.isfinite(value) for value in control)
        for control in controls
    ):
        raise CorruptDataError(f"{codec} controls are invalid")
    if any(
        knots[index] > knots[index + 1]
        for index in range(len(knots) - 1)
    ):
        raise CorruptDataError(f"{codec} knots are not sorted")
    return GrannyCurve(codec, degree, knots, controls)


def parse_granny_animation(
    data: bytes,
    platform: str | None = "pc",
    name: str = "Animation",
) -> GrannyAnimation:
    _require_pc(platform)
    strings, strings_end, additive_bone_indices = _parse_strings(
        data, animation=True
    )
    sections = _parse_container(data, strings_end)
    section = sections[0]
    reader = _Reader(section.data, "Granny animation section 0")
    animation_count = reader.unpack("<I", 76, "animation count")[0]
    if animation_count != 1:
        raise UnsupportedFormatError(
            f"Expected one Granny animation, found {animation_count}"
        )
    array_section, array_offset = _target(
        sections, 0, 80, "animation array"
    )
    if array_section != 0:
        raise UnsupportedFormatError(
            "Granny animation arrays outside section 0 are unsupported"
        )
    animation_section, animation_offset = _target(
        sections, 0, array_offset, "animation"
    )
    if animation_section != 0:
        raise UnsupportedFormatError(
            "Granny animation objects outside section 0 are unsupported"
        )
    _, duration, time_step, oversampling, group_count = reader.unpack(
        "<IfffI", animation_offset, "animation object"
    )
    if (
        not math.isfinite(duration)
        or not math.isfinite(time_step)
        or not math.isfinite(oversampling)
        or duration <= 0.0
        or time_step <= 0.0
        or oversampling <= 0.0
    ):
        raise CorruptDataError("Granny animation timing is invalid")
    if not 1 <= group_count <= _MAX_TRACKS:
        raise CorruptDataError(
            f"Unsafe Granny track-group count: {group_count}"
        )
    group_array_section, group_array_offset = _target(
        sections,
        0,
        animation_offset + 20,
        "track-group array",
    )
    if group_array_section != 0:
        raise UnsupportedFormatError(
            "Granny track-group arrays outside section 0 are unsupported"
        )

    decoder = _AnimationDecoder(
        sections, _curve_type_names(sections, strings)
    )
    tracks = []
    track_names = set()
    for group_index in range(group_count):
        group_section, group_offset = _target(
            sections,
            0,
            group_array_offset + group_index * 4,
            f"track group {group_index}",
        )
        if group_section != 0:
            raise UnsupportedFormatError(
                "Granny track groups outside section 0 are unsupported"
            )
        vector_track_count = reader.unpack(
            "<I", group_offset + 4, "vector-track count"
        )[0]
        if vector_track_count:
            raise UnsupportedFormatError(
                "Granny vector animation tracks are unsupported"
            )
        track_count = reader.unpack(
            "<I", group_offset + 12, "transform-track count"
        )[0]
        if track_count > _MAX_TRACKS or len(tracks) + track_count > _MAX_TRACKS:
            raise CorruptDataError(
                f"Unsafe Granny transform-track count: {track_count}"
            )
        track_section, track_offset = _target(
            sections,
            0,
            group_offset + 16,
            "transform-track array",
        )
        if track_section != 0:
            raise UnsupportedFormatError(
                "Granny transform tracks outside section 0 are unsupported"
            )
        reader.require(track_offset, track_count * 32, "transform tracks")
        for track_index in range(track_count):
            offset = track_offset + track_index * 32
            name_index, flags = reader.unpack(
                "<II", offset, f"track {track_index} header"
            )
            if name_index >= len(strings):
                raise CorruptDataError(
                    f"Granny track {track_index} name index is out of range"
                )
            track_name = strings[name_index]
            if not track_name or track_name in track_names:
                raise CorruptDataError(
                    f"Granny track name is empty or duplicated: {track_name!r}"
                )
            tracks.append(
                GrannyTransformTrack(
                    name=track_name,
                    flags=flags,
                    rotation=decoder.rotation(offset + 8),
                    translation=decoder.translation(offset + 16),
                    scale_shear=decoder.scale_shear(offset + 24),
                )
            )
            track_names.add(track_name)
    if not tracks:
        raise CorruptDataError("Granny animation contains no transform tracks")
    return GrannyAnimation(
        name=name,
        duration=duration,
        time_step=time_step,
        oversampling=oversampling,
        tracks=tuple(tracks),
        additive_bone_indices=additive_bone_indices,
    )
