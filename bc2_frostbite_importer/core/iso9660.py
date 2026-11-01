# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import struct
from pathlib import Path

from .errors import CorruptDataError, UnsupportedSourceError
from .io import RangeReader, copy_exact, read_exact
from .records import DiscFile


class Iso9660Image:
    SECTOR_SIZE = 2048
    DESCRIPTOR_START = 16

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(Path(path).resolve())
        self._size = Path(self.path).stat().st_size
        self._files: dict[str, DiscFile] | None = None
        self._root_extent, self._root_size = self._read_primary_volume()

    @classmethod
    def probe(cls, path: str | os.PathLike[str]) -> bool:
        try:
            with open(path, "rb") as stream:
                stream.seek(cls.DESCRIPTOR_START * cls.SECTOR_SIZE)
                descriptor = stream.read(7)
            return len(descriptor) == 7 and descriptor[1:6] == b"CD001"
        except OSError:
            return False

    def _read_primary_volume(self) -> tuple[int, int]:
        with open(self.path, "rb") as stream:
            for sector in range(self.DESCRIPTOR_START, self.DESCRIPTOR_START + 128):
                stream.seek(sector * self.SECTOR_SIZE)
                descriptor = read_exact(stream, self.SECTOR_SIZE, "ISO volume descriptor")
                if descriptor[1:6] != b"CD001":
                    raise UnsupportedSourceError("The image is not ISO9660")
                descriptor_type = descriptor[0]
                if descriptor_type == 1:
                    record = descriptor[156:]
                    return self._parse_record(record).metadata["extent"], self._parse_record(
                        record
                    ).size
                if descriptor_type == 255:
                    break
        raise CorruptDataError("ISO9660 primary volume descriptor is missing")

    def _parse_record(self, record: bytes) -> DiscFile:
        if not record or record[0] < 34 or len(record) < record[0]:
            raise CorruptDataError("Invalid ISO9660 directory record")
        length = record[0]
        extent_le = struct.unpack_from("<I", record, 2)[0]
        extent_be = struct.unpack_from(">I", record, 6)[0]
        size_le = struct.unpack_from("<I", record, 10)[0]
        size_be = struct.unpack_from(">I", record, 14)[0]
        if extent_le != extent_be or size_le != size_be:
            raise CorruptDataError("ISO9660 both-endian fields disagree")
        name_length = record[32]
        if 33 + name_length > length:
            raise CorruptDataError("ISO9660 file identifier exceeds its record")
        raw_name = record[33 : 33 + name_length]
        name = raw_name.decode("ascii", "replace")
        if ";" in name:
            name = name.split(";", 1)[0]
        return DiscFile(
            path=name,
            offset=extent_le * self.SECTOR_SIZE,
            size=size_le,
            is_directory=bool(record[25] & 0x02),
            metadata={"extent": extent_le},
        )

    def _read_directory(self, extent: int, size: int) -> list[DiscFile]:
        if size < 0 or extent * self.SECTOR_SIZE + size > self._size:
            raise CorruptDataError("ISO9660 directory points outside the image")
        with open(self.path, "rb") as stream:
            stream.seek(extent * self.SECTOR_SIZE)
            data = read_exact(stream, size, "ISO9660 directory")
        entries: list[DiscFile] = []
        position = 0
        while position < len(data):
            record_length = data[position]
            if record_length == 0:
                position = ((position // self.SECTOR_SIZE) + 1) * self.SECTOR_SIZE
                continue
            if position + record_length > len(data):
                raise CorruptDataError("Truncated ISO9660 directory record")
            entry = self._parse_record(data[position : position + record_length])
            if entry.path not in ("\x00", "\x01"):
                entries.append(entry)
            position += record_length
        return entries

    def files(self) -> dict[str, DiscFile]:
        if self._files is not None:
            return self._files
        found: dict[str, DiscFile] = {}
        pending = [("", self._root_extent, self._root_size)]
        visited: set[tuple[int, int]] = set()
        while pending:
            prefix, extent, size = pending.pop()
            key = (extent, size)
            if key in visited:
                continue
            visited.add(key)
            for entry in self._read_directory(extent, size):
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
                        (normalized, entry.metadata["extent"], entry.size)
                    )
        self._files = found
        return found

    def find(self, path: str) -> DiscFile | None:
        return self.files().get(path.replace("\\", "/").casefold())

    def open_file(self, entry: DiscFile) -> RangeReader:
        if entry.is_directory:
            raise IsADirectoryError(entry.path)
        return RangeReader(self.path, entry.offset, entry.size)

    def extract(self, entry: DiscFile, destination: str | os.PathLike[str]):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".partial")
        with self.open_file(entry) as source, open(temporary, "wb") as target:
            copy_exact(source, target, entry.size)
        os.replace(temporary, destination)
