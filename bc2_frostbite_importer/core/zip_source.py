# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import CorruptDataError, ExtractionError, UnsupportedSourceError
from .io import safe_cache_name


ProgressCallback = Callable[[int, int, str], None]
CancelCallback = Callable[[], bool]

_COPY_CHUNK_SIZE = 4 << 20
_MAX_ENTRY_COUNT = 100_000
_MAX_SOURCE_SIZE = 16 << 30
_MIN_FALLBACK_SOURCE_SIZE = 1 << 20
_STFS_MAGICS = (b"LIVE", b"PIRS", b"CON ")


@dataclass(frozen=True)
class MaterializedZipMember:
    path: Path
    member_name: str
    size: int


def _leaf_name(member_name: str) -> str:
    return member_name.replace("\\", "/").rsplit("/", 1)[-1]


def _member_summary(entries: list[zipfile.ZipInfo]) -> str:
    names = [entry.filename for entry in entries[:5]]
    if len(entries) > len(names):
        names.append(f"... and {len(entries) - len(names)} more")
    return ", ".join(repr(name) for name in names)


def _read_prefix(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> bytes:
    if entry.flag_bits & 0x1:
        return b""
    try:
        with archive.open(entry, "r") as stream:
            return stream.read(4)
    except NotImplementedError:
        return b""
    except RuntimeError as exc:
        raise CorruptDataError(
            f"Could not inspect ZIP member {entry.filename!r}: {exc}"
        ) from exc


def _select_source_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    entries = archive.infolist()
    if len(entries) > _MAX_ENTRY_COUNT:
        raise UnsupportedSourceError(
            f"ZIP contains too many entries: {len(entries):,}"
        )
    files = [entry for entry in entries if not entry.is_dir()]
    if not files:
        raise UnsupportedSourceError("ZIP contains no files")

    candidates = []
    for entry in files:
        leaf = _leaf_name(entry.filename)
        if leaf.casefold().endswith(".iso"):
            candidates.append(entry)
        elif entry.file_size >= 4 and _read_prefix(archive, entry) in _STFS_MAGICS:
            candidates.append(entry)

    if not candidates:
        substantial = [
            entry
            for entry in files
            if entry.file_size >= _MIN_FALLBACK_SOURCE_SIZE
        ]
        if len(substantial) == 1:
            candidates = substantial
        elif len(files) == 1:
            candidates = files

    if len(candidates) != 1:
        considered = candidates or [
            entry
            for entry in files
            if entry.file_size >= _MIN_FALLBACK_SOURCE_SIZE
        ]
        if not considered:
            considered = files
        raise UnsupportedSourceError(
            "ZIP must contain exactly one identifiable source member; found "
            f"{len(candidates)} candidates among {_member_summary(considered)}"
        )
    return candidates[0]


def _materialize_member(
    archive: zipfile.ZipFile,
    entry: zipfile.ZipInfo,
    cache_directory: Path,
    *,
    progress: ProgressCallback | None,
    cancelled: CancelCallback | None,
) -> MaterializedZipMember:
    if entry.file_size <= 0:
        raise UnsupportedSourceError(
            f"ZIP source member is empty: {entry.filename!r}"
        )
    if entry.file_size > _MAX_SOURCE_SIZE:
        raise UnsupportedSourceError(
            f"ZIP source member exceeds the 16 GiB safety limit: "
            f"{entry.filename!r} ({entry.file_size:,} bytes)"
        )
    if entry.flag_bits & 0x1:
        raise UnsupportedSourceError(
            f"Encrypted ZIP source members are not supported: {entry.filename!r}"
        )

    cache_directory.mkdir(parents=True, exist_ok=True)
    leaf = _leaf_name(entry.filename)
    suffix = ".iso" if leaf.casefold().endswith(".iso") else ".bin"
    cache_key = f"{entry.filename}|{entry.CRC:08x}|{entry.file_size}"
    destination = cache_directory / safe_cache_name(cache_key, suffix)
    if destination.is_file() and destination.stat().st_size == entry.file_size:
        return MaterializedZipMember(destination, entry.filename, entry.file_size)

    available = shutil.disk_usage(cache_directory).free
    if available < entry.file_size:
        raise ExtractionError(
            f"Not enough free cache space for ZIP member {entry.filename!r}: "
            f"need {entry.file_size:,} bytes, have {available:,}"
        )

    temporary_path: Path | None = None
    try:
        try:
            source = archive.open(entry, "r")
        except NotImplementedError as exc:
            raise UnsupportedSourceError(
                f"ZIP compression method {entry.compress_type} is not supported "
                f"for {entry.filename!r}"
            ) from exc
        except RuntimeError as exc:
            raise UnsupportedSourceError(
                f"Could not open ZIP source member {entry.filename!r}: {exc}"
            ) from exc

        with source, tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="bc2-zip-",
            suffix=".tmp",
            dir=cache_directory,
            delete=False,
        ) as target:
            temporary_path = Path(target.name)
            copied = 0
            if progress:
                progress(0, entry.file_size, entry.filename)
            while copied < entry.file_size:
                if cancelled and cancelled():
                    raise ExtractionError("ZIP source preparation was cancelled")
                chunk = source.read(
                    min(_COPY_CHUNK_SIZE, entry.file_size - copied)
                )
                if not chunk:
                    raise CorruptDataError(
                        f"Truncated ZIP member {entry.filename!r}: expected "
                        f"{entry.file_size:,} bytes, received {copied:,}"
                    )
                target.write(chunk)
                copied += len(chunk)
                if progress:
                    progress(copied, entry.file_size, entry.filename)
            if source.read(1):
                raise CorruptDataError(
                    f"ZIP member exceeds its declared size: {entry.filename!r}"
                )
        os.replace(temporary_path, destination)
        temporary_path = None
        return MaterializedZipMember(destination, entry.filename, entry.file_size)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def materialize_zip_source(
    archive_path: str | os.PathLike[str],
    cache_directory: str | os.PathLike[str],
    *,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
) -> MaterializedZipMember:
    path = Path(archive_path)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            entry = _select_source_member(archive)
            return _materialize_member(
                archive,
                entry,
                Path(cache_directory),
                progress=progress,
                cancelled=cancelled,
            )
    except zipfile.BadZipFile as exc:
        raise CorruptDataError(f"Invalid ZIP source {path}: {exc}") from exc
