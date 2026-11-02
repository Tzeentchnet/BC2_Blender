# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

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
T90_ASSEMBLY_PATH = "Objects/Vehicles/Land/T90/T90.dbx"
T90_MESH_PATH = "objects/vehicles/land/t90/t90_mesh.res"
T90_TRACK_MESH_PATH = "objects/vehicles/land/t90/t90tracks_mesh.res"
T90_EXPECTED_SOURCE_TRANSLATIONS = {
    "Track Right": (
        -1.6299405097961426,
        1.0235121250152588,
        -0.025497138500213623,
    ),
    "Track Left": (
        1.6129388809204102,
        1.0235121250152588,
        -0.025497138500213623,
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
            "material_mode": "PLACEHOLDER",
            "composite_visibility": "DEFAULT_STATE",
            "import_vehicle_assemblies": True,
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


def _import_selected_mesh(repository, mesh_asset, import_assembly):
    settings = _settings()
    settings.assets = (
        SimpleNamespace(
            asset_id=int(mesh_asset["id"]),
            path=str(mesh_asset["path"]),
            asset_type=str(mesh_asset["asset_type"]),
        ),
    )
    settings.active_asset_index = 0
    settings.import_vehicle_assemblies = import_assembly
    settings.diagnostic_count = 0
    settings.status = ""
    reports = []

    class RecordingOperator:
        def report(self, level, message):
            reports.append((set(level), message))

    original_get_repository = blender_addon._get_repository
    original_get_settings = blender_addon._settings
    blender_addon._get_repository = lambda context: repository
    blender_addon._settings = lambda context: settings
    try:
        result = blender_addon.BC2_OT_import_selected.execute(
            RecordingOperator(),
            bpy.context,
        )
    finally:
        blender_addon._get_repository = original_get_repository
        blender_addon._settings = original_get_settings
    if result != {"FINISHED"}:
        raise AssertionError(
            f"Selected mesh import failed: {reports}"
        )
    return settings, reports


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

        bmd3_track_count = len(reopened_tracks)
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)

        t90_mesh_asset = repository.catalog.find_asset(
            T90_MESH_PATH,
            (ARCHIVE_PATH,),
        )
        if t90_mesh_asset is None:
            raise AssertionError(
                f"T90 main mesh is missing: {T90_MESH_PATH}"
            )
        linked_vehicle = (
            blender_addon._vehicle_assembly_for_mesh_resource(
                repository,
                t90_mesh_asset,
            )
        )
        if linked_vehicle is None:
            raise AssertionError(
                "Direct T90_Mesh.res did not resolve its vehicle assembly"
            )
        t90_assembly_asset, t90_assembly = linked_vehicle
        if (
            str(t90_assembly_asset["path"]).casefold()
            != T90_ASSEMBLY_PATH.casefold()
            or t90_assembly.kind != "VEHICLE"
            or not any(
                component.role == "VEHICLE"
                and component.mesh_set_path.casefold()
                == T90_MESH_PATH
                for component in t90_assembly.components
            )
        ):
            raise AssertionError(
                "Direct T90 mesh resolved the wrong vehicle assembly"
            )

        t90_track_asset = repository.catalog.find_asset(
            T90_TRACK_MESH_PATH,
            (ARCHIVE_PATH,),
        )
        if t90_track_asset is None:
            raise AssertionError(
                f"T90 track mesh is missing: {T90_TRACK_MESH_PATH}"
            )
        if (
            blender_addon._vehicle_assembly_for_mesh_resource(
                repository,
                t90_track_asset,
            )
            is not None
        ):
            raise AssertionError(
                "Direct T90Tracks_Mesh.res must remain a focused import"
            )

        _import_selected_mesh(
            repository,
            t90_mesh_asset,
            import_assembly=False,
        )
        if (
            not any(obj.type == "MESH" for obj in bpy.data.objects)
            or any(
                obj.get("bc2_component_role") == "TRACK"
                for obj in bpy.data.objects
            )
        ):
            raise AssertionError(
                "Disabled vehicle promotion did not stay focused"
            )
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)

        operator_settings, reports = _import_selected_mesh(
            repository,
            t90_mesh_asset,
            import_assembly=True,
        )
        t90_roots = [
            collection
            for collection in bpy.data.collections
            if str(
                collection.get("bc2_assembly_path", "")
            ).casefold()
            == T90_ASSEMBLY_PATH.casefold()
            and "bc2_component_count" in collection
        ]
        if (
            len(t90_roots) != 1
            or t90_roots[0]["bc2_component_count"] != 3
            or t90_roots[0]["bc2_track_component_count"] != 2
            or operator_settings.diagnostic_count != 0
            or not any(
                "Imported vehicle assembly" in message
                for _, message in reports
            )
        ):
            raise AssertionError(
                "Selected T90 mesh did not import one body and two tracks"
            )
        t90_component_count = int(
            t90_roots[0]["bc2_component_count"]
        )
        t90_track_definitions = {
            component.name: component
            for component in t90_assembly.components
            if component.role == "TRACK"
        }
        if set(t90_track_definitions) != set(
            T90_EXPECTED_SOURCE_TRANSLATIONS
        ):
            raise AssertionError("Imported T90 track instances are missing")
        expected_by_guid = {}
        for name, source_translation in (
            T90_EXPECTED_SOURCE_TRANSLATIONS.items()
        ):
            definition = t90_track_definitions[name]
            if definition.source_guid is None:
                raise AssertionError(
                    f"{name} has no source component GUID"
                )
            armatures = [
                obj
                for obj in bpy.data.objects
                if obj.type == "ARMATURE"
                and obj.get("bc2_component_source_guid")
                == definition.source_guid
                and obj.get("bc2_component_role") == "TRACK"
            ]
            objects = [
                obj
                for obj in bpy.data.objects
                if obj.type == "MESH"
                and obj.get("bc2_component_source_guid")
                == definition.source_guid
                and obj.get("bc2_component_role") == "TRACK"
            ]
            if (
                len(armatures) != 1
                or len(objects) != 1
            ):
                raise AssertionError(
                    f"{name} has no source-backed T90 track rig"
                )
            armature = armatures[0]
            obj = objects[0]
            _assert_near(
                armature.matrix_world.translation,
                _blender_translation(source_translation),
                f"T90 {name}",
            )
            if (
                len(obj.data.vertices) != 1282
                or len(obj.data.polygons) != 990
                or obj.parent != armature
                or not any(
                    modifier.type == "ARMATURE"
                    and modifier.object == armature
                    for modifier in obj.modifiers
                )
            ):
                raise AssertionError(
                    f"{name} T90 track geometry or rig binding is incomplete"
                )
            expected_by_guid[definition.source_guid] = (
                _blender_translation(source_translation)
            )
        bpy.context.view_layer.update()
        bpy.ops.wm.save_as_mainfile(
            filepath=str(output),
            check_existing=False,
        )
        bpy.ops.wm.open_mainfile(filepath=str(output), load_ui=False)
        bpy.context.view_layer.update()

        reopened_t90_armatures = {
            obj.get("bc2_component_source_guid"): obj
            for obj in bpy.data.objects
            if obj.type == "ARMATURE"
            and str(obj.get("bc2_assembly_path", "")).casefold()
            == T90_ASSEMBLY_PATH.casefold()
            and obj.get("bc2_component_role") == "TRACK"
        }
        reopened_t90_tracks = [
            obj
            for obj in bpy.data.objects
            if obj.type == "MESH"
            and str(obj.get("bc2_assembly_path", "")).casefold()
            == T90_ASSEMBLY_PATH.casefold()
            and obj.get("bc2_component_role") == "TRACK"
        ]
        if (
            len(reopened_t90_armatures) != 2
            or len(reopened_t90_tracks) != 2
        ):
            raise AssertionError(
                "Saved scene lost a promoted T90 track mesh or rig"
            )
        for obj in reopened_t90_tracks:
            guid = obj.get("bc2_component_source_guid")
            armature = reopened_t90_armatures.get(guid)
            if (
                armature is None
                or obj.parent != armature
                or len(obj.data.vertices) != 1282
                or len(obj.data.polygons) != 990
            ):
                raise AssertionError(
                    "Saved scene lost T90 track geometry placement"
                )
            _assert_near(
                obj.matrix_world.translation,
                expected_by_guid[guid],
                "Reopened T90 track",
            )

        metrics = {
            "assembly_components": len(imported.components),
            "track_instances": bmd3_track_count,
            "track_armatures": len(reopened_armatures),
            "track_bones_each": 8,
            "track_vertices_each": 1138,
            "track_triangles_each": 1096,
            "verified_track_assemblies": len(TRACK_SAMPLE_X),
            "source_translations": EXPECTED_SOURCE_TRANSLATIONS,
            "direct_t90_components": t90_component_count,
            "direct_t90_track_instances": len(reopened_t90_tracks),
            "direct_t90_track_vertices_each": 1282,
            "direct_t90_track_triangles_each": 990,
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
