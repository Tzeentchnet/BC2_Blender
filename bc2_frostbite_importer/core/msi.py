# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import PurePosixPath

from .errors import ExtractionError, PlatformRequirementError
from .records import InstallerFile


ERROR_SUCCESS = 0
ERROR_MORE_DATA = 234
ERROR_NO_MORE_ITEMS = 259


class _MsiDatabase:
    def __init__(self, path: str):
        if os.name != "nt":
            raise PlatformRequirementError(
                "Reading the PC installer database currently requires Windows"
            )
        self._msi = ctypes.WinDLL("msi", use_last_error=True)
        self._configure_api()
        self.handle = wintypes.UINT(0)
        result = self._msi.MsiOpenDatabaseW(path, None, ctypes.byref(self.handle))
        self._check(result, f"open MSI database {path}")

    def _configure_api(self):
        handle = wintypes.UINT
        self._msi.MsiOpenDatabaseW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.POINTER(handle),
        ]
        self._msi.MsiOpenDatabaseW.restype = wintypes.UINT
        self._msi.MsiDatabaseOpenViewW.argtypes = [
            handle,
            wintypes.LPCWSTR,
            ctypes.POINTER(handle),
        ]
        self._msi.MsiDatabaseOpenViewW.restype = wintypes.UINT
        self._msi.MsiViewExecute.argtypes = [handle, handle]
        self._msi.MsiViewExecute.restype = wintypes.UINT
        self._msi.MsiViewFetch.argtypes = [handle, ctypes.POINTER(handle)]
        self._msi.MsiViewFetch.restype = wintypes.UINT
        self._msi.MsiRecordGetStringW.argtypes = [
            handle,
            wintypes.UINT,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._msi.MsiRecordGetStringW.restype = wintypes.UINT
        self._msi.MsiCloseHandle.argtypes = [handle]
        self._msi.MsiCloseHandle.restype = wintypes.UINT

    @staticmethod
    def _check(result: int, operation: str):
        if result != ERROR_SUCCESS:
            raise ExtractionError(f"Windows Installer failed to {operation} (error {result})")

    def _string(self, record: int, field: int) -> str:
        size = wintypes.DWORD(0)
        result = self._msi.MsiRecordGetStringW(
            record, field, None, ctypes.byref(size)
        )
        if result not in (ERROR_SUCCESS, ERROR_MORE_DATA):
            self._check(result, f"read MSI field {field}")
        capacity = wintypes.DWORD(size.value + 1)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        result = self._msi.MsiRecordGetStringW(
            record, field, buffer, ctypes.byref(capacity)
        )
        self._check(result, f"read MSI field {field}")
        return buffer.value

    def query(self, sql: str, columns: int) -> list[tuple[str, ...]]:
        view = wintypes.UINT(0)
        result = self._msi.MsiDatabaseOpenViewW(
            self.handle, sql, ctypes.byref(view)
        )
        self._check(result, f"open MSI query: {sql}")
        rows: list[tuple[str, ...]] = []
        try:
            self._check(self._msi.MsiViewExecute(view, 0), "execute MSI query")
            while True:
                record = wintypes.UINT(0)
                result = self._msi.MsiViewFetch(view, ctypes.byref(record))
                if result == ERROR_NO_MORE_ITEMS:
                    break
                self._check(result, "fetch MSI row")
                try:
                    rows.append(
                        tuple(self._string(record, field) for field in range(1, columns + 1))
                    )
                finally:
                    self._msi.MsiCloseHandle(record)
        finally:
            self._msi.MsiCloseHandle(view)
        return rows

    def close(self):
        if self.handle:
            self._msi.MsiCloseHandle(self.handle)
            self.handle = wintypes.UINT(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _long_name(value: str) -> str:
    target = value.rsplit(":", 1)[-1]
    name = target.rsplit("|", 1)[-1]
    return "" if name in ("", ".") else name


def read_installer_files(database_path: str) -> list[InstallerFile]:
    with _MsiDatabase(database_path) as database:
        directory_rows = database.query(
            "SELECT `Directory`, `Directory_Parent`, `DefaultDir` FROM `Directory`",
            3,
        )
        component_rows = database.query(
            "SELECT `Component`, `Directory_` FROM `Component`", 2
        )
        file_rows = database.query(
            "SELECT `File`, `Component_`, `FileName`, `FileSize`, `Sequence` "
            "FROM `File` ORDER BY `Sequence`",
            5,
        )
        media_rows = database.query(
            "SELECT `DiskId`, `LastSequence`, `Cabinet` FROM `Media` "
            "ORDER BY `DiskId`",
            3,
        )

    directories = {
        key: (parent, _long_name(default_dir))
        for key, parent, default_dir in directory_rows
    }
    components = dict(component_rows)
    resolved_directories: dict[str, tuple[str, ...]] = {}

    def resolve_directory(key: str, stack: frozenset[str] = frozenset()) -> tuple[str, ...]:
        if key in resolved_directories:
            return resolved_directories[key]
        if key in stack:
            raise ExtractionError(f"MSI directory graph contains a cycle at {key}")
        parent, name = directories.get(key, ("", ""))
        parts = resolve_directory(parent, stack | {key}) if parent else ()
        if name:
            parts += (name,)
        resolved_directories[key] = parts
        return parts

    media: list[tuple[int, int, str | None]] = []
    previous_last = -1
    for _, last_sequence, cabinet in media_rows:
        current_last = int(last_sequence or 0)
        external = cabinet if cabinet and not cabinet.startswith("#") else None
        media.append((previous_last + 1, current_last, external))
        previous_last = current_last

    def cabinet_for(sequence: int) -> str | None:
        for first, last, cabinet in media:
            if first <= sequence <= last:
                return cabinet
        return None

    files: list[InstallerFile] = []
    for file_key, component, file_name, file_size, sequence_raw in file_rows:
        sequence = int(sequence_raw)
        directory_key = components.get(component, "")
        path_parts = resolve_directory(directory_key) + (_long_name(file_name),)
        lowered = [part.casefold() for part in path_parts]
        start = 0
        for root_name in ("dist", "output"):
            if root_name in lowered:
                start = lowered.index(root_name)
                break
        path = str(PurePosixPath(*path_parts[start:]))
        files.append(
            InstallerFile(
                file_key=file_key,
                path=path,
                size=int(file_size),
                sequence=sequence,
                cabinet=cabinet_for(sequence),
            )
        )
    return files
