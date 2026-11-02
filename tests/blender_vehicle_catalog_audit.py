# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import traceback
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if "--installed" in sys.argv:
    package = (
        "bl_ext.user_default.bc2_frostbite_importer."
        "bc2_frostbite_importer"
    )
    blender_addon = importlib.import_module(package + ".blender_addon")
    parse_dbx = importlib.import_module(package + ".core.dbx").parse_dbx
    LevelResolver = importlib.import_module(
        package + ".core.level"
    ).LevelResolver
    parse_meshdata = importlib.import_module(
        package + ".core.mesh"
    ).parse_meshdata
    AssetRepository = importlib.import_module(
        package + ".core.repository"
    ).AssetRepository
else:
    sys.path.insert(0, str(ROOT))
    from bc2_frostbite_importer import blender_addon
    from bc2_frostbite_importer.core.dbx import parse_dbx
    from bc2_frostbite_importer.core.level import LevelResolver
    from bc2_frostbite_importer.core.mesh import parse_meshdata
    from bc2_frostbite_importer.core.repository import AssetRepository


EXPECTED_VEHICLE_LOD0_RESOURCES = 62


def _arguments():
    values = sys.argv
    values = values[values.index("--") + 1 :] if "--" in values else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--installed", action="store_true")
    return parser.parse_args(values)


def _vehicle_mesh_paths(repository) -> tuple[str, ...]:
    rows = repository.catalog.connection.execute(
        """
        SELECT MIN(path) AS path
        FROM assets
        WHERE asset_type = 'MeshData'
          AND lower(path) LIKE 'objects/vehicles/%'
          AND lower(path) GLOB '*_lod0_data.res'
        GROUP BY lower(path)
        ORDER BY path COLLATE NOCASE
        """
    ).fetchall()
    return tuple(str(row["path"]) for row in rows)


def _mesh_set_path(mesh_data_path: str) -> str:
    suffix = "_lod0_data.res"
    if not mesh_data_path.casefold().endswith(suffix):
        raise AssertionError(f"Not a LOD0 MeshData path: {mesh_data_path}")
    return mesh_data_path[: -len(suffix)] + ".res"


def _find_material(database, key: str, shader_partition: str):
    material = database.material(key)
    if material is not None:
        return material
    prefix, separator, name = key.rpartition("/")
    if separator and name.casefold().startswith("cheap_"):
        material = database.material(
            prefix + "/" + name[len("cheap_") :]
        )
        if material is not None:
            return material
    matches = database.materials_for_shader_partition(shader_partition)
    if len(matches) == 1:
        return matches[0]
    return None


def _material_sources(
    repository,
    surfaces,
    preferred_archives: tuple[str, ...],
    mesh_path: str,
):
    lookup_keys = []
    shader_partitions = []
    for surface in surfaces:
        keys, partitions = blender_addon._shader_lookup_terms(surface)
        lookup_keys.extend(keys)
        shader_partitions.extend(partitions)
    unresolved = {
        surface.database_key.casefold(): surface
        for surface in surfaces
    }
    materials = {}
    sources = {}
    candidates = repository.shader_database_candidates(
        tuple(dict.fromkeys(lookup_keys)),
        preferred_archives,
        mesh_path,
        tuple(dict.fromkeys(shader_partitions)),
    )
    for candidate in candidates:
        remaining = tuple(unresolved.values())
        if (
            bool(candidate["shader_scanned"])
            and not blender_addon._catalog_shader_database_matches_surfaces(
                repository.catalog,
                int(candidate["id"]),
                remaining,
            )
        ):
            continue
        remaining_keys = []
        remaining_partitions = []
        for surface in remaining:
            keys, partitions = blender_addon._shader_lookup_terms(
                surface
            )
            remaining_keys.extend(keys)
            remaining_partitions.extend(partitions)
        _, database = repository.read_shader_database(
            int(candidate["id"]),
            material_keys=tuple(dict.fromkeys(remaining_keys)),
            shader_partitions=tuple(
                dict.fromkeys(remaining_partitions)
            ),
        )
        for normalized, surface in tuple(unresolved.items()):
            material = _find_material(
                database,
                surface.database_key,
                surface.shader_partition,
            )
            if material is None:
                continue
            materials[normalized] = material
            sources[normalized] = (
                f"{candidate['archive_path']}|{candidate['path']}"
            )
            del unresolved[normalized]
        if not unresolved:
            break
    return (
        materials,
        sources,
        tuple(
            surface.database_key
            for surface in unresolved.values()
        ),
    )


def _mesh_definition(repository, mesh_set_asset):
    preferred = (str(mesh_set_asset["archive_path"]),)
    descriptor_path = (
        blender_addon._resource_partition(mesh_set_asset["path"]) + ".dbx"
    )
    descriptor_asset = repository.catalog.find_asset(
        descriptor_path,
        preferred,
    )
    if descriptor_asset is None:
        raise AssertionError(f"Missing mesh descriptor: {descriptor_path}")
    _, descriptor_raw = repository.read_asset(
        int(descriptor_asset["id"]),
        max_size=256 << 20,
    )
    descriptor = parse_dbx(descriptor_raw)
    instance = descriptor.primary_instance
    guid = instance.attribute("guid") if instance is not None else None
    if not guid:
        raise AssertionError(f"Mesh descriptor has no GUID: {descriptor_path}")
    resolver = LevelResolver(repository, "MODULAR", preferred)
    definition = resolver.resolve_mesh(
        descriptor_path.removesuffix(".dbx") + "/" + guid
    )
    return definition, resolver.issues


def _audit_mesh(repository, mesh_data_path: str):
    mesh_set_path = _mesh_set_path(mesh_data_path)
    mesh_set_asset = repository.catalog.find_asset(mesh_set_path)
    if mesh_set_asset is None:
        raise AssertionError(f"Missing vehicle mesh set: {mesh_set_path}")
    preferred_archives = (str(mesh_set_asset["archive_path"]),)
    mesh_asset = repository.catalog.find_asset(
        mesh_data_path,
        preferred_archives,
    )
    if mesh_asset is None:
        raise AssertionError(f"Missing vehicle MeshData: {mesh_data_path}")
    _, raw = repository.read_asset(
        int(mesh_asset["id"]),
        max_size=1 << 30,
    )
    parsed = parse_meshdata(
        raw,
        mesh_asset["platform"],
        retain_pc_vehicle_uv_candidates=True,
    )
    definition, resolver_issues = _mesh_definition(
        repository,
        mesh_set_asset,
    )
    assignments = blender_addon._surface_assignments(parsed, definition)
    keys = tuple(
        dict.fromkeys(
            surface.database_key
            for surface in definition.surfaces
        )
    )
    materials, sources, unresolved_keys = _material_sources(
        repository,
        definition.surfaces,
        preferred_archives,
        mesh_set_path,
    )

    surfaces = []
    missing_routes = []
    colorless_opaque = []
    fallback_bindings = []
    for subset, (surface, match) in zip(parsed.subsets, assignments):
        if surface is None:
            surfaces.append(
                {
                    "subset": subset.name,
                    "match": match,
                    "error": "unresolved surface",
                }
            )
            continue
        shader_material = materials.get(surface.database_key.casefold())
        if shader_material is None:
            surfaces.append(
                {
                    "subset": subset.name,
                    "surface": surface.name,
                    "match": match,
                    "error": "unresolved material key",
                }
            )
            continue
        roles = blender_addon._surface_binding_roles(
            surface,
            shader_material.textures,
        )
        variant = blender_addon._shader_variant_for_subset(
            shader_material,
            subset.vertex_stride,
            subset.uv_offsets,
        )
        binding_records = []
        for binding, role in zip(shader_material.textures, roles):
            route = blender_addon._shader_binding_route(variant, binding)
            if route is not None and route.uv_offset not in subset.uv_offsets:
                missing_routes.append(
                    {
                        "mesh": mesh_set_path,
                        "subset": subset.name,
                        "surface": surface.name,
                        "slot": binding.slot,
                        "offset": route.uv_offset,
                        "available": list(subset.uv_offsets),
                    }
                )
            if route is None and role != "ENVIRONMENT":
                fallback_bindings.append(
                    {
                        "mesh": mesh_set_path,
                        "subset": subset.name,
                        "surface": surface.name,
                        "slot": binding.slot,
                        "role": role,
                    }
                )
            binding_records.append(
                {
                    "slot": binding.slot,
                    "source_index": binding.index,
                    "path": binding.path,
                    "parameters": list(binding.parameters),
                    "role": role,
                    "dimension": binding.dimension,
                    "uv_offset": (
                        route.uv_offset if route is not None else None
                    ),
                    "scale": (
                        list(route.scale) if route is not None else None
                    ),
                }
            )
        if (
            shader_material.textures
            and not {"COLOR", "DETAIL"}.intersection(roles)
            and not blender_addon._is_vehicle_glass_surface(surface)
        ):
            colorless_opaque.append(
                {
                    "mesh": mesh_set_path,
                    "subset": subset.name,
                    "surface": surface.name,
                    "roles": list(roles),
                }
            )
        surfaces.append(
            {
                "subset": subset.name,
                "surface": surface.name,
                "match": match,
                "shader_key": surface.database_key,
                "shader_source": sources.get(
                    surface.database_key.casefold(),
                    "",
                ),
                "variant": (
                    {
                        "vertex_shader": variant.vertex_shader_index,
                        "pixel_shader": variant.pixel_shader_index,
                        "layout_uv_offsets": (
                            list(variant.layout.uv_offsets)
                            if variant.layout is not None
                            else []
                        ),
                        "route_error": variant.route_error,
                    }
                    if variant is not None
                    else None
                ),
                "bindings": binding_records,
            }
        )
    return {
        "mesh_set": mesh_set_path,
        "mesh_data": mesh_data_path,
        "archive": str(mesh_set_asset["archive_path"]),
        "mesh_type": parsed.mesh_type,
        "subsets": len(parsed.subsets),
        "strides": sorted(
            {subset.vertex_stride for subset in parsed.subsets}
        ),
        "part_offsets": sorted(
            {
                subset.part_index_offset
                for subset in parsed.subsets
                if subset.part_index_offset is not None
            }
        ),
        "uv_declarations": sorted(
            {subset.uv_offsets for subset in parsed.subsets}
        ),
        "uv_sources": sorted(
            {subset.uv_source for subset in parsed.subsets}
        ),
        "resolver_issues": [
            {
                "code": issue.code,
                "reference": issue.reference,
                "message": issue.message,
            }
            for issue in resolver_issues
        ],
        "unresolved_keys": list(unresolved_keys),
        "missing_routes": missing_routes,
        "colorless_opaque": colorless_opaque,
        "fallback_bindings": fallback_bindings,
        "surfaces": surfaces,
    }


def main():
    arguments = _arguments()
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    repository = AssetRepository(arguments.iso, arguments.cache)
    try:
        mesh_paths = _vehicle_mesh_paths(repository)
        if len(mesh_paths) != EXPECTED_VEHICLE_LOD0_RESOURCES:
            raise AssertionError(
                f"Expected {EXPECTED_VEHICLE_LOD0_RESOURCES} unique vehicle "
                f"LOD0 resources, found {len(mesh_paths)}"
            )
        results = []
        failures = []
        source_limitations = []
        for index, mesh_path in enumerate(mesh_paths, 1):
            try:
                result = _audit_mesh(repository, mesh_path)
                results.append(result)
                source_issue_codes = {
                    "missing_surface_shaders",
                    "missing_shader_reference",
                }
                source_limitations.extend(
                    issue
                    for issue in result["resolver_issues"]
                    if issue["code"] in source_issue_codes
                )
                failures.extend(
                    issue
                    for issue in result["resolver_issues"]
                    if issue["code"] not in source_issue_codes
                )
                source_limitations.extend(
                    {
                        "code": "unresolved_shader_key",
                        "mesh": result["mesh_set"],
                        "shader_key": value,
                    }
                    for value in result["unresolved_keys"]
                )
                failures.extend(
                    {"code": "missing_shader_uv_layer", **value}
                    for value in result["missing_routes"]
                )
                unresolved_surfaces = [
                    {
                        "code": "unresolved_subset_surface",
                        "mesh": result["mesh_set"],
                        "subset": surface["subset"],
                    }
                    for surface in result["surfaces"]
                    if surface.get("error")
                ]
                if source_issue_codes.intersection(
                    issue["code"] for issue in result["resolver_issues"]
                ):
                    source_limitations.extend(unresolved_surfaces)
                else:
                    failures.extend(unresolved_surfaces)
            except Exception as exc:
                failures.append(
                    {
                        "code": "vehicle_audit_failed",
                        "mesh_data": mesh_path,
                        "message": str(exc),
                    }
                )
            print(
                f"VEHICLE_CATALOG_AUDIT_PROGRESS {index}/"
                f"{len(mesh_paths)} {mesh_path}",
                flush=True,
            )
        summary = {
            "resources": len(results),
            "mesh_types": dict(
                sorted(Counter(item["mesh_type"] for item in results).items())
            ),
            "subsets": sum(item["subsets"] for item in results),
            "surfaces": sum(len(item["surfaces"]) for item in results),
            "colorless_opaque": sum(
                len(item["colorless_opaque"]) for item in results
            ),
            "fallback_bindings": sum(
                len(item["fallback_bindings"]) for item in results
            ),
            "source_limitations": len(source_limitations),
            "failures": len(failures),
        }
        report = {
            "summary": summary,
            "failures": failures,
            "source_limitations": source_limitations,
            "vehicles": results,
        }
        output.write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(
            "VEHICLE_CATALOG_AUDIT "
            + json.dumps(summary, sort_keys=True),
            flush=True,
        )
        if failures:
            raise AssertionError(
                f"Vehicle catalog audit found {len(failures)} failure(s)"
            )
    finally:
        repository.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        os._exit(1)
