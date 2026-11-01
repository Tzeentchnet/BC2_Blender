# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import gzip
import io
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

from .errors import CorruptDataError
from .io import copy_exact, read_exact


MAX_METADATA_SIZE = 512 << 20
MAX_ENTRY_COUNT = 2_000_000


TYPE_EXTENSIONS = {
    "<non-resource>": "",
    "AimAnimation": ".aimanimation",
    "AnimTreeInfo": ".animtreeinfo",
    "BinkMemory": ".binkmemory",
    "CompositeMeshSet": ".compositemeshset",
    "DeltaAnimation": ".deltaanimation",
    "Dx9ShaderDatabase": ".dx9shaderdatabase",
    "GrannyAnimation": ".grannyanimation",
    "GrannyModel": ".grannymodel",
    "HavokPhysicsData": ".havokphysicsdata",
    "ImpulseResponse": ".impulseresponse",
    "ITexture": ".itexture",
    "IrradianceVolume": ".irradiancevolume",
    "MeshData": ".meshdata",
    "OccluderMesh": ".occludermesh",
    "RagdollResource": ".ragdollresource",
    "RigidMeshSet": ".rigidmeshset",
    "SkinnedMeshSet": ".skinnedmeshset",
    "SootMesh": ".sootmesh",
    "SwfMovie": ".swfmovie",
    "Terrain": ".terrain",
    "TerrainHeightfield": ".terrainheightfield",
    "TerrainMaterialMap": ".terrainmaterialmap",
    "TreeMeshSet": ".treemeshset",
    "VisualTerrain": ".visualterrain",
    "VisualWater": ".visualwater",
    "WaterMesh": ".watermesh",
    "Wave": ".wave",
    "WeatherSystem": ".weathersystem",
    "XenonTexture": ".xenontexture",
}


MESH_TYPES = {
    "MeshData",
    "RigidMeshSet",
    "SkinnedMeshSet",
    "CompositeMeshSet",
    "TreeMeshSet",
    "OccluderMesh",
    "SootMesh",
    "WaterMesh",
}
TEXTURE_TYPES = {"ITexture", "XenonTexture", "Ps3Texture", "TerrainMaterialMap"}
TERRAIN_TYPES = {
    "Terrain",
    "TerrainHeightfield",
    "VisualTerrain",
    "VisualWater",
    "IrradianceVolume",
}
ANIMATION_TYPES = {
    "AimAnimation",
    "AnimTreeInfo",
    "DeltaAnimation",
    "GrannyAnimation",
    "GrannyModel",
    "RagdollResource",
}
AUDIO_TYPES = {"Wave", "ImpulseResponse"}
UI_VIDEO_TYPES = {"SwfMovie", "BinkMemory"}


def asset_category(asset_type: str, path: str) -> str:
    if asset_type in MESH_TYPES:
        return "MESH"
    if asset_type in TEXTURE_TYPES:
        return "TEXTURE"
    if asset_type in TERRAIN_TYPES:
        return "TERRAIN"
    if asset_type in ANIMATION_TYPES:
        return "ANIMATION"
    if asset_type in AUDIO_TYPES:
        return "AUDIO"
    if asset_type in UI_VIDEO_TYPES:
        return "UI_VIDEO"
    if asset_type == "<non-resource>":
        suffix = Path(path).suffix.casefold()
        return "LEVEL_DATA" if suffix in {".dbx", ".dbmanifest", ".xml"} else "DATA"
    return "OTHER"


def extension_for(asset_type: str, path: str) -> str:
    existing = Path(path).suffix
    if asset_type == "<non-resource>" and existing:
        return existing
    return TYPE_EXTENSIONS.get(asset_type, existing or ".bin")


@dataclass(frozen=True)
class FbrbEntry:
    path: str
    asset_type: str
    offset: int
    size: int
    alternate_size: int
    flags: int

    @property
    def deleted(self) -> bool:
        return self.flags == 0

    @property
    def category(self) -> str:
        return asset_category(self.asset_type, self.path)


@dataclass(frozen=True)
class FbrbInfo:
    metadata_compressed_size: int
    payload_offset: int
    payload_compressed: bool
    payload_size: int
    entries: tuple[FbrbEntry, ...]


def _decompress_metadata(data: bytes) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as compressed:
            metadata = compressed.read(MAX_METADATA_SIZE + 1)
    except (OSError, EOFError) as exc:
        raise CorruptDataError(f"Invalid FbRB metadata gzip stream: {exc}") from exc
    if len(metadata) > MAX_METADATA_SIZE:
        raise CorruptDataError("FbRB metadata exceeds the safety limit")
    return metadata


def _read_string(strings: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(strings):
        raise CorruptDataError(f"FbRB string offset is out of range: {offset}")
    end = strings.find(b"\0", offset)
    if end < 0:
        raise CorruptDataError("Unterminated FbRB metadata string")
    return strings[offset:end].decode("utf-8", "replace")


def read_fbrb(stream: BinaryIO) -> FbrbInfo:
    stream.seek(0)
    header = read_exact(stream, 8, "FbRB header")
    if header[:4] != b"FbRB":
        raise CorruptDataError(
            f"Invalid FbRB signature: expected b'FbRB', found {header[:4]!r}"
        )
    compressed_size = struct.unpack_from(">I", header, 4)[0]
    if compressed_size < 18 or compressed_size > MAX_METADATA_SIZE:
        raise CorruptDataError(
            f"Implausible FbRB metadata size: {compressed_size:,} bytes"
        )
    metadata = _decompress_metadata(
        read_exact(stream, compressed_size, "FbRB compressed metadata")
    )
    if len(metadata) < 13:
        raise CorruptDataError("FbRB metadata is too short")
    _, string_size = struct.unpack_from(">II", metadata, 0)
    strings_end = 8 + string_size
    if strings_end + 5 > len(metadata):
        raise CorruptDataError("FbRB string table exceeds metadata")
    strings = metadata[8:strings_end]
    entry_count = struct.unpack_from(">I", metadata, strings_end)[0]
    if entry_count > MAX_ENTRY_COUNT:
        raise CorruptDataError(f"FbRB entry count is unsafe: {entry_count:,}")
    entries_start = strings_end + 4
    entries_end = entries_start + entry_count * 24
    if entries_end + 5 > len(metadata):
        raise CorruptDataError("FbRB file table exceeds metadata")
    entries: list[FbrbEntry] = []
    for index in range(entry_count):
        values = struct.unpack_from(">6I", metadata, entries_start + index * 24)
        path_offset, flags, offset, size, alternate_size, type_offset = values
        entries.append(
            FbrbEntry(
                path=_read_string(strings, path_offset),
                asset_type=_read_string(strings, type_offset),
                offset=offset,
                size=size,
                alternate_size=alternate_size,
                flags=flags,
            )
        )
    payload_compressed = metadata[entries_end] != 0
    payload_size = struct.unpack_from(">I", metadata, entries_end + 1)[0]
    maximum_entry_end = max(
        (entry.offset + entry.size for entry in entries if not entry.deleted),
        default=0,
    )
    if maximum_entry_end > payload_size:
        discrepancy = maximum_entry_end - payload_size
        tolerance = max(1 << 20, payload_size // 1000)
        if discrepancy > tolerance:
            raise CorruptDataError(
                f"FbRB file table exceeds its declared payload by {discrepancy:,} bytes"
            )
        payload_size = maximum_entry_end
    return FbrbInfo(
        metadata_compressed_size=compressed_size,
        payload_offset=8 + compressed_size,
        payload_compressed=payload_compressed,
        payload_size=payload_size,
        entries=tuple(entries),
    )


def find_entry(
    info: FbrbInfo, path: str, offset: int | None = None, size: int | None = None
) -> FbrbEntry:
    for entry in info.entries:
        if entry.path != path:
            continue
        if offset is not None and entry.offset != offset:
            continue
        if size is not None and entry.size != size:
            continue
        return entry
    raise CorruptDataError(f"Asset is no longer present in its FbRB archive: {path}")


def _skip_exact(stream: BinaryIO, size: int):
    remaining = size
    while remaining:
        chunk = stream.read(min(4 << 20, remaining))
        if not chunk:
            raise CorruptDataError(
                f"FbRB payload ended while skipping {size:,} bytes"
            )
        remaining -= len(chunk)


def copy_entry(
    archive: BinaryIO,
    info: FbrbInfo,
    entry: FbrbEntry,
    target: BinaryIO,
    progress: Callable[[int, int], None] | None = None,
):
    if entry.deleted:
        raise CorruptDataError(f"Cannot extract deleted FbRB entry: {entry.path}")
    archive.seek(info.payload_offset)
    if info.payload_compressed:
        try:
            payload: BinaryIO = gzip.GzipFile(fileobj=archive, mode="rb")
            _skip_exact(payload, entry.offset)
        except (OSError, EOFError) as exc:
            raise CorruptDataError(f"Invalid FbRB payload gzip stream: {exc}") from exc
    else:
        archive.seek(info.payload_offset + entry.offset)
        payload = archive
    if progress is None:
        copy_exact(payload, target, entry.size)
        return
    remaining = entry.size
    completed = 0
    while remaining:
        chunk = payload.read(min(4 << 20, remaining))
        if not chunk:
            raise CorruptDataError(
                f"FbRB asset is truncated: {entry.path} ({remaining:,} bytes missing)"
            )
        target.write(chunk)
        completed += len(chunk)
        remaining -= len(chunk)
        progress(completed, entry.size)


def read_entry_bytes(
    archive: BinaryIO,
    info: FbrbInfo,
    entry: FbrbEntry,
    max_size: int | None = None,
) -> bytes:
    if max_size is not None and entry.size > max_size:
        raise CorruptDataError(
            f"Asset is too large to load in memory ({entry.size:,} > {max_size:,})"
        )
    target = io.BytesIO()
    copy_entry(archive, info, entry, target)
    return target.getvalue()
