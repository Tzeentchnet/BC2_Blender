# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import struct
from pathlib import Path

from .errors import CorruptDataError, UnsupportedSourceError
from .io import RangeReader, read_exact
from .records import DiscFile


class XdvdfsImage:
    SECTOR_SIZE = 2048
    DESCRIPTOR_DELTA = 0x10000
    MAGIC = b"MICROSOFT*XBOX*MEDIA"
    KNOWN_DESCRIPTOR_OFFSETS = (
        0x10000,
        0xFDA0000,
        0x2090000,
    )

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(Path(path).resolve())
        self._image_size = Path(self.path).stat().st_size
        self.descriptor_offset = self.find_descriptor(self.path)
        if self.descriptor_offset is None:
            raise UnsupportedSourceError("No XDVDFS game partition was found")
        self.partition_offset = self.descriptor_offset - self.DESCRIPTOR_DELTA
        self._files: dict[str, DiscFile] | None = None
        with open(self.path, "rb") as stream:
            stream.seek(self.descriptor_offset)
            descriptor = read_exact(stream, self.SECTOR_SIZE, "XDVDFS descriptor")
        if descriptor[:20] != self.MAGIC:
            raise CorruptDataError("Invalid XDVDFS volume signature")
        self.root_sector, self.root_size = struct.unpack_from("<II", descriptor, 20)
        if descriptor[-20:] != self.MAGIC:
            raise CorruptDataError("XDVDFS descriptor trailer is invalid")

    @classmethod
    def probe(cls, path: str | os.PathLike[str]) -> bool:
        return cls.find_descriptor(path, scan=False) is not None

    @classmethod
    def find_descriptor(
        cls, path: str | os.PathLike[str], scan: bool = True
    ) -> int | None:
        try:
            image_size = Path(path).stat().st_size
            with open(path, "rb") as stream:
                for offset in cls.KNOWN_DESCRIPTOR_OFFSETS:
                    if offset + len(cls.MAGIC) > image_size:
                        continue
                    stream.seek(offset)
                    if stream.read(len(cls.MAGIC)) == cls.MAGIC:
                        return offset
                if not scan:
                    return None
                limit = min(image_size, 512 << 20)
                chunk_size = 8 << 20
                overlap = len(cls.MAGIC) - 1
                position = 0
                tail = b""
                while position < limit:
                    stream.seek(position)
                    chunk = stream.read(min(chunk_size, limit - position))
                    if not chunk:
                        break
                    combined = tail + chunk
                    index = combined.find(cls.MAGIC)
                    if index >= 0:
                        return position - len(tail) + index
                    tail = combined[-overlap:]
                    position += len(chunk)
        except OSError:
            return None
        return None

    def _read_directory_tree(self, sector: int, size: int) -> list[DiscFile]:
        absolute = self.partition_offset + sector * self.SECTOR_SIZE
        if (
            absolute < self.partition_offset
            or size < 0
            or absolute + size > self._image_size
        ):
            raise CorruptDataError("XDVDFS directory points outside the image")
        with open(self.path, "rb") as stream:
            stream.seek(absolute)
            data = read_exact(stream, size, "XDVDFS directory")
        entries: list[DiscFile] = []
        visited: set[int] = set()

        def visit(offset: int):
            if offset in visited:
                raise CorruptDataError("XDVDFS directory tree contains a cycle")
            if offset < 0 or offset + 14 > len(data):
                raise CorruptDataError("XDVDFS directory node is out of bounds")
            visited.add(offset)
            left, right, start_sector, file_size = struct.unpack_from(
                "<HHII", data, offset
            )
            attributes = data[offset + 12]
            name_length = data[offset + 13]
            name_end = offset + 14 + name_length
            if name_length == 0 or name_end > len(data):
                raise CorruptDataError("XDVDFS directory name is invalid")
            name = data[offset + 14 : name_end].decode("latin-1")
            if left:
                visit(left * 4)
            file_offset = self.partition_offset + start_sector * self.SECTOR_SIZE
            if file_offset + file_size > self._image_size:
                raise CorruptDataError(f"XDVDFS entry points outside the image: {name}")
            entries.append(
                DiscFile(
                    path=name,
                    offset=file_offset,
                    size=file_size,
                    is_directory=bool(attributes & 0x10),
                    metadata={
                        "sector": start_sector,
                        "attributes": attributes,
                    },
                )
            )
            if right:
                visit(right * 4)

        if size:
            visit(0)
        return entries

    def files(self) -> dict[str, DiscFile]:
        if self._files is not None:
            return self._files
        found: dict[str, DiscFile] = {}
        pending = [("", self.root_sector, self.root_size)]
        visited: set[tuple[int, int]] = set()
        while pending:
            prefix, sector, size = pending.pop()
            key = (sector, size)
            if key in visited:
                continue
            visited.add(key)
            for entry in self._read_directory_tree(sector, size):
                path = f"{prefix}/{entry.path}" if prefix else entry.path
                normalized = path.replace("\\", "/")
                resolved = DiscFile(
                    path=normalized,
                    offset=entry.offset,
                    size=entry.size,
                    is_directory=entry.is_directory,
                    metadata=entry.metadata,
                )
                found[normalized.casefold()] = resolved
                if entry.is_directory:
                    pending.append(
                        (normalized, entry.metadata["sector"], entry.size)
                    )
        self._files = found
        return found

    def find(self, path: str) -> DiscFile | None:
        return self.files().get(path.replace("\\", "/").casefold())

    def open_file(self, entry: DiscFile) -> RangeReader:
        if entry.is_directory:
            raise IsADirectoryError(entry.path)
        return RangeReader(self.path, entry.offset, entry.size)
