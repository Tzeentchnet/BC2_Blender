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
    AssetRepository = importlib.import_module(
        package + ".core.repository"
    ).AssetRepository
else:
    sys.path.insert(0, str(ROOT))
    from bc2_frostbite_importer import blender_addon
    from bc2_frostbite_importer.core.assembly import modular_assembly
    from bc2_frostbite_importer.core.dbx import parse_dbx
    from bc2_frostbite_importer.core.repository import AssetRepository


ARCHIVE_PATH = "Dist/win32/levels/mp_common/level-00.fbrb"
ASSEMBLY_PATH = "Objects/Vehicles/Land/BMD3/BMD3.dbx"
TRACK_MESH_PATH = "objects/vehicles/land/bmd3/bmd3tracks_mesh.res"
TRACK_SKELETON_PATH = (
    "objects/vehicles/land/bmd3/bmd3tracks_skeleton.res"
)
EXPECTED_SOURCE_TRANSLATIONS = {
    "Track Right": (
        -1.5676994323730469,
        0.002112865447998047,
        -0.00961768627166748,
    ),
    "Track Left": (
        1.5676989555358887,
        0.002112865447998047,
        -0.00961768627166748,
    ),
}
TRACK_SAMPLE_X = {
    "Objects/Vehicles/Land/BMD3/BMD3.dbx": (
        -1.5676994323730469,
        1.5676989555358887,
    ),
    "Objects/Vehicles/Land/M1A2/M1A2.dbx": (
        -1.6286342144012451,
        1.6131556034088135,
    ),
    "Objects/Vehicles/Land/T90/T90.dbx": (
        -1.6299405097961426,
        1.6129388809204102,
    ),
}


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
        "VehicleTrackSettings",
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


def _blender_translation(source):
    return source[0], -source[2], source[1]


def _assert_near(actual, expected, label):
    if any(
        abs(left - right) >= 1e-5
        for left, right in zip(actual, expected)
    ):
        raise AssertionError(
            f"{label} translation {tuple(actual)} does not match {expected}"
        )


def _track_components(imported):
    return {
        component.definition.name: component
        for component in imported.components
        if component.definition.role == "TRACK"
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

        for sample_path, expected_x in TRACK_SAMPLE_X.items():
            sample_asset = repository.catalog.find_asset(
                sample_path,
                (ARCHIVE_PATH,),
            )
            if sample_asset is None:
                raise AssertionError(
                    f"Track sample is missing: {sample_path}"
                )
            _, sample_raw = repository.read_asset(
                int(sample_asset["id"]),
                max_size=256 << 20,
            )
            sample_assembly = modular_assembly(
                parse_dbx(sample_raw),
                sample_path,
            )
            if sample_assembly is None:
                raise AssertionError(
                    f"Track sample is not a vehicle assembly: {sample_path}"
                )
            sample_tracks = tuple(
                component
                for component in sample_assembly.components
                if component.role == "TRACK"
            )
            if (
                len(sample_tracks) != 2
                or len(
                    {
                        component.mesh_set_path.casefold()
                        for component in sample_tracks
                    }
                )
                != 1
                or any(
                    component.skeleton_reference is None
                    or component.transform is None
                    for component in sample_tracks
                )
            ):
                raise AssertionError(
                    f"{sample_path} track instances are incomplete"
                )
            actual_x = tuple(
                sorted(
                    component.transform.translation[0]
                    for component in sample_tracks
                )
            )
            _assert_near(actual_x, expected_x, sample_path)

        assembly_asset = repository.catalog.find_asset(
            ASSEMBLY_PATH,
            (ARCHIVE_PATH,),
        )
        if assembly_asset is None:
            raise AssertionError(
                f"BMD3 assembly is missing: {ASSEMBLY_PATH}"
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
            raise AssertionError("BMD3 DBX was not resolved as a vehicle")
        track_definitions = {
            component.name: component
            for component in assembly.components
            if component.role == "TRACK"
        }
        if set(track_definitions) != set(EXPECTED_SOURCE_TRANSLATIONS):
            raise AssertionError(
                f"BMD3 track definitions changed: {set(track_definitions)}"
            )
        for name, source_translation in (
            EXPECTED_SOURCE_TRANSLATIONS.items()
        ):
            component = track_definitions[name]
            if (
                component.mesh_set_path.casefold() != TRACK_MESH_PATH
                or component.skeleton_reference is None
                or component.skeleton_reference.rsplit("/", 1)[0].casefold()
                + ".res"
                != TRACK_SKELETON_PATH
                or component.transform is None
            ):
                raise AssertionError(
                    f"{name} lost its source mesh, skeleton, or transform"
                )
            _assert_near(
                component.transform.translation,
                source_translation,
                f"{name} source",
            )

        imported = blender_addon._create_modular_assembly(
            bpy.context,
            repository,
            assembly_asset,
            assembly,
            _settings(),
        )
        if imported.armature is not None:
            raise AssertionError(
                "BMD3 import assigned a fabricated global vehicle armature"
            )
        if (
            len(imported.components) != 3
            or imported.root_collection["bc2_component_count"] != 3
            or imported.root_collection["bc2_track_component_count"] != 2
        ):
            raise AssertionError(
                "BMD3 did not resolve one body and two track instances"
            )
        tracks = _track_components(imported)
        if set(tracks) != set(EXPECTED_SOURCE_TRANSLATIONS):
            raise AssertionError("Imported BMD3 track instances are missing")

        source_guids = set()
        for name, source_translation in (
            EXPECTED_SOURCE_TRANSLATIONS.items()
        ):
            component = tracks[name]
            armature = component.armature
            if armature is None or armature.type != "ARMATURE":
                raise AssertionError(f"{name} has no source-backed track rig")
            if (
                armature.get("bc2_bone_count") != 8
                or str(
                    armature.get("bc2_asset_path", "")
                ).casefold()
                != TRACK_SKELETON_PATH
            ):
                raise AssertionError(f"{name} track skeleton is incorrect")
            _assert_near(
                armature.matrix_world.translation,
                _blender_translation(source_translation),
                name,
            )
            if len(component.objects) != 1:
                raise AssertionError(
                    f"{name} imported {len(component.objects)} mesh objects"
                )
            obj = component.objects[0]
            if (
                len(obj.data.vertices) != 1138
                or len(obj.data.polygons) != 1096
                or obj.parent != armature
                or not any(
                    modifier.type == "ARMATURE"
                    and modifier.object == armature
                    for modifier in obj.modifiers
                )
                or len(obj.vertex_groups) == 0
                or json.loads(obj.data["bc2_uv_offsets"])
                != [40, 44, 32, 36]
            ):
                raise AssertionError(
                    f"{name} track geometry or rig binding is incomplete"
                )
            source_guid = component.collection.get(
                "bc2_component_source_guid"
            )
            if (
                not source_guid
                or component.collection.get("bc2_component_role") != "TRACK"
                or str(
                    component.collection.get(
                        "bc2_component_skeleton_path",
                        "",
                    )
                ).casefold()
                != TRACK_SKELETON_PATH
                or len(
                    component.collection.get(
                        "bc2_component_transform",
                        (),
                    )
                )
                != 16
            ):
                raise AssertionError(
                    f"{name} source component metadata is incomplete"
                )
            source_guids.add(source_guid)
        if len(source_guids) != 2:
            raise AssertionError(
                "BMD3 track instances do not retain distinct source GUIDs"
            )

        bpy.ops.wm.save_as_mainfile(
            filepath=str(output),
            check_existing=False,
        )
        bpy.ops.wm.open_mainfile(filepath=str(output), load_ui=False)

        reopened_armatures = {
            obj.get("bc2_component_source_guid"): obj
            for obj in bpy.data.objects
            if obj.type == "ARMATURE"
            and obj.get("bc2_assembly_path") == ASSEMBLY_PATH
            and obj.get("bc2_component_role") == "TRACK"
        }
        reopened_tracks = [
            obj
            for obj in bpy.data.objects
            if obj.type == "MESH"
            and obj.get("bc2_assembly_path") == ASSEMBLY_PATH
            and obj.get("bc2_component_role") == "TRACK"
        ]
        if (
            len(reopened_armatures) != 2
            or len(reopened_tracks) != 2
            or {
                obj.get("bc2_component_source_guid")
                for obj in reopened_tracks
            }
            != set(reopened_armatures)
        ):
            raise AssertionError(
                "Saved scene lost a BMD3 track mesh or source-backed rig"
            )
        for obj in reopened_tracks:
            armature = reopened_armatures[
                obj.get("bc2_component_source_guid")
            ]
            if (
                obj.parent != armature
                or len(obj.data.vertices) != 1138
                or len(obj.data.polygons) != 1096
            ):
                raise AssertionError(
                    "Saved scene lost BMD3 track geometry placement"
                )

        metrics = {
            "assembly_components": len(imported.components),
            "track_instances": len(reopened_tracks),
            "track_armatures": len(reopened_armatures),
            "track_bones_each": 8,
            "track_vertices_each": 1138,
            "track_triangles_each": 1096,
            "verified_track_assemblies": len(TRACK_SAMPLE_X),
            "source_translations": EXPECTED_SOURCE_TRANSLATIONS,
            "output": str(output),
        }
        print(
            "VEHICLE_TRACK_INTEGRATION "
            + json.dumps(metrics, sort_keys=True)
        )
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
