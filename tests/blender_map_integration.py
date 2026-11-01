# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
import traceback
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import bpy
from mathutils import Vector


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bc2_frostbite_importer import blender_addon
from bc2_frostbite_importer.core.level import (
    LevelResolver,
    index_pc_level_archives,
)
from bc2_frostbite_importer.core.repository import AssetRepository


def _arguments():
    arguments = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--level", default="MP_001")
    parser.add_argument("--terrain-resolution", type=int, default=1024)
    parser.add_argument("--no-materials", action="store_true")
    parser.add_argument("--pack-images", action="store_true")
    return parser.parse_args(arguments)


def _progress(prefix):
    last = [-1]

    def report(current, total, label):
        percentage = int(current / max(1, total) * 100)
        if percentage != last[0]:
            print(f"{prefix} {percentage:3d}% {label}", flush=True)
            last[0] = percentage

    return report


def _scene_progress(fraction, label):
    print(f"CREATE {fraction * 100:5.1f}% {label}", flush=True)


def _resolve_progress(fraction, label):
    print(f"RESOLVE {fraction * 100:5.1f}% {label}", flush=True)


def _validate_resolved_model(level_scene):
    if level_scene.name != "MP_001":
        return
    checks = {
        "placements": (len(level_scene.placements), 5380),
        "mesh variants": (len(level_scene.meshes), 211),
        "unique mesh resources": (
            len(
                {
                    mesh.mesh_data_path.casefold()
                    for mesh in level_scene.meshes
                }
            ),
            178,
        ),
        "heightfields": (len(level_scene.heightfield_paths), 13),
        "water meshes": (int(level_scene.water_mesh_path is not None), 1),
    }
    failures = [
        f"{name}: {actual:,} != {expected:,}"
        for name, (actual, expected) in checks.items()
        if actual != expected
    ]
    issue_codes = [issue.code for issue in level_scene.issues]
    if issue_codes != (
        ["non_visual_reference"] * 24
        + ["unsupported_terrain_visual_decals"]
    ):
        failures.append(
            "issues: expected 24 non_visual_reference entries and one "
            "unsupported_terrain_visual_decals entry, got "
            + json.dumps(issue_codes)
        )
    if level_scene.terrain_material_map_path is None:
        failures.append("terrain material map was not resolved")
    if failures:
        raise AssertionError("MP_001 model mismatch: " + "; ".join(failures))


def _validate_saved_scene(root_name, expected):
    root = bpy.data.collections.get(root_name)
    if root is None:
        raise AssertionError(f"Saved map collection is missing: {root_name}")
    placements = [
        obj
        for collection in root.children_recursive
        for obj in collection.objects
        if "bc2_mesh_key" in obj
    ]
    unique_meshes = {
        obj.data.as_pointer(): obj.data
        for obj in placements
        if obj.type == "MESH"
    }
    if len(placements) != expected["placements"]:
        raise AssertionError(
            f"Saved placement count is {len(placements):,}, "
            f"expected {expected['placements']:,}"
        )
    if len(unique_meshes) != expected["mesh_variants"]:
        raise AssertionError(
            f"Saved linked mesh count is {len(unique_meshes):,}, "
            f"expected {expected['mesh_variants']:,}"
        )
    if len(unique_meshes) >= len(placements):
        raise AssertionError("Map placements are not using linked mesh data")
    if int(root["bc2_heightfield_count"]) != expected["heightfields"]:
        raise AssertionError("Saved terrain tile count does not match")
    if expected.get("terrain_material_maps") != 1:
        raise AssertionError("Resolved terrain material map was not counted")
    if int(root.get("bc2_terrain_material_map_side", 0)) != 4096:
        raise AssertionError("Terrain material-map metadata is missing")
    if not bpy.data.texts.get(root["bc2_diagnostics_text"]):
        raise AssertionError("Saved diagnostics text is missing")
    collection_counts = {
        collection.name: len(collection.objects)
        for collection in root.children
    }
    expected_collections = {
        "Direct": 2461,
        "Forest": 878,
        "Middlegrowth": 390,
        "Rigid Group": 97,
        "Static Group": 1554,
        "Terrain": 13,
        "Water": 1,
        "Diagnostics": 1,
    }
    if expected["level"] == "MP_001":
        for name, count in expected_collections.items():
            if collection_counts.get(name) != count:
                raise AssertionError(
                    f"{name} has {collection_counts.get(name)} objects, "
                    f"expected {count}"
                )

    diagnostics = json.loads(
        bpy.data.texts[root["bc2_diagnostics_text"]].as_string()
    )
    diagnostic_counts = collections.Counter(
        issue["code"] for issue in diagnostics["issues"]
    )
    unexpected_codes = set(diagnostic_counts) - {
        "missing_shader_key",
        "non_visual_reference",
        "unsupported_terrain_visual_decals",
        "unresolved_subset_surface",
    }
    if unexpected_codes:
        raise AssertionError(
            f"Unexpected saved diagnostics: {sorted(unexpected_codes)}"
        )

    surface_matches = collections.Counter()
    for mesh in unique_meshes.values():
        for assignment in json.loads(mesh["bc2_surface_assignments"]):
            surface_matches[assignment["match"]] += 1

    source_images = [
        image for image in bpy.data.images if "bc2_asset_path" in image
    ]
    unavailable_images = [
        image.name
        for image in source_images
        if image.source != "FILE"
        or not Path(bpy.path.abspath(image.filepath)).is_file()
    ]
    if unavailable_images:
        raise AssertionError(
            f"Images are not available as native files: {unavailable_images[:5]}"
        )

    def world_bounds(objects):
        points = [
            obj.matrix_world @ Vector(corner)
            for obj in objects
            for corner in obj.bound_box
        ]
        return [
            [min(point[axis] for point in points), max(point[axis] for point in points)]
            for axis in range(3)
        ]

    terrain_bounds = world_bounds(root.children["Terrain"].objects)
    if (
        terrain_bounds[0][0] > -1000.0
        or terrain_bounds[0][1] < 1000.0
        or terrain_bounds[1][0] > -1000.0
        or terrain_bounds[1][1] < 1000.0
    ):
        raise AssertionError(f"Terrain footprint is incomplete: {terrain_bounds}")
    terrain_regions = [
        (obj, world_bounds((obj,)))
        for obj in root.children["Terrain"].objects
    ]
    central_terrain = bpy.data.objects.get("MP_001.Heightfield-000")
    if central_terrain is None:
        raise AssertionError("MP_001.Heightfield-000 is missing")
    if len(central_terrain.data.vertices) != 1_048_576:
        raise AssertionError(
            "MP_001.Heightfield-000 is not full resolution: "
            f"{len(central_terrain.data.vertices):,} vertices"
        )
    central_heights = [
        vertex.co.z for vertex in central_terrain.data.vertices
    ]
    central_height_range = (
        min(central_heights),
        max(central_heights),
    )
    if not (
        109.2 < central_height_range[0] < 109.4
        and 276.4 < central_height_range[1] < 276.6
    ):
        raise AssertionError(
            "MP_001.Heightfield-000 uses the wrong sample layout: "
            f"{central_height_range}"
        )
    if (
        tuple(central_terrain.data.polygons[0].vertices)
        != (0, 1024, 1025)
        or tuple(central_terrain.data.polygons[1].vertices)
        != (0, 1025, 1)
    ):
        raise AssertionError(
            "MP_001.Heightfield-000 uses the wrong quad diagonal"
        )

    def terrain_height(x, y):
        for obj, bounds in terrain_regions:
            if (
                bounds[0][0] - 0.01 <= x <= bounds[0][1] + 0.01
                and bounds[1][0] - 0.01 <= y <= bounds[1][1] + 0.01
            ):
                inverse = obj.matrix_world.inverted()
                hit, location, _, _ = obj.ray_cast(
                    inverse @ Vector((x, y, 1_000.0)),
                    inverse.to_3x3() @ Vector((0.0, 0.0, -1.0)),
                    distance=2_000.0,
                )
                if hit:
                    return (obj.matrix_world @ location).z
        return None

    forest_contacts = []
    for obj in root.children["Forest"].objects:
        location = obj.matrix_world.translation
        height = terrain_height(location.x, location.y)
        if height is not None:
            forest_contacts.append(location.z - height)
    forest_contacts.sort()
    median_forest_contact = forest_contacts[len(forest_contacts) // 2]
    minimum_forest_contacts = int(
        expected_collections["Forest"] * 0.95
    )
    if (
        len(forest_contacts) < minimum_forest_contacts
        or not -15.0 < median_forest_contact < 15.0
    ):
        raise AssertionError(
            "Terrain is not in the placement coordinate frame: "
            f"{len(forest_contacts)} contacts, "
            f"median offset {median_forest_contact:.3f}"
        )

    water_objects = root.children["Water"].objects
    water_bounds = world_bounds(water_objects)
    if not 120.0 < water_bounds[2][0] < water_bounds[2][1] < 140.0:
        raise AssertionError(f"Water elevation is unexpected: {water_bounds}")
    if expected["level"] == "MP_001":
        if len(water_objects) != 1:
            raise AssertionError(
                f"Expected one MP_001 WaterMesh, got {len(water_objects)}"
            )
        water_mesh = water_objects[0].data
        if (
            len(water_mesh.vertices) != 569
            or len(water_mesh.polygons) != 1_013
        ):
            raise AssertionError(
                "MP_001 WaterMesh topology is unexpected: "
                f"{len(water_mesh.vertices)} vertices, "
                f"{len(water_mesh.polygons)} polygons"
            )
        referenced_vertices = {
            loop.vertex_index for loop in water_mesh.loops
        }
        if len(referenced_vertices) != len(water_mesh.vertices):
            raise AssertionError(
                "MP_001 WaterMesh did not consume its complete uint32 "
                f"topology: {len(referenced_vertices)} of "
                f"{len(water_mesh.vertices)} vertices referenced"
            )

    terrain_materials = [
        material
        for material in bpy.data.materials
        if material.get("bc2_material_status")
        == "resolved terrain material map"
    ]
    if len(terrain_materials) != 1:
        raise AssertionError(
            "Expected one resolved terrain material, got "
            f"{len(terrain_materials)}"
        )
    if any(
        not obj.data.materials
        or obj.data.materials[0] is not terrain_materials[0]
        for obj in root.children["Terrain"].objects
    ):
        raise AssertionError("Terrain tiles do not share the resolved material")
    if any(
        int(obj.get("bc2_terrain_material_map_side", 0)) != 4096
        for obj in root.children["Terrain"].objects
    ):
        raise AssertionError("Terrain tile material-map metadata is missing")
    material_map_nodes = [
        node
        for node in terrain_materials[0].node_tree.nodes
        if node.type == "TEX_IMAGE"
        and node.label == "Terrain material indices"
    ]
    if (
        len(material_map_nodes) != 1
        or material_map_nodes[0].interpolation != "Closest"
        or material_map_nodes[0].image.source != "FILE"
    ):
        raise AssertionError("Terrain material-index image is not file-backed")

    material_status = collections.Counter(
        material.get("bc2_material_status", "<none>")
        for material in bpy.data.materials
    )
    resolved_materials = [
        material
        for material in bpy.data.materials
        if material.get("bc2_material_status") == "resolved material graph"
    ]
    base_color_graphs = sum(
        any(
            node.type == "BSDF_PRINCIPLED"
            and node.inputs["Base Color"].is_linked
            for node in material.node_tree.nodes
        )
        for material in resolved_materials
    )
    if expected["level"] == "MP_001" and base_color_graphs < 395:
        raise AssertionError(
            "Expected at least 395 resolved Base Color graphs, got "
            f"{base_color_graphs}"
        )

    targets = {}
    for enumeration in (4419, 4808, 5777):
        target = next(
            (
                obj
                for obj in placements
                if obj.get("bc2_enumeration") == enumeration
            ),
            None,
        )
        if target is None:
            raise AssertionError(
                f"Placement enumeration {enumeration} is missing"
            )
        transform = target["bc2_transform"]
        expected_location = Vector(
            (transform[12], -transform[14], transform[13])
        )
        if (target.location - expected_location).length > 1e-4:
            raise AssertionError(
                f"Placement {enumeration} uses a reflected translation"
            )
        determinant = target.matrix_world.to_3x3().determinant()
        if determinant <= 0.0:
            raise AssertionError(
                f"Placement {enumeration} is reflected: {determinant}"
            )
        height = terrain_height(target.location.x, target.location.y)
        if height is None or abs(target.location.z - height) >= 1.0:
            raise AssertionError(
                f"Placement {enumeration} misses terrain: "
                f"object={target.location.z:.3f}, terrain={height}"
            )
        targets[enumeration] = {
            "name": target.name,
            "location": tuple(round(value, 4) for value in target.location),
            "terrain_offset": round(target.location.z - height, 4),
            "determinant": round(determinant, 6),
        }

    for enumeration in (4419, 4808):
        target = next(
            obj
            for obj in placements
            if obj.get("bc2_enumeration") == enumeration
        )
        uv_offsets = json.loads(
            target.data.get("bc2_uv_offsets", "[]")
        )
        if not uv_offsets or any(
            tuple(offsets) != (28, 36, 32, 24)
            for offsets in uv_offsets
        ):
            raise AssertionError(
                f"Placement {enumeration} has incorrect UV offsets: "
                f"{uv_offsets}"
            )
        if [layer.name for layer in target.data.uv_layers] != [
            "UVMap",
            "UV1",
            "UV2",
            "UV3",
        ]:
            raise AssertionError(f"Placement {enumeration} is missing UVs")
        decal_bindings = []
        for slot in target.material_slots:
            bindings = json.loads(
                slot.material.get("bc2_texture_bindings", "[]")
            )
            slot_decals = [
                binding
                for binding in bindings
                if "decal_atlas" in binding["path"].casefold()
            ]
            decal_bindings.extend(slot_decals)
            if slot_decals and not slot.material.get(
                "bc2_color_model",
                "",
            ).startswith("OcRGB-controlled"):
                raise AssertionError(
                    f"Placement {enumeration} samples its decal atlas "
                    "as primary color"
                )
        if not decal_bindings or any(
            binding["role"] != "DECAL"
            for binding in decal_bindings
        ):
            raise AssertionError(
                f"Placement {enumeration} misclassifies its decal atlas"
            )

    reported = {
        4497: {
            "offsets": (28, 36, 32, 24),
            "routes": {
                "shipsystem_aftfront_oc_rgb": "UVMap",
                "dt_rust_001": "UV1",
                "dt_dirt_002": "UV1",
            },
        },
        4275: {
            "offsets": (28, 36, 32, 24),
            "minimum_detail_span": 20.0,
            "routes": {
                "harbour_ocrgb": "UVMap",
                "dt_concretereinforced": "UV1",
                "dt_concreterough": "UV1",
                "dt_concreteedgecracks": "UV2",
            },
            "sizes": {
                "harbour_ocrgb": (512, 512),
                "dt_concretereinforced": (256, 256),
            },
        },
        3707: {
            "offsets": (24, 28, 32, 40),
            "routes": {
                "truck_c": "UV1",
                "dirtmaskrgb": "UV2",
            },
            "sizes": {"truck_c": (1024, 1024)},
        },
        6139: {
            "offsets": (28, 36, 32, 24),
            "minimum_detail_span": 5.0,
            "routes": {
                "pilebricks_t02v01_ocrgb": "UVMap",
                "dt_largebrick_001": "UV1",
                "nm_largebrick_001": "UV1",
            },
            "sizes": {
                "pilebricks_t02v01_ocrgb": (128, 128),
                "dt_largebrick_001": (256, 256),
            },
        },
        2220: {
            "offsets": (28, 36, 32, 24),
            "minimum_detail_span": 2.0,
            "routes": {
                "dt_wirefence_001": "UV1",
                "dt_razorwire": "UV1",
            },
            "scales": {
                "dt_wirefence_001": (15.0, 15.0),
                "dt_razorwire": (10.0, 10.0),
            },
            "sizes": {
                "dt_wirefence_001": (256, 256),
                "dt_razorwire": (128, 128),
            },
        },
    }
    for enumeration, requirement in reported.items():
        target = next(
            (
                obj
                for obj in placements
                if obj.get("bc2_enumeration") == enumeration
            ),
            None,
        )
        if target is None:
            raise AssertionError(
                f"Reported placement enumeration {enumeration} is missing"
            )
        uv_offsets = json.loads(
            target.data.get("bc2_uv_offsets", "[]")
        )
        expected_offsets = requirement["offsets"]
        if not uv_offsets or any(
            tuple(offsets) != expected_offsets
            for offsets in uv_offsets
        ):
            raise AssertionError(
                f"Placement {enumeration} has incorrect declared UVs: "
                f"{uv_offsets}"
            )
        minimum_span = requirement.get("minimum_detail_span")
        if minimum_span is not None:
            detail_layer = target.data.uv_layers.get("UV1")
            if detail_layer is None:
                raise AssertionError(
                    f"Placement {enumeration} is missing detail UV1"
                )
            coordinates = [
                coordinate
                for value in detail_layer.data
                for coordinate in value.uv
            ]
            span = max(coordinates) - min(coordinates)
            if span < minimum_span:
                raise AssertionError(
                    f"Placement {enumeration} detail UV span is too small: "
                    f"{span:.3f}"
                )
        observed_routes = {}
        observed_scales = {}
        observed_sizes = {}
        for slot in target.material_slots:
            material = slot.material
            shader = next(
                (
                    node
                    for node in material.node_tree.nodes
                    if node.type == "BSDF_PRINCIPLED"
                ),
                None,
            )
            if shader is None or not shader.inputs["Base Color"].is_linked:
                raise AssertionError(
                    f"Placement {enumeration} has an unlit material"
                )
            for node in material.node_tree.nodes:
                if node.type != "TEX_IMAGE" or "bc2_asset_path" not in node:
                    continue
                stem = PurePosixPath(
                    node["bc2_asset_path"]
                ).name.casefold().removesuffix(".res")
                observed_routes[stem] = node.get("bc2_uv_map")
                observed_scales[stem] = tuple(
                    node.get("bc2_uv_scale", ())
                )
                observed_sizes[stem] = tuple(node.image.size)
        for stem, uv_map in requirement["routes"].items():
            if observed_routes.get(stem) != uv_map:
                raise AssertionError(
                    f"Placement {enumeration} routes {stem} through "
                    f"{observed_routes.get(stem)!r}, expected {uv_map}"
                )
        for stem, scale in requirement.get("scales", {}).items():
            if observed_scales.get(stem) != scale:
                raise AssertionError(
                    f"Placement {enumeration} scales {stem} by "
                    f"{observed_scales.get(stem)}, expected {scale}"
                )
        for stem, size in requirement.get("sizes", {}).items():
            if observed_sizes.get(stem) != size:
                raise AssertionError(
                    f"Placement {enumeration} loaded {stem} at "
                    f"{observed_sizes.get(stem)}, expected full size {size}"
                )

    small_factory = next(
        obj
        for obj in placements
        if obj.get("bc2_enumeration") == 5777
    )
    factory_assignments = json.loads(
        small_factory.data["bc2_surface_assignments"]
    )
    if any(
        assignment["match"] == "unresolved"
        for assignment in factory_assignments
    ):
        raise AssertionError(
            "SmallFactory sparse override left unresolved surfaces"
        )

    placement_extents = [
        max(
            maximum - minimum
            for minimum, maximum in world_bounds((obj,))
        )
        for obj in placements
    ]
    largest_placement_extent = max(placement_extents)
    if largest_placement_extent > 4_000.0:
        raise AssertionError(
            "A placement has implausible decoded bounds: "
            f"{largest_placement_extent:.3f}"
        )

    shader_key_matches = collections.Counter(
        material.get("bc2_shader_key_match", "<none>")
        for material in bpy.data.materials
    )
    return {
        "placements": len(placements),
        "linked_meshes": len(unique_meshes),
        "heightfields": int(root["bc2_heightfield_count"]),
        "images": len(source_images),
        "collection_counts": collection_counts,
        "diagnostic_counts": dict(diagnostic_counts),
        "surface_matches": dict(surface_matches),
        "material_status": dict(material_status),
        "base_color_graphs": base_color_graphs,
        "largest_placement_extent": largest_placement_extent,
        "shader_key_matches": dict(shader_key_matches),
        "terrain_bounds": terrain_bounds,
        "median_forest_contact": median_forest_contact,
        "water_bounds": water_bounds,
        "targets": targets,
    }


def main():
    args = _arguments()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    level_name = args.level.strip().upper()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    settings = SimpleNamespace(
        axis_conversion=True,
        unit_scale=1.0,
        flip_v=True,
        include_z_only=False,
        terrain_resolution=args.terrain_resolution,
        map_import_materials=not args.no_materials,
    )

    started = time.perf_counter()
    repository = AssetRepository(args.iso, args.cache)
    try:
        if repository.needs_scan():
            failures = repository.scan(_progress("SCAN"))
            if failures:
                raise RuntimeError(f"Disc scan had {len(failures)} failure(s)")
        archives = index_pc_level_archives(
            repository,
            level_name,
            _progress("INDEX"),
        )
        print("RESOLVE placement graph", flush=True)
        level_scene = LevelResolver(
            repository,
            level_name,
            archives,
        ).resolve(_resolve_progress)
        _validate_resolved_model(level_scene)
        result = blender_addon._create_level_scene(
            bpy.context,
            repository,
            level_scene,
            archives,
            settings,
            _scene_progress,
        )
        summary = result["summary"]
        root_name = result["root"].name
        bpy.context.scene["bc2_level"] = level_name
        bpy.context.scene["bc2_import_summary"] = json.dumps(
            summary,
            separators=(",", ":"),
        )
        if args.pack_images:
            bpy.ops.file.pack_all()
        bpy.ops.wm.save_as_mainfile(filepath=str(output), check_existing=False)
    finally:
        repository.close()

    bpy.ops.wm.open_mainfile(filepath=str(output), load_ui=False)
    saved = _validate_saved_scene(root_name, summary)
    elapsed = time.perf_counter() - started
    print(
        "PASS "
        + json.dumps(
            {
                "output": str(output),
                "elapsed_seconds": round(elapsed, 3),
                "summary": summary,
                "saved": saved,
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
