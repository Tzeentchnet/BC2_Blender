# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
import math
import struct
from dataclasses import dataclass
from xml.sax.saxutils import escape, quoteattr

from .errors import CorruptDataError
from .io import read_exact


HASH_FIELDS = {
    "CompositeMeshPartNames",
    "Hash",
    "HashedName",
    "HashedWaveName",
    "Id",
    "OnRoadMaterialNameHashes",
    "OriginalHashedWaveName",
}


@dataclass(frozen=True)
class DbxElement:
    tag: str
    attributes: tuple[tuple[str, str], ...] = ()
    value: str | None = None
    children: tuple["DbxElement", ...] = ()
    container: bool = False

    def attribute(self, name: str) -> str | None:
        return next(
            (value for key, value in self.attributes if key == name),
            None,
        )

    def child(self, tag: str, name: str | None = None) -> "DbxElement | None":
        return next(
            (
                element
                for element in self.children
                if element.tag == tag
                and (name is None or element.attribute("name") == name)
            ),
            None,
        )

    def children_named(
        self, tag: str, name: str | None = None
    ) -> tuple["DbxElement", ...]:
        return tuple(
            element
            for element in self.children
            if element.tag == tag
            and (name is None or element.attribute("name") == name)
        )


class DbxDocument:
    def __init__(self, root: DbxElement):
        if root.tag != "partition":
            raise CorruptDataError(
                f"DBX root is {root.tag!r}, expected 'partition'"
            )
        self.root = root
        self.instances = root.children_named("instance")
        self._instances_by_guid = {
            guid.casefold(): instance
            for instance in self.instances
            if (guid := instance.attribute("guid"))
        }

    @property
    def guid(self) -> str | None:
        return self.root.attribute("guid")

    @property
    def primary_instance(self) -> DbxElement | None:
        guid = self.root.attribute("primaryInstance")
        return self.instance(guid) if guid else None

    def instance(self, guid: str | None) -> DbxElement | None:
        return self._instances_by_guid.get(guid.casefold()) if guid else None


@dataclass
class _MutableElement:
    tag: str
    attributes: tuple[tuple[str, str], ...]
    value: str | None
    children: list["_MutableElement"]
    container: bool = False


def _read_leb128(stream: io.BytesIO) -> int:
    result = 0
    shift = 0
    for _ in range(10):
        raw = stream.read(1)
        if not raw:
            raise EOFError
        value = raw[0]
        result |= (value & 0x7F) << shift
        if not value & 0x80:
            return result
        shift += 7
    raise CorruptDataError("DBX LEB128 value is too long")


def _number32(raw: bytes, field_name: str) -> str:
    integer = struct.unpack(">i", raw)[0]
    if field_name in HASH_FIELDS or integer >> 24 == 0 or integer >> 23 in (255, -1):
        return str(integer)
    value = struct.unpack(">f", raw)[0]
    if not math.isfinite(value):
        return str(integer)
    return repr(value)


def _freeze(element: _MutableElement) -> DbxElement:
    return DbxElement(
        tag=element.tag,
        attributes=element.attributes,
        value=element.value,
        children=tuple(_freeze(child) for child in element.children),
        container=element.container,
    )


def parse_dbx(data: bytes) -> DbxDocument:
    if not data.startswith(b"{binary}"):
        raise CorruptDataError("DBX resource does not start with {binary}")
    stream = io.BytesIO(data[8:])
    header = read_exact(stream, 16, "DBX header")
    _, _, relocation_offset, string_count = struct.unpack(">IIII", header)
    if string_count > 1_000_000:
        raise CorruptDataError(f"DBX string count is unsafe: {string_count:,}")
    offsets = (
        struct.unpack(
            ">" + "I" * string_count,
            read_exact(stream, string_count * 4, "DBX string offsets"),
        )
        if string_count
        else ()
    )
    table_size = relocation_offset - string_count * 4
    if table_size < 0:
        raise CorruptDataError("DBX string table size is negative")
    table = read_exact(stream, table_size, "DBX string table")
    strings = []
    for index, offset in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < len(offsets) else len(table)
        if offset > end or end > len(table):
            raise CorruptDataError("DBX string offsets are invalid")
        raw = table[offset:end]
        if raw.endswith(b"\0"):
            raw = raw[:-1]
        strings.append(raw.decode("utf-8", "replace"))

    def string(index: int) -> str:
        if index < 0 or index >= len(strings):
            raise CorruptDataError(f"DBX string index is out of range: {index}")
        return strings[index]

    roots: list[_MutableElement] = []
    open_elements: list[_MutableElement] = []

    def append(element: _MutableElement):
        if open_elements:
            open_elements[-1].children.append(element)
        else:
            roots.append(element)

    while stream.tell() < len(stream.getbuffer()):
        try:
            prefix_number = _read_leb128(stream)
        except EOFError:
            break
        if prefix_number == 0:
            if not open_elements:
                raise CorruptDataError("DBX closes an element that was never opened")
            open_elements.pop()
            continue
        prefix = string(prefix_number)
        type_and_count = read_exact(stream, 1, "DBX element type")[0]
        value_type = type_and_count >> 4
        attribute_count = type_and_count & 0x0F
        attributes = []
        for _ in range(attribute_count):
            name = string(_read_leb128(stream))
            value = string(_read_leb128(stream))
            attributes.append((name, value))
        attributes_tuple = tuple(attributes)
        if value_type == 0xA:
            read_exact(stream, 1, "DBX container marker")
            element = _MutableElement(
                prefix,
                attributes_tuple,
                None,
                [],
                container=True,
            )
            append(element)
            open_elements.append(element)
        elif value_type == 0x2:
            content = string(_read_leb128(stream))
            append(
                _MutableElement(
                    prefix,
                    attributes_tuple,
                    content if content else None,
                    [],
                )
            )
        elif value_type == 0x7:
            number_count = _read_leb128(stream)
            number_size = _read_leb128(stream)
            if number_count > 10_000_000 or number_size not in (2, 4, 8):
                raise CorruptDataError(
                    f"Unsupported DBX numeric array: {number_count} x {number_size}"
                )
            field_name = attributes[0][1] if attributes else ""
            values = []
            if number_size == 4:
                values = [
                    _number32(read_exact(stream, 4, "DBX number"), field_name)
                    for _ in range(number_count)
                ]
            elif number_size == 8:
                values = [
                    repr(struct.unpack(">d", read_exact(stream, 8, "DBX number"))[0])
                    for _ in range(number_count)
                ]
            else:
                values = [
                    str(struct.unpack(">H", read_exact(stream, 2, "DBX number"))[0])
                    for _ in range(number_count)
                ]
            append(
                _MutableElement(
                    prefix,
                    attributes_tuple,
                    "/".join(values),
                    [],
                )
            )
        else:
            read_exact(stream, 1, "DBX scalar marker")
            value = read_exact(stream, 1, "DBX scalar value")[0]
            content = "true" if value == 1 else "false" if value == 0 else str(value)
            append(
                _MutableElement(prefix, attributes_tuple, content, [])
            )
    if open_elements:
        raise CorruptDataError(
            f"DBX ended with {len(open_elements)} unclosed element(s)"
        )
    if len(roots) != 1:
        raise CorruptDataError(
            f"DBX contains {len(roots)} root elements, expected exactly one"
        )
    return DbxDocument(_freeze(roots[0]))


def _element_xml(element: DbxElement, depth: int, lines: list[str]):
    indent = "  " * depth
    attribute_text = "".join(
        f" {name}={quoteattr(value)}" for name, value in element.attributes
    )
    if element.children or element.container:
        lines.append(f"{indent}<{element.tag}{attribute_text}>")
        for child in element.children:
            _element_xml(child, depth + 1, lines)
        lines.append(f"{indent}</{element.tag}>")
    elif element.value is None:
        lines.append(f"{indent}<{element.tag}{attribute_text} />")
    else:
        lines.append(
            f"{indent}<{element.tag}{attribute_text}>"
            f"{escape(element.value)}</{element.tag}>"
        )


def dbx_to_xml(data: bytes) -> str:
    document = parse_dbx(data)
    lines = ['<?xml version="1.0" encoding="utf-8"?>']
    _element_xml(document.root, 0, lines)
    return "\n".join(lines) + "\n"
