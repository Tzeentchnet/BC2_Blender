# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

import bpy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bc2_frostbite_importer import blender_addon
from bc2_frostbite_importer.core.assembly import modular_assembly
from bc2_frostbite_importer.core.dbx import parse_dbx
from bc2_frostbite_importer.core.repository import AssetRepository


ARCHIVE_PATH = "Dist/win32/levels/mp_common/level-00.fbrb"
ASSEMBLY_PATH = "Objects/Vehicles/Air/AH60/AH60.dbx"
EXPECTED_TRIANGLES = 27_388
EXPECTED_PARTS = 36
EXPECTED_VISIBLE_PARTS = 34
EXPECTED_HIDDEN_PARTS = 2
EXPECTED_DOUBLE_WRECK_OBJECTS = 11
EXPECTED_TARGET_VERTICES = 948
EXPECTED_TARGET_TRIANGLES = 722
EXPECTED_HIDDEN_STATES = {
    "RotorBlurred": "ALTERNATE",
    "TailRotorBlurred": "ALTERNATE",
}
EXPECTED_SINGLE_WRECK_PARTS = {
    "NoseWreck",
    "WreckPartTail",
    "WreckPartTopLeft",
    "WreckPartTopRight",
}
EXPECTED_TRANSLATIONS = {
    "DoorLeft": (-1.523, 1.531, 1.520),
    "Rotor": (0.000, -0.008, 3.917),
    "RotorTail": (-0.548, 10.820, 3.652),
    "WheelLeft": (1.672, -1.212, 0.347),
}


def _arguments():
    values = sys.argv
    values = values[values.index("--") + 1 :] if "--" in values else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--iso", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args(values)


def _settings():
    return type(
        "VehicleSettings",
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


def _child(collection, name):
    return next(
        (child for child in collection.children if child.name == name),
        None,
    )


def _source_images(materials):
    return {
        node.image.name: node.image
        for material in materials
        for node in material.node_tree.nodes
        if node.type == "TEX_IMAGE" and node.image is not None
    }


def _assert_translation(part_name, objects, expected):
    part_objects = [
        obj
        for obj in objects
        if obj.get("bc2_part_name") == part_name
    ]
    if not part_objects:
        raise AssertionError(f"AH60 part is missing: {part_name}")
    for obj in part_objects:
        actual = obj.matrix_basis.translation
        if any(
            abs(left - right) >= 0.01
            for left, right in zip(actual, expected)
        ):
            raise AssertionError(
                f"{part_name} translation {tuple(actual)} "
                f"does not match {expected}"
            )


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
            raise AssertionError(f"PC archive is missing: {ARCHIVE_PATH}")
        if not archive["indexed"]:
            repository.index_archive(int(archive["id"]))

        assembly_asset = repository.catalog.find_asset(
            ASSEMBLY_PATH,
            (ARCHIVE_PATH,),
        )
        if assembly_asset is None:
            raise AssertionError(
                f"Vehicle assembly is missing: {ASSEMBLY_PATH}"
            )
        _, assembly_raw = repository.read_asset(
            int(assembly_asset["id"]),
            max_size=256 << 20,
        )
        assembly = modular_assembly(
            parse_dbx(assembly_raw),
            ASSEMBLY_PATH,
        )
        if assembly is None or assembly.kind != "VEHICLE":
            raise AssertionError("AH60 DBX was not resolved as a vehicle")

        imported = blender_addon._create_modular_assembly(
            bpy.context,
            repository,
            assembly_asset,
            assembly,
            _settings(),
        )
        if imported.armature is not None:
            raise AssertionError("Vehicle import invented an armature")
        if len(imported.components) != 1:
            raise AssertionError("AH60 did not resolve one composite mesh")
        component = imported.components[0]
        objects = list(component.objects)
        if not objects:
            raise AssertionError("AH60 composite mesh created no objects")

        components_collection = _child(
            imported.root_collection,
            "Components",
        )
        parts_collection = _child(component.collection, "Parts")
        if components_collection is None or parts_collection is None:
            raise AssertionError("AH60 modular collection hierarchy is missing")
        part_collections = list(parts_collection.children)
        if len(part_collections) != EXPECTED_PARTS:
            raise AssertionError(
                f"AH60 has {len(part_collections)} part collections "
                f"instead of {EXPECTED_PARTS}"
            )
        if not any("Fuselage" in item.name for item in part_collections):
            raise AssertionError("AH60 source Fuselage part name is missing")
        if not any("Rotor" in item.name for item in part_collections):
            raise AssertionError("AH60 source rotor part name is missing")
        parts_by_name = {
            item["bc2_part_name"]: item
            for item in part_collections
        }
        visible_parts = [
            item
            for item in part_collections
            if not item.hide_viewport and not item.hide_render
        ]
        hidden_parts = [
            item
            for item in part_collections
            if item.hide_viewport and item.hide_render
        ]
        if any(
            "bc2_part_bounds" not in item
            or "bc2_part_transform" not in item
            or "bc2_part_state_source" not in item
            for item in part_collections
        ):
            raise AssertionError("AH60 part transform metadata is incomplete")
        if any(
            item.get("bc2_part_default_visible")
            != (item in visible_parts)
            for item in part_collections
        ):
            raise AssertionError("AH60 visibility metadata is inconsistent")
        if len(visible_parts) != EXPECTED_VISIBLE_PARTS:
            raise AssertionError(
                f"AH60 has {len(visible_parts)} default-visible parts "
                f"instead of {EXPECTED_VISIBLE_PARTS}"
            )
        if len(hidden_parts) != EXPECTED_HIDDEN_PARTS:
            raise AssertionError(
                f"AH60 has {len(hidden_parts)} hidden parts "
                f"instead of {EXPECTED_HIDDEN_PARTS}"
            )
        if {item["bc2_part_name"] for item in hidden_parts} != set(
            EXPECTED_HIDDEN_STATES
        ):
            raise AssertionError("AH60 default-hidden part set is incorrect")
        for part_name, expected_state in EXPECTED_HIDDEN_STATES.items():
            part = parts_by_name[part_name]
            if part["bc2_part_state"] != expected_state:
                raise AssertionError(
                    f"{part_name} state is {part['bc2_part_state']} "
                    f"instead of {expected_state}"
                )
        single_wreck_parts = [
            item
            for item in part_collections
            if item["bc2_part_name"].casefold().count("wreck") == 1
        ]
        if {
            item["bc2_part_name"]
            for item in single_wreck_parts
        } != EXPECTED_SINGLE_WRECK_PARTS:
            raise AssertionError(
                "AH60 single-Wreck part collection set is incorrect"
            )
        if any(
            item.hide_viewport
            or item.hide_render
            or item.get("bc2_part_state") != "DEFAULT"
            or item.get("bc2_part_default_visible") is not True
            for item in single_wreck_parts
        ):
            raise AssertionError(
                "AH60 single-Wreck part collection is hidden"
            )
        if (
            parts_by_name["Rotor"].hide_viewport
            or parts_by_name["RotorTail"].hide_viewport
        ):
            raise AssertionError("AH60 low-RPM rotors are hidden")
        if (
            component.collection["bc2_default_visible_part_count"]
            != EXPECTED_VISIBLE_PARTS
            or component.collection["bc2_hidden_part_count"]
            != EXPECTED_HIDDEN_PARTS
        ):
            raise AssertionError("AH60 part visibility summary is incorrect")
        for part_name, expected in EXPECTED_TRANSLATIONS.items():
            _assert_translation(part_name, objects, expected)

        triangle_count = sum(
            len(obj.data.polygons) for obj in objects
        )
        if triangle_count != EXPECTED_TRIANGLES:
            raise AssertionError(
                f"AH60 imported {triangle_count} total triangles "
                f"instead of {EXPECTED_TRIANGLES}"
            )
        if any(
            obj.get("bc2_part_index", -1) < 0
            or not obj.get("bc2_part_name")
            for obj in objects
        ):
            raise AssertionError("AH60 has unassigned composite geometry")
        if any(
            obj.get("bc2_assembly_path") != ASSEMBLY_PATH
            or obj.get("bc2_component_role") != "VEHICLE"
            for obj in objects
        ):
            raise AssertionError("AH60 source metadata is incomplete")
        double_wreck_objects = [
            obj
            for obj in objects
            if obj.name.casefold().count("wreck") >= 2
        ]
        if len(double_wreck_objects) != EXPECTED_DOUBLE_WRECK_OBJECTS:
            raise AssertionError(
                f"AH60 has {len(double_wreck_objects)} double-Wreck "
                f"objects instead of {EXPECTED_DOUBLE_WRECK_OBJECTS}"
            )
        if any(
            not obj.hide_viewport
            or not obj.hide_render
            or not obj.hide_get()
            or obj.get("bc2_geometry_default_visible") is not False
            or obj.get("bc2_geometry_visibility_source")
            != "name_contains_wreck_twice"
            for obj in double_wreck_objects
        ):
            raise AssertionError(
                "AH60 double-Wreck geometry is not hidden by default"
            )
        single_wreck_objects = [
            obj
            for obj in objects
            if obj.name.casefold().count("wreck") == 1
        ]
        if not single_wreck_objects or any(
            obj.hide_viewport
            or obj.hide_render
            or obj.get("bc2_geometry_wreck_name_count") != 1
            for obj in single_wreck_objects
        ):
            raise AssertionError("AH60 single-Wreck geometry was hidden")
        visible_single_wreck = [
            obj
            for obj in single_wreck_objects
            if obj.get("bc2_part_name") in EXPECTED_SINGLE_WRECK_PARTS
        ]
        if not visible_single_wreck or any(
            obj.hide_viewport
            or obj.hide_render
            or obj.hide_get()
            or obj.get("bc2_geometry_default_visible") is not True
            for obj in visible_single_wreck
        ):
            raise AssertionError(
                "AH60 single-Wreck collection hides healthy geometry"
            )

        target_objects = [
            obj
            for obj in objects
            if obj.get("bc2_source_subset")
            == "lod0_WreckPartTail_Main"
            and obj.get("bc2_part_name") == "WingLeft"
        ]
        if len(target_objects) != 1:
            raise AssertionError(
                "AH60 WingLeft Main LOD0 source piece is ambiguous"
            )
        target = target_objects[0]
        if (
            not str(target.get("bc2_asset_path", "")).casefold().endswith(
                "_lod0_data.res"
            )
            or len(target.data.vertices) != EXPECTED_TARGET_VERTICES
            or len(target.data.polygons) != EXPECTED_TARGET_TRIANGLES
        ):
            raise AssertionError(
                "AH60 WingLeft did not retain authored LOD0 geometry"
            )
        if [layer.name for layer in target.data.uv_layers] != [
            "UVMap",
            "UV1",
            "UV2",
            "UV3",
        ]:
            raise AssertionError(
                "AH60 WingLeft is missing declared shader coordinates"
            )
        if json.loads(target.data["bc2_uv_offsets"]) != [24, 28, 32, 40]:
            raise AssertionError(
                "AH60 WingLeft shader-coordinate offsets are incorrect"
            )
        if _degenerate_uv_triangles(target, "UV1"):
            raise AssertionError(
                "AH60 WingLeft base-color UV channel is degenerate"
            )

        materials = {
            obj.data.materials[0].name: obj.data.materials[0]
            for obj in objects
            if obj.data.materials
        }
        unresolved_materials = {
            name: material.get("bc2_material_status", "")
            for name, material in materials.items()
            if not str(
                material.get("bc2_material_status", "")
            ).startswith("resolved ")
        }
        if not materials or unresolved_materials:
            raise AssertionError(
                f"AH60 has unresolved source materials: "
                f"{unresolved_materials}"
            )
        if any(
            material.get("bc2_shader_key_match") != "exact"
            for material in materials.values()
        ):
            raise AssertionError("AH60 material keys did not resolve exactly")
        glass = materials.get("BC2 Glass")
        interior = materials.get("BC2 Interior")
        main = materials.get("BC2 Main")
        minigun = materials.get("BC2 Minigun_Main")
        if (
            glass is None
            or interior is None
            or main is None
            or minigun is None
        ):
            raise AssertionError("AH60 verified material families are missing")
        glass_shader = next(
            node
            for node in glass.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        if (
            glass.get("bc2_material_family") != "AH60 glass"
            or not glass_shader.inputs["Alpha"].is_linked
            or glass_shader.inputs[
                "Transmission Weight"
            ].default_value != 1.0
            or glass.surface_render_method != "DITHERED"
        ):
            raise AssertionError("AH60 glass graph is incomplete")
        interior_shader = next(
            node
            for node in interior.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        if (
            interior.get("bc2_material_family") != "AH60 interior"
            or not interior_shader.inputs["Base Color"].is_linked
            or interior_shader.inputs[
                "Base Color"
            ].links[0].from_node.name
            != "BC2 interior albedo scale"
        ):
            raise AssertionError("AH60 interior graph is incomplete")
        main_shader = next(
            node
            for node in main.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        main_uvs = {
            node.get("bc2_binding_index"): node.get("bc2_uv_map")
            for node in main.node_tree.nodes
            if node.get("bc2_binding_index") in {1, 2, 3, 4, 5}
        }
        generic_main_mask = next(
            node
            for node in main.node_tree.nodes
            if node.get("bc2_binding_index") == 5
        )
        if (
            main.get("bc2_material_family") != "AH60 main"
            or not main_shader.inputs["Base Color"].is_linked
            or main_shader.inputs["Base Color"].links[0].from_node.name
            != "BC2 AH60 main detail modulation"
            or main_uvs
            != {
                1: "UV1",
                2: "UV2",
                3: "UV3",
                4: "UVMap",
                5: "UV1",
            }
            or tuple(generic_main_mask.get("bc2_uv_scale"))
            != (15.0, 15.0)
        ):
            raise AssertionError("AH60 Main graph or UV routing is incomplete")
        minigun_shader = next(
            node
            for node in minigun.node_tree.nodes
            if node.type == "BSDF_PRINCIPLED"
        )
        minigun_color = minigun_shader.inputs["Base Color"]
        minigun_specular = minigun_shader.inputs["Specular IOR Level"]
        minigun_control = next(
            node
            for node in minigun.node_tree.nodes
            if node.get("bc2_binding_index") == 4
        )
        generic_mask = next(
            node
            for node in minigun.node_tree.nodes
            if node.get("bc2_binding_index") == 3
        )
        if (
            minigun.get("bc2_material_family") != "Minigun main"
            or not minigun_color.is_linked
            or minigun_color.links[0].from_node.name
            != "BC2 minigun base color"
            or not minigun_specular.is_linked
            or minigun_control.image.colorspace_settings.name != "Non-Color"
            or minigun_control.get("bc2_uv_map") != "UVMap"
            or generic_mask.get("bc2_uv_map") != "UVMap"
            or tuple(generic_mask.get("bc2_uv_scale")) != (13.0, 13.0)
        ):
            raise AssertionError("AH60 minigun graph is incomplete")
        symbols = next(
            node
            for node in minigun.node_tree.nodes
            if node.get("bc2_binding_index") == 2
        )
        if symbols.get("bc2_uv_map") != "UV1":
            raise AssertionError("AH60 minigun symbols do not use UV1")
        images = _source_images(materials.values())
        if not images:
            raise AssertionError("AH60 loaded no source material images")
        for image in images.values():
            if image.source != "FILE" or not Path(
                bpy.path.abspath(image.filepath)
            ).is_file():
                raise AssertionError(
                    f"{image.name} is not backed by a source DDS file"
                )

        bpy.ops.wm.save_as_mainfile(
            filepath=str(output),
            check_existing=False,
        )
        component_collection_name = component.collection.name
        bpy.ops.wm.open_mainfile(filepath=str(output), load_ui=False)

        reopened = [
            obj
            for obj in bpy.data.objects
            if obj.type == "MESH"
            and obj.get("bc2_assembly_path") == ASSEMBLY_PATH
        ]
        if len(reopened) != len(objects):
            raise AssertionError("Saved scene lost AH60 part objects")
        reopened_triangles = sum(
            len(obj.data.polygons) for obj in reopened
        )
        if reopened_triangles != EXPECTED_TRIANGLES:
            raise AssertionError("Saved scene lost AH60 topology")
        if len(
            {
                obj.get("bc2_part_index")
                for obj in reopened
            }
        ) != EXPECTED_PARTS:
            raise AssertionError("Saved scene lost AH60 source part metadata")
        reopened_component = bpy.data.collections.get(
            component_collection_name
        )
        reopened_parts = (
            _child(reopened_component, "Parts")
            if reopened_component is not None
            else None
        )
        if reopened_parts is None:
            raise AssertionError("Saved scene lost AH60 Parts collection")
        reopened_by_name = {
            item["bc2_part_name"]: item
            for item in reopened_parts.children
        }
        for part_name, expected_state in EXPECTED_HIDDEN_STATES.items():
            item = reopened_by_name.get(part_name)
            if (
                item is None
                or not item.hide_viewport
                or not item.hide_render
                or item.get("bc2_part_state") != expected_state
                or item.get("bc2_part_default_visible") is not False
                or "bc2_part_transform" not in item
            ):
                raise AssertionError(
                    f"Saved scene lost {part_name} visibility state"
                )
        for part_name in EXPECTED_SINGLE_WRECK_PARTS:
            item = reopened_by_name.get(part_name)
            if (
                item is None
                or item.hide_viewport
                or item.hide_render
                or item.get("bc2_part_state") != "DEFAULT"
                or item.get("bc2_part_default_visible") is not True
            ):
                raise AssertionError(
                    f"Saved scene hid single-Wreck part {part_name}"
                )
        for part_name, expected in EXPECTED_TRANSLATIONS.items():
            _assert_translation(part_name, reopened, expected)
        reopened_double_wreck = [
            obj
            for obj in reopened
            if obj.name.casefold().count("wreck") >= 2
        ]
        if len(reopened_double_wreck) != len(
            double_wreck_objects
        ) or any(
            not obj.hide_viewport
            or not obj.hide_render
            or not obj.hide_get()
            for obj in reopened_double_wreck
        ):
            raise AssertionError(
                "Saved scene lost double-Wreck geometry visibility"
            )
        reopened_visible_single = [
            obj
            for obj in reopened
            if obj.name.casefold().count("wreck") == 1
            and obj.get("bc2_part_name") in EXPECTED_SINGLE_WRECK_PARTS
        ]
        if not reopened_visible_single or any(
            obj.hide_viewport
            or obj.hide_render
            or obj.hide_get()
            or obj.get("bc2_geometry_default_visible") is not True
            for obj in reopened_visible_single
        ):
            raise AssertionError(
                "Saved scene hid geometry in a single-Wreck collection"
            )
        reopened_target = next(
            (
                obj
                for obj in reopened
                if obj.get("bc2_source_subset")
                == "lod0_WreckPartTail_Main"
                and obj.get("bc2_part_name") == "WingLeft"
            ),
            None,
        )
        if (
            reopened_target is None
            or len(reopened_target.data.vertices)
            != EXPECTED_TARGET_VERTICES
            or len(reopened_target.data.polygons)
            != EXPECTED_TARGET_TRIANGLES
            or [
                layer.name
                for layer in reopened_target.data.uv_layers
            ]
            != ["UVMap", "UV1", "UV2", "UV3"]
            or _degenerate_uv_triangles(reopened_target, "UV1")
        ):
            raise AssertionError(
                "Saved scene lost AH60 WingLeft LOD0 or UV data"
            )
        reopened_main = bpy.data.materials.get("BC2 Main")
        if reopened_main is None:
            raise AssertionError("Saved scene lost the AH60 Main material")
        reopened_main_uvs = {
            node.get("bc2_binding_index"): node.get("bc2_uv_map")
            for node in reopened_main.node_tree.nodes
            if node.get("bc2_binding_index") in {1, 2, 3, 4, 5}
        }
        if reopened_main_uvs != main_uvs:
            raise AssertionError(
                "Saved scene lost AH60 Main UV routing: "
                f"{reopened_main_uvs}"
            )
        reopened_minigun = bpy.data.materials.get("BC2 Minigun_Main")
        if reopened_minigun is None:
            raise AssertionError("Saved scene lost the minigun material")
        reopened_minigun_uvs = {
            node.get("bc2_binding_index"): node.get("bc2_uv_map")
            for node in reopened_minigun.node_tree.nodes
            if node.get("bc2_binding_index") in {2, 3, 4}
        }
        if reopened_minigun_uvs != {
            2: "UV1",
            3: "UVMap",
            4: "UVMap",
        }:
            raise AssertionError(
                "Saved scene lost minigun UV routing: "
                f"{reopened_minigun_uvs}"
            )

        metrics = {
            "assembly_components": len(imported.components),
            "part_collections": len(part_collections),
            "part_objects": len(reopened),
            "visible_parts": len(visible_parts),
            "hidden_parts": len(hidden_parts),
            "hidden_double_wreck_objects": len(
                reopened_double_wreck
            ),
            "triangles": reopened_triangles,
            "materials": len(materials),
            "source_images": len(images),
            "output": str(output),
        }
        print("VEHICLE_INTEGRATION " + json.dumps(metrics, sort_keys=True))
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
