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
    modular_assembly = importlib.import_module(
        package + ".core.assembly"
    ).modular_assembly
    parse_dbx = importlib.import_module(package + ".core.dbx").parse_dbx
    parse_meshdata = importlib.import_module(
        package + ".core.mesh"
    ).parse_meshdata
    AssetRepository = importlib.import_module(
        package + ".core.repository"
    ).AssetRepository
else:
    sys.path.insert(0, str(ROOT))
    from bc2_frostbite_importer import blender_addon
    from bc2_frostbite_importer.core.assembly import modular_assembly
    from bc2_frostbite_importer.core.dbx import parse_dbx
    from bc2_frostbite_importer.core.mesh import parse_meshdata
    from bc2_frostbite_importer.core.repository import AssetRepository


ARCHIVE_PATH = "Dist/win32/levels/mp_005/level-00.fbrb"
ASSEMBLY_PATH = "Objects/Vehicles/Air/Mi28/Mi28.dbx"
MESH_DATA_PATH = (
    "Objects/Vehicles/Air/Mi28/Mi28_Mesh_lod0_data.res"
)
EXPECTED_PARTS = 24
EXPECTED_OBJECTS = 44
EXPECTED_TRIANGLES = 21_032
EXPECTED_UV_OFFSETS = [24, 28, 36, 40]


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
        "Mi28Settings",
        (),
        {
            "axis_conversion": True,
            "unit_scale": 1.0,
            "flip_v": True,
            "include_z_only": False,
            "material_mode": "FULL",
            "composite_visibility": "DEFAULT_STATE",
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


def _texture_routes(material):
    return {
        node.get("bc2_binding_index"): (
            node.get("bc2_uv_map"),
            tuple(node.get("bc2_uv_scale")),
        )
        for node in material.node_tree.nodes
        if node.type == "TEX_IMAGE"
    }


def main():
    arguments = _arguments()
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    repository = AssetRepository(arguments.iso, arguments.cache)
    try:
        if repository.needs_scan():
            repository.scan()
        archive = repository.catalog.archive_by_path(ARCHIVE_PATH)
        if archive is None:
            raise AssertionError(
                f"PC archive is missing: {ARCHIVE_PATH}"
            )
        if not archive["indexed"]:
            repository.index_archive(int(archive["id"]))

        mesh_asset = repository.catalog.find_asset(
            MESH_DATA_PATH,
            (ARCHIVE_PATH,),
        )
        if mesh_asset is None:
            raise AssertionError(
                f"MI28 LOD0 MeshData is missing: {MESH_DATA_PATH}"
            )
        _, mesh_raw = repository.read_asset(
            int(mesh_asset["id"]),
            max_size=1 << 30,
        )
        parsed = parse_meshdata(mesh_raw, mesh_asset["platform"])
        if parsed.mesh_type != 2 or len(parsed.subsets) != 10:
            raise AssertionError("MI28 composite LOD0 structure changed")
        if any(
            list(subset.uv_offsets) != EXPECTED_UV_OFFSETS
            for subset in parsed.subsets
        ):
            raise AssertionError(
                "MI28 stride-48 shader coordinates are incorrect: "
                f"{[subset.uv_offsets for subset in parsed.subsets]}"
            )
        if {
            subset.part_index_offset
            for subset in parsed.subsets
            if subset.part_index_offset is not None
        } != {32}:
            raise AssertionError(
                "MI28 composite part selector is not byte 32"
            )

        assembly_asset = repository.catalog.find_asset(
            ASSEMBLY_PATH,
            (ARCHIVE_PATH,),
        )
        if assembly_asset is None:
            raise AssertionError(
                f"MI28 assembly is missing: {ASSEMBLY_PATH}"
            )
        _, assembly_raw = repository.read_asset(
            int(assembly_asset["id"]),
            max_size=256 << 20,
        )
        assembly = modular_assembly(
            parse_dbx(assembly_raw),
            ASSEMBLY_PATH,
        )
        if (
            assembly is None
            or assembly.kind != "VEHICLE"
            or len(assembly.parts) != EXPECTED_PARTS
        ):
            raise AssertionError("MI28 modular assembly was not resolved")

        imported = blender_addon._create_modular_assembly(
            bpy.context,
            repository,
            assembly_asset,
            assembly,
            _settings(),
        )
        if imported.armature is not None:
            raise AssertionError("MI28 import invented an armature")
        if len(imported.components) != 1:
            raise AssertionError("MI28 did not resolve one composite mesh")
        objects = list(imported.components[0].objects)
        if (
            len(objects) != EXPECTED_OBJECTS
            or sum(len(obj.data.polygons) for obj in objects)
            != EXPECTED_TRIANGLES
        ):
            raise AssertionError(
                "MI28 imported topology does not match retail LOD0"
            )
        if imported.root_collection["bc2_diagnostic_count"] != 0:
            raise AssertionError("MI28 import produced material diagnostics")

        windshield = next(
            (
                obj
                for obj in objects
                if obj.get("bc2_source_subset")
                == "lod0_Windshields_Glass"
            ),
            None,
        )
        if (
            windshield is None
            or windshield.get("bc2_part_name") != "Windshields"
            or len(windshield.data.vertices) != 36
            or len(windshield.data.polygons) != 20
            or list(windshield.data.uv_layers.keys())
            != ["UVMap", "UV1", "UV2", "UV3"]
            or json.loads(windshield.data["bc2_uv_offsets"])
            != EXPECTED_UV_OFFSETS
            or _degenerate_uv_triangles(windshield, "UVMap")
            or _degenerate_uv_triangles(windshield, "UV1")
        ):
            raise AssertionError(
                "MI28 windshield geometry or UV channels are incorrect"
            )

        materials = {
            material.name: material
            for obj in objects
            for material in obj.data.materials
        }
        main = materials.get("BC2 Main")
        main_metal = materials.get("BC2 MainMetal")
        glass = materials.get("BC2 Glass")
        if main is None or main_metal is None or glass is None:
            raise AssertionError("MI28 verified materials are missing")
        expected_main_routes = {
            1: ("UVMap", (1.0, 1.0)),
            2: ("UV1", (1.0, 1.0)),
            3: ("UV2", (1.0, 1.0)),
            4: ("UV3", (1.0, 1.0)),
            5: ("UVMap", (1.0, 1.0)),
            6: ("UV1", (10.0, 10.0)),
        }
        expected_metal_routes = {
            index + 1: route
            for index, route in expected_main_routes.items()
        }
        if (
            main.get("bc2_material_family") != "MI28 main"
            or _texture_routes(main) != expected_main_routes
            or main.node_tree.nodes.get(
                "BC2 MI28 main detail modulation"
            )
            is None
            or main_metal.get("bc2_material_family")
            != "MI28 main metal"
            or _texture_routes(main_metal) != expected_metal_routes
        ):
            raise AssertionError(
                "MI28 Main/MainMetal material decoding is incorrect"
            )

        glass_shader = next(
            node
            for node in glass.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        if (
            glass.get("bc2_material_family") != "MI28 glass"
            or glass.get("bc2_material_status")
            != "resolved verified MI28 glass graph"
            or not glass_shader.inputs["Alpha"].is_linked
            or glass_shader.inputs[
                "Transmission Weight"
            ].default_value
            != 1.0
            or glass.surface_render_method != "DITHERED"
        ):
            raise AssertionError("MI28 glass graph is incomplete")

        bpy.ops.wm.save_as_mainfile(filepath=str(output))
        bpy.ops.wm.open_mainfile(filepath=str(output))
        reopened_windshield = bpy.data.objects.get(
            "06_lod0_Windshields_Glass"
        )
        reopened_glass = bpy.data.materials.get("BC2 Glass")
        if (
            reopened_windshield is None
            or json.loads(
                reopened_windshield.data["bc2_uv_offsets"]
            )
            != EXPECTED_UV_OFFSETS
            or reopened_glass is None
            or reopened_glass.get("bc2_material_family") != "MI28 glass"
        ):
            raise AssertionError(
                "Saved scene lost MI28 UV or glass metadata"
            )

        metrics = {
            "objects": len(objects),
            "parts": len(assembly.parts),
            "triangles": EXPECTED_TRIANGLES,
            "uv_offsets": EXPECTED_UV_OFFSETS,
            "windshield_vertices": len(
                reopened_windshield.data.vertices
            ),
            "windshield_triangles": len(
                reopened_windshield.data.polygons
            ),
            "output": str(output),
        }
        print("MI28_INTEGRATION " + json.dumps(metrics, sort_keys=True))
    finally:
        repository.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
