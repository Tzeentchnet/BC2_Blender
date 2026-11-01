# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import hashlib
import io
import os
import re
from pathlib import Path
from typing import BinaryIO

from .errors import CorruptDataError


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def read_exact(stream: BinaryIO, size: int, label: str = "data") -> bytes:
    if size < 0:
        raise CorruptDataError(f"Negative {label} size: {size}")
    data = stream.read(size)
    if len(data) != size:
        raise CorruptDataError(
            f"Truncated {label}: expected {size:,} bytes, received {len(data):,}"
        )
    return data


def copy_exact(source: BinaryIO, target: BinaryIO, size: int, chunk_size: int = 4 << 20):
    remaining = size
    while remaining:
        chunk = source.read(min(chunk_size, remaining))
        if not chunk:
            raise CorruptDataError(
                f"Truncated source while copying {size:,} bytes ({remaining:,} missing)"
            )
        target.write(chunk)
        remaining -= len(chunk)


def safe_cache_name(value: str, suffix: str = "") -> str:
    stem = _UNSAFE_NAME.sub("_", value).strip("._")[:96] or "asset"
    digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"{stem}-{digest}{suffix}"


def source_fingerprint(path: str | os.PathLike[str]) -> str:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    raw = f"{resolved}|{stat.st_size}|{stat.st_mtime_ns}".encode(
        "utf-8", "surrogatepass"
    )
    return hashlib.sha256(raw).hexdigest()


class RangeReader(io.RawIOBase):
    """Seekable, bounded view over a byte range in a file."""

    def __init__(self, path: str | os.PathLike[str], start: int, length: int):
        super().__init__()
        if start < 0 or length < 0:
            raise ValueError("Range start and length must be non-negative")
        self._file = open(path, "rb")
        self._start = start
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
            raise ValueError("Cannot seek before the bounded range")
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
        self._file.seek(self._start + self._position)
        data = self._file.read(size)
        self._position += len(data)
        return data

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def close(self):
        if not self.closed:
            self._file.close()
        super().close()
