# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
import traceback
from collections import Counter
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
if "--installed" in sys.argv:
    package = (
        "bl_ext.user_default.bc2_frostbite_importer."
        "bc2_frostbite_importer"
    )
    blender_addon = importlib.import_module(package + ".blender_addon")
    level_module = importlib.import_module(package + ".core.level")
    mesh_module = importlib.import_module(package + ".core.mesh")
    repository_module = importlib.import_module(
        package + ".core.repository"
    )
    terrain_module = importlib.import_module(package + ".core.terrain")
else:
    sys.path.insert(0, str(ROOT))
    from bc2_frostbite_importer import blender_addon
    from bc2_frostbite_importer.core import (
        level as level_module,
        mesh as mesh_module,
        repository as repository_module,
        terrain as terrain_module,
    )


AssetRepository = repository_module.AssetRepository
LevelResolver = level_module.LevelResolver
discover_pc_levels = level_module.discover_pc_levels
index_pc_level_archives = level_module.index_pc_level_archives
parse_meshdata = mesh_module.parse_meshdata
parse_material_map = terrain_module.parse_material_map

EXPECTED_MAPS = 33


def _arguments():
    values = sys.argv
    values = values[values.index("--") + 1 :] if "--" in values else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--installed", action="store_true")
    return parser.parse_args(values)


def _shader_database(repository, level_scene, preferred_archives):
    if not level_scene.shader_database_path:
        raise AssertionError(
            f"{level_scene.name} has no resolved shader database"
        )
    asset = repository.catalog.find_asset(
        level_scene.shader_database_path,
        preferred_archives,
    )
    if asset is None:
        raise AssertionError(
            f"Shader database is not indexed: "
            f"{level_scene.shader_database_path}"
        )
    if not repository.catalog.shader_database_recorded(int(asset["id"])):
        repository.read_shader_database(
            int(asset["id"]),
            material_keys=(),
        )
    material_keys = []
    shader_partitions = []
    for definition in level_scene.meshes:
        for surface in definition.surfaces:
            keys, partitions = blender_addon._shader_lookup_terms(
                surface
            )
            material_keys.extend(keys)
            shader_partitions.extend(partitions)
    terrain_name = PurePosixPath(
        level_scene.terrain_material_map_path or ""
    ).parent.name.casefold()
    terrain_prefix = (
        f"terrains/{terrain_name}/{terrain_name}/"
        if terrain_name
        else ""
    )
    terrain_keys = tuple(
        key
        for key in repository.catalog.shader_database_material_keys(
            int(asset["id"])
        )
        if terrain_prefix
        and key.casefold().startswith(terrain_prefix)
    )
    material_keys.extend(terrain_keys)
    _, database = repository.read_shader_database(
        int(asset["id"]),
        material_keys=tuple(dict.fromkeys(material_keys)),
        shader_partitions=tuple(dict.fromkeys(shader_partitions)),
    )
    return asset, database, terrain_keys


def _parsed_mesh(
    repository,
    definition,
    preferred_archives,
    cache,
    shader_uv_offsets,
):
    asset = repository.catalog.find_asset(
        definition.mesh_data_path,
        preferred_archives,
    )
    if asset is None:
        raise AssertionError(
            f"MeshData is not indexed: {definition.mesh_data_path}"
        )
    cache_key = int(asset["id"])
    parsed = cache.get(cache_key)
    if parsed is None:
        _, raw = repository.read_asset(
            cache_key,
            max_size=1 << 30,
        )
        parsed = parse_meshdata(
            raw,
            asset["platform"],
            retain_pc_vehicle_uv_candidates=(
                definition.mesh_data_path.casefold().startswith(
                    "objects/vehicles/"
                )
            ),
            pc_declared_uv_offsets_by_stride=shader_uv_offsets.get(
                definition.mesh_data_path.casefold(),
            ),
        )
        cache[cache_key] = parsed
    return asset, parsed


def _is_layered_static(shader_material, roles):
    return any(
        role == "OCCLUSION"
        and any(
            marker in PurePosixPath(binding.path).name.casefold()
            for marker in ("ocrgb", "oc_rgb", "oc-rgb")
        )
        for binding, role in zip(shader_material.textures, roles)
    )


def _binding_record(
    repository,
    preferred_archives,
    surface,
    subset,
    binding,
    role,
    route,
    layered_static,
):
    routed_uv_map = (
        blender_addon._uv_map_for_offset(
            subset.uv_offsets,
            route.uv_offset,
        )
        if route is not None
        else None
    )
    fallback_uv_map = blender_addon._surface_binding_uv_map(
        surface,
        binding,
        role,
        layered_static=layered_static,
    )
    if route is None:
        route_status = "semantic_fallback"
    elif routed_uv_map is None:
        route_status = "unavailable_offset"
    elif any(
        value is None
        for value in (*route.scale, *route.offset)
    ):
        route_status = "dynamic_transform"
    else:
        route_status = "physical"
    texture_asset = blender_addon._texture_asset(
        repository,
        binding.path,
        preferred_archives,
    )
    return {
        "slot": binding.slot,
        "source_index": binding.index,
        "path": binding.path,
        "parameters": list(binding.parameters),
        "dimension": binding.dimension,
        "role": role,
        "texture_source": (
            (
                f"{texture_asset['archive_path']}|"
                f"{texture_asset['path']}"
            )
            if texture_asset is not None
            else ""
        ),
        "route_status": route_status,
        "uv_map": routed_uv_map or fallback_uv_map,
        "route": (
            {
                "uv_offset": route.uv_offset,
                "scale": list(route.scale),
                "offset": list(route.offset),
            }
            if route is not None
            else None
        ),
        "fallback_scale": list(
            blender_addon._surface_binding_uv_scale(
                surface,
                binding,
            )
        ),
    }


def _surface_record(
    repository,
    preferred_archives,
    shader_database,
    subset,
    surface,
    subset_match,
):
    record = {
        "subset": subset.name,
        "surface": surface.name if surface is not None else "",
        "surface_match": subset_match,
        "shader_key": (
            surface.database_key if surface is not None else ""
        ),
        "shader_partition": (
            surface.shader_partition if surface is not None else ""
        ),
        "key_match": "",
        "resolved_shader_key": "",
        "status": "unresolved_surface",
        "variant": None,
        "bindings": [],
    }
    if surface is None:
        return record
    shader_material, resolved_key, key_match = (
        blender_addon._find_shader_material(
            shader_database,
            surface.database_key,
            surface.shader_partition,
        )
    )
    record["key_match"] = key_match
    record["resolved_shader_key"] = resolved_key
    if shader_material is None:
        record["status"] = "missing_shader_key"
        return record
    record["status"] = "resolved"
    roles = blender_addon._surface_binding_roles(
        surface,
        shader_material.textures,
    )
    variant = blender_addon._shader_variant_for_subset(
        shader_material,
        subset.vertex_stride,
        subset.uv_offsets,
    )
    if variant is not None:
        record["variant"] = {
            "table_index": variant.table_index,
            "vertex_shader": variant.vertex_shader_index,
            "pixel_shader": variant.pixel_shader_index,
            "layout_reference": (
                variant.layout.reference.hex()
                if variant.layout is not None
                else None
            ),
            "layout_stride": (
                variant.layout.stride
                if variant.layout is not None
                else None
            ),
            "layout_uv_offsets": (
                list(variant.layout.uv_offsets)
                if variant.layout is not None
                else []
            ),
            "route_error": variant.route_error,
        }
    layered_static = _is_layered_static(shader_material, roles)
    record["bindings"] = [
        _binding_record(
            repository,
            preferred_archives,
            surface,
            subset,
            binding,
            role,
            blender_addon._shader_binding_route(variant, binding),
            layered_static,
        )
        for binding, role in zip(shader_material.textures, roles)
    ]
    return record


def _definition_record(
    repository,
    preferred_archives,
    shader_database,
    definition,
    mesh_cache,
    shader_uv_offsets,
):
    asset, parsed = _parsed_mesh(
        repository,
        definition,
        preferred_archives,
        mesh_cache,
        shader_uv_offsets,
    )
    assignments = blender_addon._surface_assignments(
        parsed,
        definition,
    )
    return {
        "key": definition.key,
        "reference": definition.reference,
        "name": definition.name,
        "mesh_data": definition.mesh_data_path,
        "mesh_source": (
            f"{asset['archive_path']}|{asset['path']}"
        ),
        "mesh_type": parsed.mesh_type,
        "subsets": len(parsed.subsets),
        "source_surfaces": len(definition.surfaces),
        "shader_set_reference": (
            definition.shader_set_reference or ""
        ),
        "materials": [
            _surface_record(
                repository,
                preferred_archives,
                shader_database,
                subset,
                surface,
                subset_match,
            )
            for subset, (surface, subset_match) in zip(
                parsed.subsets,
                assignments,
            )
        ],
    }


def _terrain_record(
    repository,
    level_scene,
    preferred_archives,
    shader_database,
    terrain_keys,
):
    if not level_scene.terrain_material_map_path:
        return {
            "status": "missing",
            "path": "",
            "side": 0,
            "endian": "",
            "shader_keys": len(terrain_keys),
            "candidate_textures": 0,
            "layers": [],
        }
    asset = repository.catalog.find_asset(
        level_scene.terrain_material_map_path,
        preferred_archives,
    )
    if asset is None:
        raise AssertionError(
            "Terrain material map is not indexed: "
            f"{level_scene.terrain_material_map_path}"
        )
    _, raw = repository.read_asset(
        int(asset["id"]),
        max_size=1 << 30,
    )
    material_map = parse_material_map(
        raw,
        platform=asset["platform"],
    )
    candidates = blender_addon._terrain_shader_texture_paths(
        shader_database,
        str(asset["path"]),
    )
    layers = []
    for index, name in enumerate(material_map.names):
        texture_path = blender_addon._terrain_layer_texture(
            name,
            candidates,
        )
        texture_asset = (
            blender_addon._texture_asset(
                repository,
                texture_path,
                preferred_archives,
            )
            if texture_path is not None
            else None
        )
        layers.append(
            {
                "index": index,
                "name": name,
                "texture": texture_path or "",
                "texture_source": (
                    (
                        f"{texture_asset['archive_path']}|"
                        f"{texture_asset['path']}"
                    )
                    if texture_asset is not None
                    else ""
                ),
            }
        )
    return {
        "status": "resolved",
        "path": str(asset["path"]),
        "side": material_map.side,
        "endian": material_map.endian,
        "shader_keys": len(terrain_keys),
        "candidate_textures": len(candidates),
        "candidates": sorted(candidates),
        "layers": layers,
    }


def _map_summary(level_scene, definitions, terrain):
    materials = [
        material
        for definition in definitions
        for material in definition["materials"]
    ]
    bindings = [
        binding
        for material in materials
        for binding in material["bindings"]
    ]
    return {
        "placements": len(level_scene.placements),
        "mesh_variants": len(definitions),
        "unique_mesh_data": len(
            {
                definition["mesh_data"].casefold()
                for definition in definitions
            }
        ),
        "subsets": sum(
            definition["subsets"] for definition in definitions
        ),
        "source_surfaces": sum(
            definition["source_surfaces"]
            for definition in definitions
        ),
        "material_status": dict(
            Counter(material["status"] for material in materials)
        ),
        "surface_matches": dict(
            Counter(
                material["surface_match"] for material in materials
            )
        ),
        "shader_key_matches": dict(
            Counter(
                material["key_match"]
                for material in materials
                if material["key_match"]
            )
        ),
        "bindings": len(bindings),
        "binding_roles": dict(
            Counter(binding["role"] for binding in bindings)
        ),
        "route_status": dict(
            Counter(
                binding["route_status"] for binding in bindings
            )
        ),
        "missing_textures": sum(
            not binding["texture_source"] for binding in bindings
        ),
        "variant_route_errors": sum(
            bool(material["variant"])
            and bool(material["variant"]["route_error"])
            for material in materials
        ),
        "terrain_layers": len(terrain["layers"]),
        "terrain_textures": sum(
            bool(layer["texture_source"])
            for layer in terrain["layers"]
        ),
        "heightfields": len(level_scene.heightfield_paths),
        "water_meshes": int(
            level_scene.water_mesh_path is not None
        ),
        "resolver_issues": dict(
            Counter(issue.code for issue in level_scene.issues)
        ),
    }


def _audit_map(repository, level_name, mesh_cache):
    started = time.perf_counter()
    archives = index_pc_level_archives(
        repository,
        level_name,
    )
    resolver = LevelResolver(
        repository,
        level_name,
        archives,
    )
    level_scene = resolver.resolve()
    shader_asset, shader_database, terrain_keys = _shader_database(
        repository,
        level_scene,
        archives,
    )
    shader_uv_offsets = blender_addon._shader_uv_offsets_by_mesh(
        level_scene.meshes,
        shader_database,
    )
    definitions = [
        _definition_record(
            repository,
            archives,
            shader_database,
            definition,
            mesh_cache,
            shader_uv_offsets,
        )
        for definition in level_scene.meshes
    ]
    terrain = _terrain_record(
        repository,
        level_scene,
        archives,
        shader_database,
        terrain_keys,
    )
    summary = _map_summary(level_scene, definitions, terrain)
    return {
        "level": level_scene.name,
        "archives": list(archives),
        "shader_database": (
            f"{shader_asset['archive_path']}|{shader_asset['path']}"
        ),
        "elapsed_seconds": round(
            time.perf_counter() - started,
            3,
        ),
        "summary": summary,
        "resolver_issues": [
            {
                "code": issue.code,
                "message": issue.message,
                "reference": issue.reference or "",
            }
            for issue in level_scene.issues
        ],
        "terrain": terrain,
        "definitions": definitions,
    }


def _aggregate(results, errors):
    summary = {
        "maps": len(results),
        "errors": len(errors),
        "placements": sum(
            result["summary"]["placements"] for result in results
        ),
        "mesh_variants": sum(
            result["summary"]["mesh_variants"] for result in results
        ),
        "unique_mesh_data": len(
            {
                definition["mesh_data"].casefold()
                for result in results
                for definition in result["definitions"]
            }
        ),
        "subsets": sum(
            result["summary"]["subsets"] for result in results
        ),
        "source_surfaces": sum(
            result["summary"]["source_surfaces"]
            for result in results
        ),
        "bindings": sum(
            result["summary"]["bindings"] for result in results
        ),
        "missing_textures": sum(
            result["summary"]["missing_textures"]
            for result in results
        ),
        "variant_route_errors": sum(
            result["summary"]["variant_route_errors"]
            for result in results
        ),
        "terrain_layers": sum(
            result["summary"]["terrain_layers"]
            for result in results
        ),
        "terrain_textures": sum(
            result["summary"]["terrain_textures"]
            for result in results
        ),
        "heightfields": sum(
            result["summary"]["heightfields"]
            for result in results
        ),
        "water_meshes": sum(
            result["summary"]["water_meshes"]
            for result in results
        ),
    }
    for field in (
        "material_status",
        "surface_matches",
        "shader_key_matches",
        "binding_roles",
        "route_status",
        "resolver_issues",
    ):
        counter = Counter()
        for result in results:
            counter.update(result["summary"][field])
        summary[field] = dict(counter)
    return summary


def _validation_failures(summary, results):
    failures = []
    subset_count = summary["subsets"]
    if summary["material_status"] != {"resolved": subset_count}:
        failures.append(
            {
                "code": "unresolved_subset_material",
                "statuses": summary["material_status"],
            }
        )
    if summary["shader_key_matches"] != {"exact": subset_count}:
        failures.append(
            {
                "code": "nonexact_shader_key",
                "matches": summary["shader_key_matches"],
            }
        )
    if sum(summary["surface_matches"].values()) != subset_count:
        failures.append(
            {
                "code": "unmatched_subset_surface",
                "matches": summary["surface_matches"],
            }
        )
    if summary["missing_textures"]:
        failures.append(
            {
                "code": "missing_indexed_texture",
                "count": summary["missing_textures"],
            }
        )
    if summary["variant_route_errors"]:
        failures.append(
            {
                "code": "shader_route_analysis_error",
                "count": summary["variant_route_errors"],
            }
        )
    if sum(summary["route_status"].values()) != summary["bindings"]:
        failures.append(
            {
                "code": "unclassified_shader_binding",
                "bindings": summary["bindings"],
                "routes": summary["route_status"],
            }
        )
    for result in results:
        terrain = result["terrain"]
        if (
            terrain["status"] != "resolved"
            or not terrain["layers"]
            or not result["summary"]["terrain_textures"]
        ):
            failures.append(
                {
                    "code": "unresolved_terrain_materials",
                    "level": result["level"],
                    "status": terrain["status"],
                    "layers": len(terrain["layers"]),
                    "textures": result["summary"]["terrain_textures"],
                }
            )
    return failures


def main():
    arguments = _arguments()
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    repository = AssetRepository(arguments.iso, arguments.cache)
    try:
        levels = discover_pc_levels(
            row["path"]
            for row in repository.catalog.archives(limit=2000)
        )
        if len(levels) != EXPECTED_MAPS:
            raise AssertionError(
                f"Expected {EXPECTED_MAPS} PC maps, found {len(levels)}"
            )
        results = []
        errors = []
        mesh_cache = {}
        for index, level_name in enumerate(levels, start=1):
            try:
                results.append(
                    _audit_map(
                        repository,
                        level_name,
                        mesh_cache,
                    )
                )
            except Exception as exc:
                errors.append(
                    {
                        "level": level_name,
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
            print(
                f"MAP_MATERIAL_AUDIT_PROGRESS {index}/{len(levels)} "
                f"{level_name}",
                flush=True,
            )
        summary = _aggregate(results, errors)
        validation_failures = _validation_failures(summary, results)
        report = {
            "summary": summary,
            "errors": errors,
            "validation_failures": validation_failures,
            "maps": results,
        }
        output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(
            "MAP_MATERIAL_CATALOG_AUDIT "
            + json.dumps(
                {
                    "output": str(output),
                    "summary": summary,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        if errors or validation_failures:
            raise AssertionError(
                "Map material audit found "
                f"{len(errors)} error(s) and "
                f"{len(validation_failures)} validation failure(s)"
            )
    finally:
        repository.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        os._exit(1)
