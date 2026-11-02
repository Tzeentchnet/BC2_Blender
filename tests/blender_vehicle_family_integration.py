# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import traceback
from pathlib import Path

import bpy


ROOT = Path(__file__).resolve().parents[1]
if "--installed" in sys.argv:
    package = (
        "bl_ext.user_default.bc2_frostbite_importer."
        "bc2_frostbite_importer"
    )
    blender_addon = importlib.import_module(package + ".blender_addon")
    parse_meshdata = importlib.import_module(
        package + ".core.mesh"
    ).parse_meshdata
    AssetRepository = importlib.import_module(
        package + ".core.repository"
    ).AssetRepository
else:
    sys.path.insert(0, str(ROOT))
    from bc2_frostbite_importer import blender_addon
    from bc2_frostbite_importer.core.mesh import parse_meshdata
    from bc2_frostbite_importer.core.repository import AssetRepository


ZERO_ARCHIVE_PATH = "Dist/win32/levels/sp_01/level-00.fbrb"
ZERO_MESH_SET_PATH = (
    "Objects/Vehicles/Beach/Jap_Zero_Fighter/Zero_Fighter_Mesh.res"
)
CORSAIR_ARCHIVE_PATH = "Dist/win32/levels/sp_01/level-00.fbrb"
CORSAIR_MESH_SET_PATH = (
    "Objects/Vehicles/Beach/US_Corsair/US_corsair_Mesh.res"
)
UAV_ARCHIVE_PATH = "Dist/win32/levels/mp_common/level-00.fbrb"
UAV_MESH_SET_PATH = (
    "Objects/Vehicles/Air/UAV/UAV_Helicopter_Mesh.res"
)
EXPECTED_ZERO_OBJECTS = 24
EXPECTED_ZERO_TRIANGLES = 8_459
DECLARATION_CASES = (
    (
        "AH60",
        "Dist/win32/levels/mp_common/level-00.fbrb",
        "Objects/Vehicles/Air/AH60/AH60_Mesh_lod0_data.res",
        11,
        (24, 28, 32, 40),
        {36},
    ),
    (
        "AH64",
        "Dist/win32/levels/mp_005/level-00.fbrb",
        "Objects/Vehicles/Air/AH64/AH64_Mesh_lod0_data.res",
        7,
        (28, 24, 32, 36, 40),
        {44},
    ),
    (
        "MI28",
        "Dist/win32/levels/mp_005/level-00.fbrb",
        "Objects/Vehicles/Air/Mi28/Mi28_Mesh_lod0_data.res",
        10,
        (24, 28, 36, 40),
        {32},
    ),
    (
        "Zero Fighter",
        ZERO_ARCHIVE_PATH,
        (
            "Objects/Vehicles/Beach/Jap_Zero_Fighter/"
            "Zero_Fighter_Mesh_lod0_data.res"
        ),
        4,
        (36, 40, 44),
        {48},
    ),
    (
        "M1A2",
        "Dist/win32/levels/mp_common/level-00.fbrb",
        "Objects/Vehicles/Land/M1A2/M1A2_Mesh_lod0_data.res",
        5,
        (28, 24, 32, 36),
        {40},
    ),
    (
        "T90",
        "Dist/win32/levels/mp_common/level-00.fbrb",
        "Objects/Vehicles/Land/T90/T90_Mesh_lod0_data.res",
        4,
        (36, 32, 40, 44),
        {48},
    ),
)


def _arguments():
    values = sys.argv
    values = values[values.index("--") + 1 :] if "--" in values else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--installed", action="store_true")
    return parser.parse_args(values)


def _settings():
    return type(
        "VehicleFamilySettings",
        (),
        {
            "axis_conversion": True,
            "unit_scale": 1.0,
            "flip_v": True,
            "include_z_only": False,
            "material_mode": "FULL",
            "composite_visibility": "ALL",
            "create_diagnostics": True,
        },
    )()


def _degenerate_uv_triangles(obj, layer_name):
    layer = obj.data.uv_layers.get(layer_name)
    if layer is None:
        raise AssertionError(
            f"{obj.name} is missing UV layer {layer_name}"
        )
    values = [tuple(item.uv) for item in layer.data]
    count = 0
    for polygon in obj.data.polygons:
        if len(polygon.loop_indices) != 3:
            continue
        first, second, third = (
            values[index]
            for index in polygon.loop_indices
        )
        area = abs(
            (second[0] - first[0]) * (third[1] - first[1])
            - (second[1] - first[1]) * (third[0] - first[0])
        ) * 0.5
        if area < 1e-10:
            count += 1
    return count


def _assert_declaration_cases(repository):
    results = {}
    for (
        name,
        archive_path,
        mesh_path,
        expected_subsets,
        expected_uv_offsets,
        expected_part_offsets,
    ) in DECLARATION_CASES:
        archive = repository.catalog.archive_by_path(archive_path)
        if archive is None:
            raise AssertionError(
                f"{name} archive is missing: {archive_path}"
            )
        if not archive["indexed"]:
            repository.index_archive(int(archive["id"]))
        asset = repository.catalog.find_asset(
            mesh_path,
            (archive_path,),
        )
        if asset is None:
            raise AssertionError(
                f"{name} LOD0 MeshData is missing: {mesh_path}"
            )
        _, raw = repository.read_asset(
            int(asset["id"]),
            max_size=1 << 30,
        )
        mesh = parse_meshdata(
            raw,
            asset["platform"],
            retain_pc_vehicle_uv_candidates=True,
        )
        if mesh.mesh_type != 2 or len(mesh.subsets) != expected_subsets:
            raise AssertionError(
                f"{name} composite LOD0 structure changed"
            )
        actual_offsets = {
            subset.uv_offsets for subset in mesh.subsets
        }
        if actual_offsets != {expected_uv_offsets}:
            raise AssertionError(
                f"{name} UV declarations are {actual_offsets}, "
                f"expected {expected_uv_offsets}"
            )
        actual_part_offsets = {
            subset.part_index_offset
            for subset in mesh.subsets
            if subset.part_index_offset is not None
        }
        if actual_part_offsets != expected_part_offsets:
            raise AssertionError(
                f"{name} part selectors are {actual_part_offsets}, "
                f"expected {expected_part_offsets}"
            )
        results[name] = {
            "subsets": len(mesh.subsets),
            "uv_offsets": list(expected_uv_offsets),
            "part_offsets": sorted(expected_part_offsets),
        }
    return results


def _assert_zero_import(objects, root):
    if (
        len(objects) != EXPECTED_ZERO_OBJECTS
        or sum(len(obj.data.polygons) for obj in objects)
        != EXPECTED_ZERO_TRIANGLES
    ):
        raise AssertionError(
            "Zero Fighter imported topology does not match retail LOD0"
        )
    if int(root.get("bc2_diagnostic_count", 0)) != 0:
        raise AssertionError(
            "Zero Fighter import produced material diagnostics"
        )

    relevant = [
        obj
        for obj in objects
        if obj.get("bc2_source_subset")
        in {
            "lod0_mainframe_MainBody",
            "lod0_mainframe_Glass",
            "lod0_Windowfront_GlassInside",
        }
    ]
    if not relevant:
        raise AssertionError(
            "Zero Fighter material subsets were not imported"
        )
    if any(
        json.loads(obj.data.get("bc2_uv_offsets", "[]"))
        != [36, 40, 44]
        for obj in relevant
    ):
        raise AssertionError(
            "Zero Fighter objects do not retain the byte-36 UV declaration"
        )
    triangle_count = sum(len(obj.data.polygons) for obj in relevant)
    degenerate_count = sum(
        _degenerate_uv_triangles(obj, "UVMap")
        for obj in relevant
    )
    if degenerate_count >= triangle_count // 10:
        raise AssertionError(
            "Zero Fighter primary UV coverage remains degenerate"
        )

    materials = {
        material.get("bc2_surface_name"): material
        for obj in objects
        for material in obj.data.materials
    }
    outer_glass = materials.get("Glass")
    inner_glass = materials.get("GlassInside")
    if outer_glass is None or inner_glass is None:
        raise AssertionError(
            "Zero Fighter glass materials were not imported"
        )
    outer_shader = next(
        (
            node
            for node in outer_glass.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        ),
        None,
    )
    if (
        outer_glass.get("bc2_material_family") != "vehicle glass"
        or outer_glass.get("bc2_material_status")
        != "resolved semantic vehicle glass graph"
        or outer_glass.surface_render_method != "DITHERED"
        or outer_shader is None
        or not outer_shader.inputs["Alpha"].is_linked
        or outer_shader.inputs["Transmission Weight"].default_value != 1.0
    ):
        raise AssertionError(
            "Zero Fighter outer glass graph is incomplete"
        )
    bindings = json.loads(
        outer_glass.get("bc2_texture_bindings", "[]")
    )
    roles = {
        binding["index"]: binding["role"]
        for binding in bindings
    }
    if roles.get(257) != "ENVIRONMENT" or "COLOR" in roles.values():
        raise AssertionError(
            "Zero Fighter sky reflection was promoted to Base Color"
        )
    if (
        inner_glass.get("bc2_material_family")
        != "vehicle interior glass"
        or inner_glass.node_tree.nodes.get(
            "BC2 glass opacity scale"
        )
        is None
    ):
        raise AssertionError(
            "Zero Fighter interior glass opacity policy is missing"
        )


def _texture_nodes(material):
    return {
        Path(str(node.get("bc2_asset_path", ""))).name.casefold(): node
        for node in material.node_tree.nodes
        if node.type == "TEX_IMAGE"
    }


def _surface_material(objects, surface_name):
    return next(
        (
            material
            for obj in objects
            for material in obj.data.materials
            if str(
                material.get("bc2_surface_name", "")
            ).casefold()
            == surface_name.casefold()
        ),
        None,
    )


def _assert_corsair_import(objects, root):
    if int(root.get("bc2_diagnostic_count", 0)) != 0:
        raise AssertionError("Corsair import produced material diagnostics")
    material = _surface_material(objects, "main")
    if (
        material is None
        or material.get("bc2_material_status") != "resolved material graph"
        or "sp_01" not in str(
            material.get("bc2_shader_database_source", "")
        ).casefold()
    ):
        raise AssertionError(
            "Corsair main material did not resolve from SP_01"
        )
    shader = next(
        node
        for node in material.node_tree.nodes
        if node.type == "BSDF_PRINCIPLED"
    )
    if not shader.inputs["Base Color"].is_linked:
        raise AssertionError("Corsair _CM texture is not connected to color")
    nodes = _texture_nodes(material)
    expected = {
        "corsair_nm": ("UVMap", (1.0, 1.0)),
        "nm_general_002": ("UVMap", (128.0, 64.0)),
        "vehicledecals": ("UV1", (1.0, 1.0)),
        "corsair_cm": ("UVMap", (1.0, 1.0)),
    }
    for name, (uv_map, scale) in expected.items():
        node = nodes.get(name)
        if (
            node is None
            or node.get("bc2_uv_route_source") != "shader"
            or node.get("bc2_uv_map") != uv_map
            or tuple(node.get("bc2_uv_scale", ())) != scale
        ):
            raise AssertionError(
                f"Corsair shader route is incorrect for {name}"
            )
    bindings = json.loads(
        material.get("bc2_texture_bindings", "[]")
    )
    roles = {binding["index"]: binding["role"] for binding in bindings}
    if roles.get(4) != "COLOR" or roles.get(261) != "ENVIRONMENT":
        raise AssertionError("Corsair color or cube role is incorrect")


def _assert_uav_import(objects, root):
    if int(root.get("bc2_diagnostic_count", 0)) != 0:
        raise AssertionError("UAV import produced material diagnostics")
    material = _surface_material(objects, "UAV_Main")
    shader_source = str(
        material.get("bc2_shader_database_source", "")
        if material is not None
        else ""
    ).casefold()
    if (
        material is None
        or material.get("bc2_material_status") != "resolved material graph"
        or "dx11_single.res" not in shader_source
    ):
        raise AssertionError(
            "UAV main material has no shader database provenance"
        )
    nodes = _texture_nodes(material)
    expected = {
        "uav_d": ("UV1", (1.0, 1.0)),
        "dt_dirt_002": ("UV1", (8.0, 8.0)),
    }
    for name, (uv_map, scale) in expected.items():
        node = nodes.get(name)
        if (
            node is None
            or node.get("bc2_uv_route_source") != "shader"
            or node.get("bc2_uv_map") != uv_map
            or tuple(node.get("bc2_uv_scale", ())) != scale
        ):
            raise AssertionError(f"UAV shader route is incorrect for {name}")


def _import_mesh_set(repository, mesh_set_path, archive_path):
    mesh_set_asset = repository.catalog.find_asset(
        mesh_set_path,
        (archive_path,),
    )
    if mesh_set_asset is None:
        raise AssertionError(f"Vehicle mesh set is missing: {mesh_set_path}")
    mesh_asset, mesh_raw = repository.read_asset(
        int(mesh_set_asset["id"]),
        max_size=1 << 30,
        resolve_mesh=True,
    )
    parsed = parse_meshdata(
        mesh_raw,
        mesh_asset["platform"],
        retain_pc_vehicle_uv_candidates=True,
    )
    root, objects = blender_addon._create_mesh_set_import(
        bpy.context,
        repository,
        mesh_set_asset,
        parsed,
        mesh_asset,
        _settings(),
    )
    return root, objects, str(mesh_asset["path"])


def main():
    arguments = _arguments()
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    repository = AssetRepository(arguments.iso, arguments.cache)
    try:
        declaration_results = _assert_declaration_cases(repository)
        zero_root, zero_objects, zero_mesh_data = _import_mesh_set(
            repository,
            ZERO_MESH_SET_PATH,
            ZERO_ARCHIVE_PATH,
        )
        corsair_root, corsair_objects, corsair_mesh_data = _import_mesh_set(
            repository,
            CORSAIR_MESH_SET_PATH,
            CORSAIR_ARCHIVE_PATH,
        )
        uav_root, uav_objects, uav_mesh_data = _import_mesh_set(
            repository,
            UAV_MESH_SET_PATH,
            UAV_ARCHIVE_PATH,
        )
        _assert_zero_import(zero_objects, zero_root)
        _assert_corsair_import(corsair_objects, corsair_root)
        _assert_uav_import(uav_objects, uav_root)
        root_names = {
            "zero": zero_root.name,
            "corsair": corsair_root.name,
            "uav": uav_root.name,
        }

        bpy.ops.wm.save_as_mainfile(filepath=str(output))
        bpy.ops.wm.open_mainfile(filepath=str(output))

        roots = {
            name: bpy.data.collections.get(root_name)
            for name, root_name in root_names.items()
        }
        if any(root is None for root in roots.values()):
            raise AssertionError(
                "Vehicle root collection did not survive save/reopen"
            )
        reopened = {
            "zero": [
                obj
                for obj in bpy.data.objects
                if str(obj.get("bc2_asset_path", "")).casefold()
                == zero_mesh_data.casefold()
            ],
            "corsair": [
                obj
                for obj in bpy.data.objects
                if str(obj.get("bc2_asset_path", "")).casefold()
                == corsair_mesh_data.casefold()
            ],
            "uav": [
                obj
                for obj in bpy.data.objects
                if str(obj.get("bc2_asset_path", "")).casefold()
                == uav_mesh_data.casefold()
            ],
        }
        _assert_zero_import(reopened["zero"], roots["zero"])
        _assert_corsair_import(reopened["corsair"], roots["corsair"])
        _assert_uav_import(reopened["uav"], roots["uav"])
        print(
            "VEHICLE_FAMILY_INTEGRATION "
            + json.dumps(
                {
                    "declarations": declaration_results,
                    "objects": {
                        name: len(objects)
                        for name, objects in reopened.items()
                    },
                    "output": str(output),
                    "triangles": {
                        name: sum(
                            len(obj.data.polygons)
                            for obj in objects
                        )
                        for name, objects in reopened.items()
                    },
                },
                sort_keys=True,
            )
        )
    finally:
        repository.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        os._exit(1)
