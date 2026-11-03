# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import io
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from .errors import CorruptDataError, UnsupportedSourceError
from .io import read_exact
from .records import DiscFile


def _uint24_le(data: bytes) -> int:
    return int.from_bytes(data, "little")


def _data_block_offset(data_offset: int, block: int) -> int:
    if block < 0 or block > 0xFFFFFF:
        raise CorruptDataError(f"STFS block number is out of range: {block}")
    physical_block = block + (block + 0xAA) // 0xAA
    if block > 0xAA:
        physical_block += (block + 0x70E4) // 0x70E4
        if block > 0x70E4:
            physical_block += (block + 0x4AF768) // 0x4AF768
    return data_offset + physical_block * StfsPackage.BLOCK_SIZE


class StfsFileReader(io.RawIOBase):
    """Seekable view over consecutive logical STFS data blocks."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        data_offset: int,
        start_block: int,
        length: int,
    ):
        super().__init__()
        if start_block < 0 or length < 0:
            raise ValueError("STFS block start and length must be non-negative")
        block_count = (length + StfsPackage.BLOCK_SIZE - 1) // StfsPackage.BLOCK_SIZE
        if start_block + block_count > 0x1000000:
            raise CorruptDataError("STFS file extent exceeds the block-number range")
        self._file = open(path, "rb")
        self._data_offset = data_offset
        self._start_block = start_block
        self._length = length
        self._position = 0

    @property
    def length(self) -> int:
        return self._length

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        self._checkClosed()
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._checkClosed()
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._position + offset
        elif whence == io.SEEK_END:
            position = self._length + offset
        else:
            raise ValueError(f"Unsupported seek mode: {whence}")
        if position < 0:
            raise ValueError("Cannot seek before the bounded STFS file")
        self._position = min(position, self._length)
        return self._position

    def read(self, size: int = -1) -> bytes:
        self._checkClosed()
        remaining = self._length - self._position
        if size is None or size < 0:
            size = remaining
        else:
            size = min(size, remaining)
        if size <= 0:
            return b""
        result = bytearray()
        while size:
            relative_block, within_block = divmod(
                self._position, StfsPackage.BLOCK_SIZE
            )
            chunk_size = min(size, StfsPackage.BLOCK_SIZE - within_block)
            physical_offset = _data_block_offset(
                self._data_offset,
                self._start_block + relative_block,
            )
            self._file.seek(physical_offset + within_block)
            chunk = self._file.read(chunk_size)
            result.extend(chunk)
            self._position += len(chunk)
            size -= len(chunk)
            if len(chunk) != chunk_size:
                break
        return bytes(result)

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def close(self):
        if not self.closed:
            self._file.close()
        super().close()


@dataclass(frozen=True)
class _StfsEntry:
    slot: int
    name: str
    is_directory: bool
    consecutive: bool
    block_count: int
    start_block: int
    parent: int
    size: int


class StfsPackage:
    BLOCK_SIZE = 0x1000
    HEADER_SIZE_OFFSET = 0x340
    VOLUME_DESCRIPTOR_OFFSET = 0x379
    DESCRIPTOR_TYPE_OFFSET = 0x3A9
    SUPPORTED_MAGICS = (b"LIVE", b"PIRS")
    STFS_MAGICS = (*SUPPORTED_MAGICS, b"CON ")

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(Path(path).resolve())
        self._image_size = Path(self.path).stat().st_size
        metadata_size = self.DESCRIPTOR_TYPE_OFFSET + 4
        with open(self.path, "rb") as stream:
            header = read_exact(stream, metadata_size, "STFS package header")
        self.magic = header[:4]
        if self.magic not in self.STFS_MAGICS:
            raise UnsupportedSourceError("The file is not an STFS content package")
        if self.magic not in self.SUPPORTED_MAGICS:
            raise UnsupportedSourceError(
                "Console-signed STFS packages are not supported"
            )

        self.header_size = struct.unpack_from(
            ">I", header, self.HEADER_SIZE_OFFSET
        )[0]
        if self.header_size < metadata_size or self.header_size > self._image_size:
            raise CorruptDataError(
                f"Invalid STFS header size: {self.header_size:,} bytes"
            )
        self.data_offset = (
            self.header_size + self.BLOCK_SIZE - 1
        ) & -self.BLOCK_SIZE
        if self.data_offset >= self._image_size:
            raise CorruptDataError("STFS package has no block data")

        descriptor_type = struct.unpack_from(
            ">I", header, self.DESCRIPTOR_TYPE_OFFSET
        )[0]
        if descriptor_type != 0:
            raise UnsupportedSourceError(
                f"Xbox secured-volume packages are not supported (type {descriptor_type})"
            )
        descriptor = header[
            self.VOLUME_DESCRIPTOR_OFFSET : self.VOLUME_DESCRIPTOR_OFFSET + 0x24
        ]
        if descriptor[0] != 0x24:
            raise CorruptDataError("Invalid STFS volume descriptor size")
        self.block_separation = descriptor[2]
        self.file_table_block_count = struct.unpack_from("<H", descriptor, 3)[0]
        self.file_table_start_block = _uint24_le(descriptor[5:8])
        self.total_allocated_blocks = struct.unpack_from(">I", descriptor, 0x1C)[0]
        self.total_unallocated_blocks = struct.unpack_from(">I", descriptor, 0x20)[
            0
        ]
        if self.total_allocated_blocks > 0x1000000:
            raise CorruptDataError(
                "STFS allocated block count exceeds the format limit"
            )
        if (
            self.file_table_start_block + self.file_table_block_count
            > self.total_allocated_blocks
        ):
            raise CorruptDataError("STFS file table exceeds the allocated block range")
        if self.total_allocated_blocks:
            final_offset = _data_block_offset(
                self.data_offset, self.total_allocated_blocks - 1
            )
            if final_offset + self.BLOCK_SIZE > self._image_size:
                raise CorruptDataError("STFS allocated blocks exceed the package")
        self._files: dict[str, DiscFile] | None = None

    @classmethod
    def probe(cls, path: str | os.PathLike[str]) -> bool:
        try:
            with open(path, "rb") as stream:
                return stream.read(4) in cls.STFS_MAGICS
        except OSError:
            return False

    def block_offset(self, block: int) -> int:
        return _data_block_offset(self.data_offset, block)

    def open_blocks(
        self,
        start_block: int,
        length: int,
        *,
        consecutive: bool,
    ) -> StfsFileReader:
        required_blocks = (length + self.BLOCK_SIZE - 1) // self.BLOCK_SIZE
        if required_blocks > 1 and not consecutive:
            raise UnsupportedSourceError(
                "Fragmented STFS files are not supported"
            )
        if (
            start_block < 0
            or start_block + required_blocks > self.total_allocated_blocks
        ):
            raise CorruptDataError("STFS file extent exceeds the allocated blocks")
        return StfsFileReader(
            self.path,
            self.data_offset,
            start_block,
            length,
        )

    def _file_table(self) -> bytes:
        length = self.file_table_block_count * self.BLOCK_SIZE
        with self.open_blocks(
            self.file_table_start_block,
            length,
            consecutive=True,
        ) as stream:
            return read_exact(stream, length, "STFS file table")

    def files(self) -> dict[str, DiscFile]:
        if self._files is not None:
            return self._files
        data = self._file_table()
        entries: dict[int, _StfsEntry] = {}
        for slot in range(len(data) // 0x40):
            record = data[slot * 0x40 : (slot + 1) * 0x40]
            if not any(record):
                if any(data[(slot + 1) * 0x40 :]):
                    raise CorruptDataError(
                        "STFS file table contains entries after its terminator"
                    )
                break
            flags = record[0x28]
            name_length = flags & 0x3F
            if name_length == 0 or name_length > 0x28:
                raise CorruptDataError("STFS file name length is invalid")
            try:
                name = record[:name_length].decode("ascii")
            except UnicodeDecodeError as exc:
                raise CorruptDataError("STFS file name is not ASCII") from exc
            if (
                "\0" in name
                or "/" in name
                or "\\" in name
                or name in {".", ".."}
            ):
                raise CorruptDataError(f"Invalid STFS file name: {name!r}")
            block_count = _uint24_le(record[0x29:0x2C])
            block_count_copy = _uint24_le(record[0x2C:0x2F])
            if block_count != block_count_copy:
                raise CorruptDataError(
                    f"STFS block counts disagree for {name}"
                )
            start_block = _uint24_le(record[0x2F:0x32])
            parent = struct.unpack_from(">H", record, 0x32)[0]
            size = struct.unpack_from(">I", record, 0x34)[0]
            is_directory = bool(flags & 0x80)
            if not is_directory:
                required_blocks = (
                    size + self.BLOCK_SIZE - 1
                ) // self.BLOCK_SIZE
                if required_blocks > block_count:
                    raise CorruptDataError(
                        f"STFS allocation is too small for {name}"
                    )
                if start_block + block_count > self.total_allocated_blocks:
                    raise CorruptDataError(
                        f"STFS allocation exceeds the package for {name}"
                    )
            entries[slot] = _StfsEntry(
                slot=slot,
                name=name,
                is_directory=is_directory,
                consecutive=bool(flags & 0x40),
                block_count=block_count,
                start_block=start_block,
                parent=parent,
                size=size,
            )

        resolved_paths: dict[int, str] = {}

        def resolve_path(slot: int, pending: set[int]) -> str:
            cached = resolved_paths.get(slot)
            if cached is not None:
                return cached
            if slot in pending:
                raise CorruptDataError("STFS directory tree contains a cycle")
            entry = entries.get(slot)
            if entry is None:
                raise CorruptDataError(
                    f"STFS path references a missing entry: {slot}"
                )
            if entry.parent == 0xFFFF:
                path = entry.name
            else:
                parent = entries.get(entry.parent)
                if parent is None or not parent.is_directory:
                    raise CorruptDataError(
                        f"STFS entry has an invalid parent: {entry.name}"
                    )
                path = (
                    resolve_path(entry.parent, pending | {slot})
                    + "/"
                    + entry.name
                )
            resolved_paths[slot] = path
            return path

        found: dict[str, DiscFile] = {}
        for slot, entry in entries.items():
            path = resolve_path(slot, set())
            key = path.casefold()
            if key in found:
                raise CorruptDataError(f"Duplicate STFS path: {path}")
            found[key] = DiscFile(
                path=path,
                offset=(
                    self.block_offset(entry.start_block)
                    if not entry.is_directory
                    else 0
                ),
                size=entry.size,
                is_directory=entry.is_directory,
                metadata={
                    "slot": entry.slot,
                    "parent": entry.parent,
                    "start_block": entry.start_block,
                    "block_count": entry.block_count,
                    "consecutive": entry.consecutive,
                },
            )
        self._files = found
        return found

    def find(self, path: str) -> DiscFile | None:
        return self.files().get(path.replace("\\", "/").casefold())

    def open_file(self, entry: DiscFile) -> StfsFileReader:
        if entry.is_directory:
            raise IsADirectoryError(entry.path)
        return self.open_blocks(
            int(entry.metadata["start_block"]),
            entry.size,
            consecutive=bool(entry.metadata["consecutive"]),
        )
