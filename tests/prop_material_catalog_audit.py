# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bc2_frostbite_importer.core.dbx import parse_dbx
from bc2_frostbite_importer.core.level import LevelResolver
from bc2_frostbite_importer.core.repository import AssetRepository


MESH_SET_TYPES = ("RigidMeshSet", "TreeMeshSet")


def _arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def _mesh_sets(repository):
    rows = repository.catalog.connection.execute(
        """
        SELECT assets.path, assets.asset_type,
               archives.path AS archive_path
        FROM assets
        JOIN archives ON archives.id = assets.archive_id
        WHERE assets.asset_type IN ('RigidMeshSet', 'TreeMeshSet')
          AND archives.platform = 'pc'
        ORDER BY assets.path COLLATE NOCASE,
                 archives.path COLLATE NOCASE,
                 assets.id
        """
    ).fetchall()
    unique = {}
    for row in rows:
        unique.setdefault(str(row["path"]).casefold(), row)
    return tuple(unique.values())


def _lookup_terms(value):
    keys = [value]
    prefix, separator, name = value.rpartition("/")
    if separator and name.casefold().startswith("cheap_"):
        keys.append(prefix + "/" + name[len("cheap_") :])
    return tuple(key.casefold() for key in keys if key)


def _material_match(keys_by_database, surface):
    requested_keys = _lookup_terms(surface.database_key)
    if any(
        requested in material_keys
        for requested in requested_keys
        for material_keys in keys_by_database.values()
    ):
        return "exact"
    for partition in _lookup_terms(surface.shader_partition):
        for material_keys in keys_by_database.values():
            matches = sum(
                key == partition or key.endswith("/" + partition)
                for key in material_keys
            )
            if matches == 1:
                return "unique_partition"
    return "missing"


def _ensure_shader_key_catalog(repository):
    databases = repository.catalog.shader_database_assets()
    for index, database in enumerate(databases, start=1):
        if not database["shader_scanned"]:
            repository.read_shader_database(
                int(database["id"]),
                material_keys=(),
            )
        print(
            f"SHADER_INDEX_PROGRESS {index}/{len(databases)}",
            flush=True,
        )
    return repository.catalog.shader_database_assets()


def _empty_summary():
    return {
        "assets": 0,
        "descriptors": 0,
        "resolved_descriptors": 0,
        "surface_count": 0,
        "material_matches": Counter(),
        "zero_surface_descriptors": [],
        "descriptorless_assets": [],
        "resolver_issues": [],
        "unmatched_surfaces": [],
        "errors": [],
    }


def main():
    arguments = _arguments()
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    repository = AssetRepository(arguments.iso, arguments.cache)
    try:
        unindexed = repository.catalog.unindexed_archives()
        if unindexed:
            print(
                "PROP_AUDIT_UNINDEXED "
                + json.dumps(
                    [str(archive["path"]) for archive in unindexed]
                ),
                flush=True,
            )
        databases = _ensure_shader_key_catalog(repository)
        keys_by_database = {
            int(database["id"]): frozenset(
                key.casefold()
                for key in (
                    repository.catalog.shader_database_material_keys(
                        int(database["id"])
                    )
                )
            )
            for database in databases
        }
        summary = {
            asset_type: _empty_summary()
            for asset_type in MESH_SET_TYPES
        }
        rows = _mesh_sets(repository)
        for index, row in enumerate(rows, start=1):
            asset_type = str(row["asset_type"])
            result = summary[asset_type]
            result["assets"] += 1
            path = str(row["path"])
            archive_path = str(row["archive_path"])
            descriptor_path = path.removesuffix(".res") + ".dbx"
            descriptor = repository.catalog.find_asset(
                descriptor_path,
                (archive_path,),
            )
            if descriptor is None:
                result["descriptorless_assets"].append(path)
                continue
            result["descriptors"] += 1
            try:
                _, raw = repository.read_asset(
                    int(descriptor["id"]),
                    max_size=256 << 20,
                )
                document = parse_dbx(raw)
                primary = document.primary_instance
                guid = (
                    primary.attribute("guid")
                    if primary is not None
                    else None
                )
                if not guid:
                    raise RuntimeError(
                        "descriptor has no primary instance GUID"
                    )
                reference = (
                    descriptor_path.removesuffix(".dbx") + "/" + guid
                )
                resolver = LevelResolver(
                    repository,
                    "PROP_AUDIT",
                    (archive_path,),
                )
                definition = resolver.resolve_mesh(reference)
                result["resolved_descriptors"] += 1
                result["surface_count"] += len(definition.surfaces)
                if not definition.surfaces:
                    result["zero_surface_descriptors"].append(path)
                result["resolver_issues"].extend(
                    {
                        "mesh": path,
                        "code": issue.code,
                        "message": issue.message,
                        "reference": issue.reference or "",
                    }
                    for issue in resolver.issues
                )
                for surface in definition.surfaces:
                    match = _material_match(
                        keys_by_database,
                        surface,
                    )
                    result["material_matches"][match] += 1
                    if match == "missing":
                        result["unmatched_surfaces"].append(
                            {
                                "mesh": path,
                                "surface": surface.name,
                                "key": surface.database_key,
                                "partition": (
                                    surface.shader_partition
                                ),
                            }
                        )
            except Exception as exc:
                result["errors"].append(
                    {
                        "mesh": path,
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
            if index % 100 == 0:
                print(
                    f"PROP_AUDIT_PROGRESS {index}/{len(rows)}",
                    flush=True,
                )

        report = {
            "platform": "pc",
            "archive_inventory": {
                "unindexed": [
                    str(archive["path"]) for archive in unindexed
                ],
            },
            "shader_database_inventory": {
                "assets": len(databases),
                "scanned": sum(
                    bool(database["shader_scanned"])
                    for database in databases
                ),
                "material_keys": sum(
                    len(material_keys)
                    for material_keys in keys_by_database.values()
                ),
            },
            "mesh_sets": {
                asset_type: {
                    **result,
                    "material_matches": dict(
                        result["material_matches"]
                    ),
                }
                for asset_type, result in summary.items()
            },
        }
        output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            "PROP_MATERIAL_CATALOG_AUDIT "
            + json.dumps(
                {
                    "output": str(output),
                    "shader_database_inventory": report[
                        "shader_database_inventory"
                    ],
                    "mesh_sets": {
                        asset_type: {
                            "assets": result["assets"],
                            "descriptors": result["descriptors"],
                            "resolved_descriptors": result[
                                "resolved_descriptors"
                            ],
                            "surface_count": result["surface_count"],
                            "material_matches": dict(
                                result["material_matches"]
                            ),
                            "errors": len(result["errors"]),
                        }
                        for asset_type, result in summary.items()
                    },
                },
                sort_keys=True,
            )
        )
    finally:
        repository.close()


if __name__ == "__main__":
    main()
