# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Callable

from .catalog import AssetCatalog
from .errors import BC2Error, CorruptDataError, ExtractionError
from .fbrb import (
    FbrbEntry,
    copy_entry,
    extension_for,
    find_entry,
    read_entry_bytes,
    read_fbrb,
)
from .io import copy_exact, read_exact, safe_cache_name, source_fingerprint
from .source import DiscSource, detect_source


ProgressCallback = Callable[[int, int, str], None]
CancelCallback = Callable[[], bool]


class AssetRepository:
    def __init__(self, image_path: str, cache_root: str | os.PathLike[str]):
        self.image_path = str(Path(image_path).resolve())
        self.cache_root = Path(cache_root).resolve()
        self.fingerprint = source_fingerprint(self.image_path)
        self.source_cache = self.cache_root / self.fingerprint[:20]
        self.source_cache.mkdir(parents=True, exist_ok=True)
        self.source: DiscSource = detect_source(self.image_path, self.source_cache)
        self.catalog = AssetCatalog(self.source_cache / "catalog.sqlite3")
        self._archive_info_cache = {}
        self._payload_path_cache = {}

    @property
    def kind(self) -> str:
        return self.source.kind

    def close(self):
        self.catalog.close()

    def needs_scan(self) -> bool:
        return (
            self.catalog.meta("source_fingerprint") != self.fingerprint
            or self.catalog.meta("source_kind") != self.kind
        )

    def scan(
        self,
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
    ):
        archives = self.source.list_archives()
        self._archive_info_cache.clear()
        self._payload_path_cache.clear()
        self.catalog.reset(self.image_path, self.kind, self.fingerprint)
        archive_ids: list[int] = []
        for archive in archives:
            archive_ids.append(self.catalog.upsert_archive(archive))
        if self.kind != "XBOX360_ISO":
            if progress:
                progress(len(archives), len(archives), "PC archive catalog ready")
            return []
        total = len(archive_ids)
        failures: list[tuple[str, str]] = []
        for position, archive_id in enumerate(archive_ids, 1):
            if cancelled and cancelled():
                break
            row = self.catalog.archive(archive_id)
            try:
                self.index_archive(archive_id)
            except (BC2Error, OSError) as exc:
                self.catalog.set_archive_error(archive_id, str(exc))
                failures.append((row["path"], str(exc)))
            if progress:
                progress(position, total, row["path"])
        return failures

    def index_archive(self, archive_id: int) -> int:
        archive = self.catalog.archive(archive_id)
        if not archive:
            raise ExtractionError(f"Unknown archive id: {archive_id}")
        locator = json.loads(archive["locator"])
        with self.source.open_archive(locator) as stream:
            info = read_fbrb(stream)
        self._archive_info_cache[archive_id] = info
        self.catalog.replace_archive_assets(
            archive_id,
            info.entries,
            info.payload_offset,
            info.payload_compressed,
            info.payload_size,
        )
        return sum(not entry.deleted for entry in info.entries)

    def index_all_archives(
        self,
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
    ) -> list[tuple[str, str]]:
        archives = self.catalog.unindexed_archives()
        total = len(archives)
        failures: list[tuple[str, str]] = []
        for position, archive in enumerate(archives, 1):
            if cancelled and cancelled():
                break
            try:
                self.index_archive(int(archive["id"]))
            except (BC2Error, OSError) as exc:
                message = str(exc)
                self.catalog.set_archive_error(int(archive["id"]), message)
                failures.append((str(archive["path"]), message))
            if progress:
                progress(position, total, str(archive["path"]))
        return failures

    def index_archive_paths(
        self,
        paths: tuple[str, ...],
        progress: ProgressCallback | None = None,
        cancelled: CancelCallback | None = None,
    ) -> list[tuple[str, str]]:
        failures: list[tuple[str, str]] = []
        total = len(paths)
        for position, path in enumerate(paths, 1):
            if cancelled and cancelled():
                break
            archive = self.catalog.archive_by_path(path)
            try:
                self.index_archive_path(path)
            except (BC2Error, OSError) as exc:
                message = str(exc)
                if archive is not None:
                    self.catalog.set_archive_error(int(archive["id"]), message)
                failures.append((path, message))
            if progress:
                progress(position, total, path)
        return failures

    def _archive_info(self, archive_id: int, stream):
        info = self._archive_info_cache.get(archive_id)
        if info is None:
            info = read_fbrb(stream)
            self._archive_info_cache[archive_id] = info
        return info

    def index_archive_path(self, path: str) -> int:
        archive = self.catalog.archive_by_path(path)
        if not archive:
            raise ExtractionError(f"Frostbite archive is not on this disc: {path}")
        if archive["indexed"]:
            return int(archive["asset_count"])
        return self.index_archive(int(archive["id"]))

    def _cached_payload_path(
        self,
        archive_id: int,
        archive_path: str,
        info,
    ):
        cached = self._payload_path_cache.get(archive_id)
        if (
            cached is not None
            and cached.exists()
            and cached.stat().st_size == info.payload_size
        ):
            return cached
        destination = (
            self.source_cache
            / "payloads"
            / safe_cache_name(archive_path, ".payload")
        )
        if (
            destination.exists()
            and destination.stat().st_size == info.payload_size
        ):
            self._payload_path_cache[archive_id] = destination
            return destination
        return None

    def _materialize_payload(
        self,
        archive_id: int,
        archive_path: str,
        info,
        stream,
    ) -> Path:
        cached = self._cached_payload_path(
            archive_id,
            archive_path,
            info,
        )
        if cached is not None:
            return cached
        destination = (
            self.source_cache
            / "payloads"
            / safe_cache_name(archive_path, ".payload")
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            destination.name + f".{os.getpid()}.partial"
        )
        stream.seek(info.payload_offset)
        try:
            with (
                gzip.GzipFile(fileobj=stream, mode="rb") as payload,
                open(temporary, "wb") as target,
            ):
                copy_exact(payload, target, info.payload_size)
            os.replace(temporary, destination)
        except (OSError, EOFError) as exc:
            raise CorruptDataError(
                f"Invalid FbRB payload gzip stream: {exc}"
            ) from exc
        finally:
            if temporary.exists():
                temporary.unlink()
        self._payload_path_cache[archive_id] = destination
        return destination

    def materialize_archive_payload(self, path: str) -> Path | None:
        archive = self.catalog.archive_by_path(path)
        if not archive:
            raise ExtractionError(f"Frostbite archive is not on this disc: {path}")
        if not archive["indexed"]:
            self.index_archive(int(archive["id"]))
            archive = self.catalog.archive(int(archive["id"]))
        locator = json.loads(archive["locator"])
        with self.source.open_archive(locator) as stream:
            info = self._archive_info(int(archive["id"]), stream)
            if not info.payload_compressed:
                return None
            return self._materialize_payload(
                int(archive["id"]),
                str(archive["path"]),
                info,
                stream,
            )

    def _read_entry(
        self,
        archive_id: int,
        archive_path: str,
        stream,
        info,
        entry: FbrbEntry,
        max_size: int | None,
    ) -> bytes:
        payload_path = self._cached_payload_path(
            archive_id,
            archive_path,
            info,
        )
        if not info.payload_compressed or payload_path is None:
            return read_entry_bytes(
                stream,
                info,
                entry,
                max_size=max_size,
            )
        if max_size is not None and entry.size > max_size:
            raise CorruptDataError(
                "Asset is too large to load in memory "
                f"({entry.size:,} > {max_size:,})"
            )
        with open(payload_path, "rb") as payload:
            payload.seek(entry.offset)
            return read_exact(payload, entry.size, entry.path)

    def _asset_and_entry(self, asset_id: int, resolve_mesh: bool = False):
        if resolve_mesh:
            resolved = self.catalog.resolve_mesh_data(asset_id)
            if resolved is not None:
                asset_id = resolved
        asset = self.catalog.asset(asset_id)
        if not asset:
            raise ExtractionError(f"Unknown asset id: {asset_id}")
        return asset_id, asset

    def read_asset(
        self,
        asset_id: int,
        max_size: int | None = None,
        resolve_mesh: bool = False,
    ) -> tuple[object, bytes]:
        _, asset = self._asset_and_entry(asset_id, resolve_mesh=resolve_mesh)
        locator = json.loads(asset["locator"])
        with self.source.open_archive(locator) as stream:
            info = self._archive_info(int(asset["archive_id"]), stream)
            entry = find_entry(
                info,
                asset["path"],
                int(asset["payload_offset"]),
                int(asset["size"]),
            )
            data = self._read_entry(
                int(asset["archive_id"]),
                str(asset["archive_path"]),
                stream,
                info,
                entry,
                max_size,
            )
        return asset, data

    def read_asset_path(
        self,
        path: str,
        max_size: int | None = None,
        preferred_archives: tuple[str, ...] = (),
    ) -> tuple[object, bytes]:
        asset = self.catalog.find_asset(path, preferred_archives)
        if asset is None:
            raise ExtractionError(f"Indexed asset was not found: {path}")
        return self.read_asset(int(asset["id"]), max_size=max_size)

    def extract_asset(
        self,
        asset_id: int,
        destination: str | os.PathLike[str] | None = None,
        resolve_mesh: bool = False,
    ) -> tuple[object, Path]:
        _, asset = self._asset_and_entry(asset_id, resolve_mesh=resolve_mesh)
        if destination is None:
            suffix = extension_for(asset["asset_type"], asset["path"])
            destination_path = (
                self.source_cache
                / "assets"
                / safe_cache_name(asset["path"], suffix)
            )
        else:
            destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists() and destination_path.stat().st_size == asset["size"]:
            return asset, destination_path
        locator = json.loads(asset["locator"])
        temporary = destination_path.with_name(destination_path.name + ".partial")
        with self.source.open_archive(locator) as stream:
            info = self._archive_info(int(asset["archive_id"]), stream)
            entry = find_entry(
                info,
                asset["path"],
                int(asset["payload_offset"]),
                int(asset["size"]),
            )
            with open(temporary, "wb") as target:
                payload_path = self._cached_payload_path(
                    int(asset["archive_id"]),
                    str(asset["archive_path"]),
                    info,
                )
                if info.payload_compressed and payload_path is not None:
                    with open(payload_path, "rb") as payload:
                        payload.seek(entry.offset)
                        copy_exact(payload, target, entry.size)
                else:
                    copy_entry(stream, info, entry, target)
        os.replace(temporary, destination_path)
        return asset, destination_path
