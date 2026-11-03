# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bc2_frostbite_importer.core.level import (
    LevelResolver,
    index_level_archives,
)
from bc2_frostbite_importer.core.repository import AssetRepository


EXPECTED_DATABASES = {
    "systems/ui/shaders.res": (11, 1),
    "scenes/uibackground/shaders/single.res": (3, 2),
    "levels/coral_sea/shaders/single.res": (299, 1174),
    "levels/guadal_canal/shaders/single.res": (563, 2663),
    "levels/iwo_jima_s/shaders/single.res": (594, 2752),
    "levels/wake_island_s/shaders/single.res": (544, 2553),
    "levels/wake_island_tutorial/shaders/single.res": (551, 2582),
}

EXPECTED_MAPS = {
    "CORAL_SEA": {
        "placements": 2889,
        "meshes": 29,
        "surfaces": 79,
        "matches": {"exact": 78, "missing": 1},
    },
    "GUADAL_CANAL": {
        "placements": 5265,
        "meshes": 102,
        "surfaces": 257,
        "matches": {"exact": 253, "missing": 4},
    },
    "IWO_JIMA_S": {
        "placements": 3672,
        "meshes": 110,
        "surfaces": 288,
        "matches": {
            "exact": 277,
            "shader_reference_suffix": 2,
            "missing": 9,
        },
    },
    "WAKE_ISLAND_S": {
        "placements": 4328,
        "meshes": 97,
        "surfaces": 237,
        "matches": {
            "exact": 228,
            "shader_reference_suffix": 1,
            "missing": 8,
        },
    },
    "WAKE_ISLAND_TUTORIAL": {
        "placements": 4419,
        "meshes": 100,
        "surfaces": 241,
        "matches": {
            "exact": 232,
            "shader_reference_suffix": 1,
            "missing": 8,
        },
    },
}


def _arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--cache", required=True)
    return parser.parse_args()


def _material(database, surface):
    material = database.material(surface.database_key)
    if material is not None:
        return material, "exact"

    prefix, separator, name = surface.database_key.rpartition("/")
    if separator and name.casefold().startswith("cheap_"):
        material = database.material(prefix + "/" + name[6:])
        if material is not None:
            return material, "cheap_alias"

    partitions = [surface.shader_partition]
    prefix, separator, name = surface.shader_partition.rpartition("/")
    if separator and name.casefold().startswith("cheap_"):
        partitions.append(prefix + "/" + name[6:])
    for partition in partitions:
        matches = database.materials_for_shader_partition(partition)
        if len(matches) == 1:
            return matches[0], "shader_reference_suffix"
    return None, "missing"


def _texture_asset(repository, path, preferred_archives):
    candidates = (path,) if path.casefold().endswith(".res") else (
        path + ".res",
        path,
    )
    for candidate in candidates:
        asset = repository.catalog.find_asset(
            candidate,
            preferred_archives,
        )
        if asset is not None:
            return asset
    return None


def main():
    args = _arguments()
    repository = AssetRepository(args.source, args.cache)
    try:
        if repository.kind != "XBOX360_STFS":
            raise AssertionError(
                f"Expected XBOX360_STFS, got {repository.kind}"
            )
        if repository.needs_scan():
            failures = repository.scan()
            if failures:
                raise AssertionError(
                    f"Source scan had {len(failures)} failures"
                )

        database_rows = repository.catalog.connection.execute(
            """
            SELECT assets.id, assets.path
            FROM assets
            WHERE assets.asset_type = 'Dx9ShaderDatabase'
            ORDER BY assets.path COLLATE NOCASE
            """
        ).fetchall()
        observed_paths = {
            str(row["path"]).casefold() for row in database_rows
        }
        if observed_paths != set(EXPECTED_DATABASES):
            raise AssertionError(
                "Unexpected BF1943 shader databases: "
                + json.dumps(sorted(observed_paths))
            )

        databases = {}
        for row in database_rows:
            _, database = repository.read_shader_database(int(row["id"]))
            path = str(row["path"]).casefold()
            observed = (
                len(database.materials),
                sum(
                    len(material.textures)
                    for material in database.materials
                ),
            )
            if observed != EXPECTED_DATABASES[path]:
                raise AssertionError(
                    f"{row['path']} decoded {observed}, expected "
                    f"{EXPECTED_DATABASES[path]}"
                )
            if database.trailing_bytes:
                raise AssertionError(
                    f"{row['path']} has {database.trailing_bytes} trailing "
                    "bytes"
                )
            databases[path] = database

        maps = {}
        for level_name, expected in EXPECTED_MAPS.items():
            archives = index_level_archives(repository, level_name)
            scene = LevelResolver(
                repository,
                level_name,
                archives,
            ).resolve()
            database = databases[
                f"levels/{level_name.casefold()}/shaders/single.res"
            ]
            surfaces = {
                surface.database_key.casefold(): surface
                for definition in scene.meshes
                for surface in definition.surfaces
            }
            matches = collections.Counter()
            binding_count = 0
            missing_textures = set()
            for surface in surfaces.values():
                material, match = _material(database, surface)
                matches[match] += 1
                if material is None:
                    continue
                binding_count += len(material.textures)
                for binding in material.textures:
                    if (
                        _texture_asset(
                            repository,
                            binding.path,
                            archives,
                        )
                        is None
                    ):
                        missing_textures.add(binding.path)

            actual = {
                "placements": len(scene.placements),
                "meshes": len(scene.meshes),
                "surfaces": len(surfaces),
                "matches": dict(matches),
            }
            if actual != expected:
                raise AssertionError(
                    f"{level_name} decoded {actual}, expected {expected}"
                )
            if not binding_count:
                raise AssertionError(
                    f"{level_name} resolved no texture bindings"
                )
            if missing_textures:
                raise AssertionError(
                    f"{level_name} has missing texture resources: "
                    + json.dumps(sorted(missing_textures))
                )
            maps[level_name] = {
                **actual,
                "bindings": binding_count,
            }

        print(
            "PASS "
            + json.dumps(
                {
                    "databases": {
                        path: {
                            "materials": values[0],
                            "bindings": values[1],
                        }
                        for path, values in EXPECTED_DATABASES.items()
                    },
                    "maps": maps,
                },
                sort_keys=True,
            )
        )
    finally:
        repository.close()


if __name__ == "__main__":
    main()
