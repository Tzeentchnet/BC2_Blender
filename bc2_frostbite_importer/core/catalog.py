# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .fbrb import FbrbEntry
from .records import ArchiveDescriptor


SCHEMA_VERSION = "1"
_CONTENT_GROUP_PREFIXES = {
    "CHARACTER": (
        "characters/%",
        "animations/faces/%",
        "animations/animals/%",
        "objects/soldiers/%",
        "objects/animals/%",
    ),
    "VEHICLE": ("objects/vehicles/%",),
    "WEAPON": ("objects/weapons/%",),
    "BUILDING": (
        "objects/buildings/%",
        "objects/architecture/%",
    ),
    "VEGETATION": (
        "vegetation/%",
        "objects/vegetation/%",
        "objects/undergrowth/%",
    ),
    "LEVEL": (
        "levels/%",
        "terrains/%",
        "weather/%",
        "gamemodes/%",
        "scenes/%",
    ),
    "EFFECT": (
        "fx/%",
        "effects/%",
        "decals/%",
    ),
    "UI": (
        "menu/%",
        "movies/%",
        "awards/%",
        "fonts/%",
        "localization/%",
        "objects/ui/%",
    ),
    "AUDIO": ("sound/%",),
}
_NON_PROP_OBJECT_PREFIXES = tuple(
    prefix
    for prefixes in _CONTENT_GROUP_PREFIXES.values()
    for prefix in prefixes
    if prefix.startswith("objects/")
)


def _prefix_filter(
    column: str,
    prefixes: tuple[str, ...],
) -> tuple[str, list[object]]:
    return (
        "(" + " OR ".join(f"{column} LIKE ?" for _ in prefixes) + ")",
        list(prefixes),
    )


def _content_group_filter(
    content_group: str,
    column: str,
) -> tuple[str | None, list[object]]:
    if content_group == "ALL":
        return None, []
    if content_group == "PROP":
        excluded, parameters = _prefix_filter(
            column,
            _NON_PROP_OBJECT_PREFIXES,
        )
        return (
            f"({column} LIKE ? AND NOT {excluded})",
            ["objects/%", *parameters],
        )
    prefixes = _CONTENT_GROUP_PREFIXES.get(content_group)
    if prefixes is not None:
        return _prefix_filter(column, prefixes)
    if content_group == "OTHER":
        recognized = tuple(
            dict.fromkeys(
                (
                    "objects/%",
                    *(
                        prefix
                        for values in _CONTENT_GROUP_PREFIXES.values()
                        for prefix in values
                    ),
                )
            )
        )
        expression, parameters = _prefix_filter(column, recognized)
        return f"NOT {expression}", parameters
    raise ValueError(f"Unknown asset content group: {content_group}")


class AssetCatalog:
    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self):
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS archives (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL UNIQUE COLLATE NOCASE,
                size INTEGER NOT NULL,
                platform TEXT NOT NULL,
                locator TEXT NOT NULL,
                indexed INTEGER NOT NULL DEFAULT 0,
                payload_offset INTEGER,
                payload_compressed INTEGER,
                payload_size INTEGER,
                asset_count INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY,
                archive_id INTEGER NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
                path TEXT NOT NULL COLLATE NOCASE,
                asset_type TEXT NOT NULL,
                category TEXT NOT NULL,
                payload_offset INTEGER NOT NULL,
                size INTEGER NOT NULL,
                alternate_size INTEGER NOT NULL,
                flags INTEGER NOT NULL,
                UNIQUE(archive_id, path, payload_offset)
            );
            CREATE INDEX IF NOT EXISTS assets_category_idx ON assets(category);
            CREATE INDEX IF NOT EXISTS assets_type_idx ON assets(asset_type);
            CREATE INDEX IF NOT EXISTS assets_path_idx ON assets(path);
            """
        )
        self.connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        self.connection.commit()

    def close(self):
        self.connection.close()

    def reset(self, source_path: str, source_kind: str, fingerprint: str):
        with self.connection:
            self.connection.execute("DELETE FROM assets")
            self.connection.execute("DELETE FROM archives")
            self.connection.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (
                    ("source_path", source_path),
                    ("source_kind", source_kind),
                    ("source_fingerprint", fingerprint),
                ),
            )

    def meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def upsert_archive(self, archive: ArchiveDescriptor) -> int:
        locator = json.dumps(archive.locator, sort_keys=True, separators=(",", ":"))
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO archives(path, size, platform, locator)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    platform=excluded.platform,
                    locator=excluded.locator
                """,
                (archive.path, archive.size, archive.platform, locator),
            )
        row = self.connection.execute(
            "SELECT id FROM archives WHERE path = ?", (archive.path,)
        ).fetchone()
        return int(row["id"])

    def archive(self, archive_id: int):
        return self.connection.execute(
            "SELECT * FROM archives WHERE id = ?", (archive_id,)
        ).fetchone()

    def archive_by_path(self, path: str):
        return self.connection.execute(
            "SELECT * FROM archives WHERE path = ? COLLATE NOCASE",
            (path,),
        ).fetchone()

    def archives(self, query: str = "", limit: int = 500, offset: int = 0):
        like = f"%{query}%"
        return self.connection.execute(
            """
            SELECT * FROM archives
            WHERE path LIKE ?
            ORDER BY path COLLATE NOCASE
            LIMIT ? OFFSET ?
            """,
            (like, limit, offset),
        ).fetchall()

    def unindexed_archives(self):
        return self.connection.execute(
            """
            SELECT * FROM archives
            WHERE indexed = 0
            ORDER BY path COLLATE NOCASE
            """
        ).fetchall()

    def archive_count(self, query: str = "") -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM archives WHERE path LIKE ?",
            (f"%{query}%",),
        ).fetchone()
        return int(row["count"])

    def archive_index_counts(self) -> tuple[int, int]:
        row = self.connection.execute(
            """
            SELECT
                SUM(CASE WHEN indexed = 1 THEN 1 ELSE 0 END) AS indexed_count,
                COUNT(*) AS total_count
            FROM archives
            """
        ).fetchone()
        return int(row["indexed_count"] or 0), int(row["total_count"])

    def replace_archive_assets(
        self,
        archive_id: int,
        entries: Iterable[FbrbEntry],
        payload_offset: int,
        payload_compressed: bool,
        payload_size: int,
    ):
        active = [entry for entry in entries if not entry.deleted]
        with self.connection:
            self.connection.execute(
                "DELETE FROM assets WHERE archive_id = ?", (archive_id,)
            )
            self.connection.executemany(
                """
                INSERT INTO assets(
                    archive_id, path, asset_type, category, payload_offset,
                    size, alternate_size, flags
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        archive_id,
                        entry.path,
                        entry.asset_type,
                        entry.category,
                        entry.offset,
                        entry.size,
                        entry.alternate_size,
                        entry.flags,
                    )
                    for entry in active
                ),
            )
            self.connection.execute(
                """
                UPDATE archives SET indexed=1, payload_offset=?,
                    payload_compressed=?, payload_size=?, asset_count=?, error=NULL
                WHERE id=?
                """,
                (
                    payload_offset,
                    int(payload_compressed),
                    payload_size,
                    len(active),
                    archive_id,
                ),
            )

    def set_archive_error(self, archive_id: int, message: str):
        with self.connection:
            self.connection.execute(
                "UPDATE archives SET error=? WHERE id=?", (message, archive_id)
            )

    def asset(self, asset_id: int):
        return self.connection.execute(
            """
            SELECT assets.*, archives.path AS archive_path,
                   archives.platform, archives.locator, archives.size AS archive_size
            FROM assets JOIN archives ON archives.id = assets.archive_id
            WHERE assets.id = ?
            """,
            (asset_id,),
        ).fetchone()

    def assets_by_path(self, path: str):
        return self.connection.execute(
            """
            SELECT assets.*, archives.path AS archive_path,
                   archives.platform, archives.locator,
                   archives.size AS archive_size
            FROM assets JOIN archives ON archives.id = assets.archive_id
            WHERE assets.path = ? COLLATE NOCASE
            ORDER BY archives.path COLLATE NOCASE, assets.id
            """,
            (path,),
        ).fetchall()

    def assets_with_prefix(
        self,
        path_prefix: str,
        asset_type: str | None = None,
    ):
        where = ["lower(assets.path) LIKE lower(?)"]
        parameters: list[object] = [path_prefix + "%"]
        if asset_type is not None:
            where.append("assets.asset_type = ?")
            parameters.append(asset_type)
        return self.connection.execute(
            f"""
            SELECT assets.*, archives.path AS archive_path,
                   archives.platform, archives.locator,
                   archives.size AS archive_size
            FROM assets JOIN archives ON archives.id = assets.archive_id
            WHERE {' AND '.join(where)}
            ORDER BY assets.path COLLATE NOCASE, assets.id
            """,
            parameters,
        ).fetchall()

    def find_asset(
        self,
        path: str,
        preferred_archives: Iterable[str] = (),
    ):
        rows = self.assets_by_path(path)
        if not rows:
            return None
        preferred = {
            archive.casefold(): index
            for index, archive in enumerate(preferred_archives)
        }
        return min(
            rows,
            key=lambda row: (
                preferred.get(
                    str(row["archive_path"]).casefold(),
                    len(preferred),
                ),
                str(row["archive_path"]).casefold(),
                int(row["id"]),
            ),
        )

    def find_mesh_data(
        self,
        mesh_partition_path: str,
        preferred_archives: Iterable[str] = (),
    ):
        rows = self.connection.execute(
            """
            SELECT assets.*, archives.path AS archive_path,
                   archives.platform, archives.locator,
                   archives.size AS archive_size
            FROM assets JOIN archives ON archives.id = assets.archive_id
            WHERE assets.asset_type = 'MeshData'
              AND lower(assets.path) LIKE lower(?)
            ORDER BY
                CASE WHEN lower(assets.path) LIKE '%_lod0_data.res' THEN 0 ELSE 1 END,
                assets.path COLLATE NOCASE,
                assets.id
            """,
            (mesh_partition_path + "_lod%_data.res",),
        ).fetchall()
        if not rows:
            return None
        preferred = {
            archive.casefold(): index
            for index, archive in enumerate(preferred_archives)
        }
        return min(
            rows,
            key=lambda row: (
                0
                if str(row["path"]).casefold().endswith("_lod0_data.res")
                else 1,
                preferred.get(
                    str(row["archive_path"]).casefold(),
                    len(preferred),
                ),
                str(row["path"]).casefold(),
                int(row["id"]),
            ),
        )

    def search_assets(
        self,
        category: str = "ALL",
        query: str = "",
        limit: int = 200,
        offset: int = 0,
        content_group: str = "ALL",
    ):
        where = ["(assets.path LIKE ? OR assets.asset_type LIKE ?)"]
        parameters: list[object] = [f"%{query}%", f"%{query}%"]
        if category != "ALL":
            where.append("assets.category = ?")
            parameters.append(category)
        content_filter, content_parameters = _content_group_filter(
            content_group,
            "assets.path",
        )
        if content_filter is not None:
            where.append(content_filter)
            parameters.extend(content_parameters)
        parameters.extend((limit, offset))
        return self.connection.execute(
            f"""
            SELECT assets.*, archives.path AS archive_path, archives.platform
            FROM assets JOIN archives ON archives.id = assets.archive_id
            WHERE {' AND '.join(where)}
            ORDER BY assets.path COLLATE NOCASE
            LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()

    def asset_count(
        self,
        category: str = "ALL",
        query: str = "",
        content_group: str = "ALL",
    ) -> int:
        where = ["(path LIKE ? OR asset_type LIKE ?)"]
        parameters: list[object] = [f"%{query}%", f"%{query}%"]
        if category != "ALL":
            where.append("category = ?")
            parameters.append(category)
        content_filter, content_parameters = _content_group_filter(
            content_group,
            "path",
        )
        if content_filter is not None:
            where.append(content_filter)
            parameters.extend(content_parameters)
        row = self.connection.execute(
            f"SELECT COUNT(*) AS count FROM assets WHERE {' AND '.join(where)}",
            parameters,
        ).fetchone()
        return int(row["count"])

    def category_counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT category, COUNT(*) AS count FROM assets GROUP BY category"
        ).fetchall()
        return {row["category"]: int(row["count"]) for row in rows}

    def resolve_mesh_data(self, asset_id: int) -> int | None:
        asset = self.asset(asset_id)
        if not asset:
            return None
        if asset["asset_type"] == "MeshData":
            return asset_id
        if asset["asset_type"] not in {
            "RigidMeshSet",
            "SkinnedMeshSet",
            "CompositeMeshSet",
            "TreeMeshSet",
        }:
            return None
        path = asset["path"]
        base = path.rsplit(".", 1)[0]
        candidates = self.connection.execute(
            """
            SELECT id, path FROM assets
            WHERE asset_type='MeshData'
              AND lower(path) LIKE lower(?)
            ORDER BY
                CASE WHEN lower(path) LIKE '%_lod0_data.res' THEN 0 ELSE 1 END,
                archive_id = ? DESC,
                path COLLATE NOCASE
            LIMIT 1
            """,
            (base + "_lod%_data.res", asset["archive_id"]),
        ).fetchone()
        return int(candidates["id"]) if candidates else None
