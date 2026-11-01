# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DiscFile:
    path: str
    offset: int
    size: int
    is_directory: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArchiveDescriptor:
    path: str
    size: int
    platform: str
    locator: dict[str, Any]


@dataclass(frozen=True)
class InstallerFile:
    file_key: str
    path: str
    size: int
    sequence: int
    cabinet: str | None
