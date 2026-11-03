# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from .errors import ExtractionError, PlatformRequirementError, UnsupportedSourceError
from .io import RangeReader, safe_cache_name
from .iso9660 import Iso9660Image
from .msi import read_installer_files
from .records import ArchiveDescriptor
from .stfs import StfsPackage
from .xdvdfs import XdvdfsImage
from .zip_source import materialize_zip_source


class DiscSource:
    kind = "UNKNOWN"
    platform = "unknown"
    index_archives_during_scan = False

    def __init__(self, image_path: str, cache_directory: str | os.PathLike[str]):
        self.image_path = str(Path(image_path).resolve())
        self.cache_directory = Path(cache_directory)
        self.cache_directory.mkdir(parents=True, exist_ok=True)

    def list_archives(self) -> list[ArchiveDescriptor]:
        raise NotImplementedError

    @contextmanager
    def open_archive(self, locator: dict) -> Iterator[BinaryIO]:
        raise NotImplementedError


class Xbox360Source(DiscSource):
    kind = "XBOX360_ISO"
    platform = "xbox360"
    index_archives_during_scan = True

    def __init__(self, image_path: str, cache_directory: str | os.PathLike[str]):
        super().__init__(image_path, cache_directory)
        self.image = XdvdfsImage(self.image_path)

    def list_archives(self) -> list[ArchiveDescriptor]:
        archives = []
        for entry in self.image.files().values():
            if entry.is_directory or not entry.path.casefold().endswith(".fbrb"):
                continue
            archives.append(
                ArchiveDescriptor(
                    path=entry.path,
                    size=entry.size,
                    platform=self.platform,
                    locator={"offset": entry.offset, "size": entry.size},
                )
            )
        return sorted(archives, key=lambda archive: archive.path.casefold())

    @contextmanager
    def open_archive(self, locator: dict) -> Iterator[BinaryIO]:
        reader = RangeReader(
            self.image_path, int(locator["offset"]), int(locator["size"])
        )
        try:
            yield reader
        finally:
            reader.close()


class Xbox360StfsSource(DiscSource):
    kind = "XBOX360_STFS"
    platform = "xbox360"
    index_archives_during_scan = True

    def __init__(self, image_path: str, cache_directory: str | os.PathLike[str]):
        super().__init__(image_path, cache_directory)
        self.package = StfsPackage(self.image_path)

    def list_archives(self) -> list[ArchiveDescriptor]:
        archives = []
        for entry in self.package.files().values():
            if entry.is_directory or not entry.path.casefold().endswith(".fbrb"):
                continue
            archives.append(
                ArchiveDescriptor(
                    path=entry.path,
                    size=entry.size,
                    platform=self.platform,
                    locator={
                        "start_block": entry.metadata["start_block"],
                        "size": entry.size,
                        "consecutive": entry.metadata["consecutive"],
                    },
                )
            )
        return sorted(archives, key=lambda archive: archive.path.casefold())

    @contextmanager
    def open_archive(self, locator: dict) -> Iterator[BinaryIO]:
        reader = self.package.open_blocks(
            int(locator["start_block"]),
            int(locator["size"]),
            consecutive=bool(locator["consecutive"]),
        )
        try:
            yield reader
        finally:
            reader.close()


class PcIsoSource(DiscSource):
    kind = "PC_ISO"
    platform = "pc"

    def __init__(self, image_path: str, cache_directory: str | os.PathLike[str]):
        if os.name != "nt":
            raise PlatformRequirementError(
                "The PC retail ISO uses Windows Installer and LZX cabinets; "
                "PC ISO extraction currently requires Windows"
            )
        super().__init__(image_path, cache_directory)
        self.image = Iso9660Image(self.image_path)
        self._cabinet_entries = {
            entry.path.casefold(): entry
            for entry in self.image.files().values()
            if not entry.is_directory and entry.path.casefold().endswith(".cab")
        }

    def _installer_database(self) -> Path:
        entry = self.image.find("AUTORUN.DAT")
        if not entry:
            raise UnsupportedSourceError("PC ISO is missing AUTORUN.DAT")
        destination = self.cache_directory / "AUTORUN.DAT"
        if not destination.exists() or destination.stat().st_size != entry.size:
            self.image.extract(entry, destination)
        return destination

    def list_archives(self) -> list[ArchiveDescriptor]:
        files = read_installer_files(str(self._installer_database()))
        archives: list[ArchiveDescriptor] = []
        for item in files:
            if not item.path.casefold().endswith(".fbrb") or not item.cabinet:
                continue
            cabinet_entry = self._cabinet_entries.get(item.cabinet.casefold())
            if not cabinet_entry:
                continue
            archives.append(
                ArchiveDescriptor(
                    path=item.path,
                    size=item.size,
                    platform=self.platform,
                    locator={
                        "cabinet": cabinet_entry.path,
                        "cabinet_size": cabinet_entry.size,
                        "cab_entry": item.file_key,
                        "archive_size": item.size,
                    },
                )
            )
        unique: dict[str, ArchiveDescriptor] = {}
        for archive in archives:
            unique[archive.path.casefold()] = archive
        return sorted(unique.values(), key=lambda archive: archive.path.casefold())

    def _materialize_cabinet(self, name: str, expected_size: int) -> Path:
        entry = self._cabinet_entries.get(name.casefold())
        if not entry:
            raise ExtractionError(f"Cabinet is not present in the ISO: {name}")
        directory = self.cache_directory / "cabinets"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / Path(entry.path).name
        if destination.exists() and destination.stat().st_size == expected_size:
            return destination
        self.image.extract(entry, destination)
        return destination

    def _materialize_archive(self, locator: dict) -> Path:
        archive_key = json.dumps(locator, sort_keys=True, separators=(",", ":"))
        destination = self.cache_directory / "archives" / safe_cache_name(
            archive_key, ".fbrb"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected_size = int(locator["archive_size"])
        if destination.exists() and destination.stat().st_size == expected_size:
            return destination
        cabinet = self._materialize_cabinet(
            str(locator["cabinet"]), int(locator["cabinet_size"])
        )
        expand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "expand.exe"
        if not expand.exists():
            raise PlatformRequirementError(f"Windows cabinet extractor not found: {expand}")
        with tempfile.TemporaryDirectory(
            prefix="bc2-cab-", dir=self.cache_directory
        ) as temporary:
            temporary_directory = Path(temporary)
            command = [
                str(expand),
                str(cabinet),
                f"-F:{locator['cab_entry']}",
                str(temporary_directory),
            ]
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                details = (completed.stderr or completed.stdout).strip()
                raise ExtractionError(
                    f"expand.exe could not extract {locator['cab_entry']}: {details}"
                )
            extracted = temporary_directory / str(locator["cab_entry"])
            if not extracted.exists():
                raise ExtractionError(
                    f"Cabinet extraction succeeded but produced no {locator['cab_entry']}"
                )
            if extracted.stat().st_size != expected_size:
                raise ExtractionError(
                    f"Extracted FbRB size mismatch: expected {expected_size:,}, "
                    f"found {extracted.stat().st_size:,}"
                )
            os.replace(extracted, destination)
        return destination

    @contextmanager
    def open_archive(self, locator: dict) -> Iterator[BinaryIO]:
        path = self._materialize_archive(locator)
        with open(path, "rb") as stream:
            yield stream


def _detect_source_file(
    path: Path, cache_directory: str | os.PathLike[str]
) -> DiscSource:
    if StfsPackage.probe(path):
        return Xbox360StfsSource(str(path), cache_directory)
    descriptor = XdvdfsImage.find_descriptor(path, scan=False)
    if descriptor is not None:
        return Xbox360Source(str(path), cache_directory)
    if Iso9660Image.probe(path):
        image = Iso9660Image(path)
        files = image.files()
        has_installer = "autorun.dat" in files
        has_cabinets = any(name.endswith(".cab") for name in files)
        if has_installer and has_cabinets:
            return PcIsoSource(str(path), cache_directory)
    descriptor = XdvdfsImage.find_descriptor(path, scan=True)
    if descriptor is not None:
        return Xbox360Source(str(path), cache_directory)
    raise UnsupportedSourceError(
        "The selected file is neither a supported PC ISO, Xbox 360 XDVDFS image, "
        "nor Xbox LIVE STFS package"
    )


def detect_source(
    image_path: str,
    cache_directory: str | os.PathLike[str],
    *,
    progress: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> DiscSource:
    path = Path(image_path)
    if not path.is_file():
        raise UnsupportedSourceError(f"Source file does not exist: {path}")
    if zipfile.is_zipfile(path):
        materialized = materialize_zip_source(
            path,
            Path(cache_directory) / "sources",
            progress=progress,
            cancelled=cancelled,
        )
        try:
            return _detect_source_file(materialized.path, cache_directory)
        except UnsupportedSourceError as exc:
            raise UnsupportedSourceError(
                f"ZIP member {materialized.member_name!r}: {exc}"
            ) from exc
    if path.suffix.casefold() == ".zip":
        raise UnsupportedSourceError(
            f"The selected .zip file is not a valid ZIP archive: {path}"
        )
    return _detect_source_file(path, cache_directory)
