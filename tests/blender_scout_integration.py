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


ARCHIVE_PATH = "Dist/win32/levels/sp_01/level-00.fbrb"
SCOUT_PATH = "Characters/Beach/3P/Us_Scout/US_Scout_Mesh.res"
SKELETON_PATH = "Characters/Skeletons/ske01.res"
EXPECTED_ACTIONS = {
    "DeltaBlink": (
        "Characters/Animations/3P/DeltaBlink.res",
        4,
        True,
        46,
    ),
    "DeltaEyeIdle": (
        "Characters/Animations/3P/DeltaEyeIdle.res",
        6,
        True,
        46,
    ),
    "FacePose": (
        "Characters/Beach/3P/Us_Scout/FacePose.res",
        46,
        False,
        46,
    ),
    "Us_Scout_SkeletonPose": (
        "Characters/Beach/3P/Us_Scout/Us_Scout_SkeletonPose.res",
        147,
        False,
        147,
    ),
    "CrouchIdle": (
        "Characters/Animations/BSet_Rif/3P/CrouchIdle.res",
        39,
        False,
        39,
    ),
    "RunBackward": (
        "Characters/Animations/BSet_Rif/3P/RunBackward.res",
        39,
        False,
        39,
    ),
    "RunForward": (
        "Characters/Animations/BSet_Rif/3P/RunForward.res",
        39,
        False,
        39,
    ),
    "SneakForward": (
        "Characters/Animations/BSet_Rif/3P/SneakForward.res",
        39,
        False,
        39,
    ),
    "StandIdle": (
        "Characters/Animations/BSet_Rif/3P/StandIdle.res",
        39,
        False,
        39,
    ),
    "StandRelaxedIdle": (
        "Characters/Animations/BSet_Rif/3P/StandRelaxedIdle.res",
        39,
        False,
        39,
    ),
    "WalkBackward": (
        "Characters/Animations/BSet_Rif/3P/WalkBackward.res",
        39,
        False,
        39,
    ),
    "WalkForward": (
        "Characters/Animations/BSet_Rif/3P/WalkForward.res",
        39,
        False,
        39,
    ),
    "dead_pose1_Delta": (
        "Animations/Faces/Generic/dead_pose1_Delta.res",
        21,
        True,
        46,
    ),
    "dead_pose2_Delta": (
        "Animations/Faces/Generic/dead_pose2_Delta.res",
        14,
        True,
        46,
    ),
}
FACE_POSE_PATH = EXPECTED_ACTIONS["FacePose"][0]


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
        "ScoutSettings",
        (),
        {
            "axis_conversion": True,
            "unit_scale": 1.0,
            "flip_v": True,
            "include_z_only": False,
            "material_mode": "PLACEHOLDER",
            "composite_visibility": "DEFAULT_STATE",
            "create_diagnostics": False,
            "import_related_animations": True,
            "import_core_gameplay_animations": True,
        },
    )()


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
        repository.index_archive_path(ARCHIVE_PATH)
        scout_asset = repository.catalog.find_asset(
            SCOUT_PATH,
            (ARCHIVE_PATH,),
        )
        if scout_asset is None:
            raise AssertionError(f"Scout asset is missing: {SCOUT_PATH}")
        mesh_asset, raw = repository.read_asset(
            int(scout_asset["id"]),
            max_size=1 << 30,
            resolve_mesh=True,
        )
        parsed = parse_meshdata(
            raw,
            mesh_asset["platform"],
            include_z_only=False,
        )
        armature, objects, related = blender_addon._create_character(
            bpy.context,
            repository,
            scout_asset,
            parsed,
            mesh_asset,
            _settings(),
        )

        if armature.get("bc2_asset_path") != SKELETON_PATH:
            raise AssertionError("Scout did not resolve the ske01 armature")
        if len(armature.data.bones) != 147:
            raise AssertionError("Scout did not import with 147 bones")
        if not objects:
            raise AssertionError("Scout imported no skinned mesh objects")
        actions = {action.name: action for action in related.actions}
        if set(actions) != set(EXPECTED_ACTIONS):
            raise AssertionError(
                f"Scout actions are {sorted(actions)}, expected "
                f"{sorted(EXPECTED_ACTIONS)}"
            )
        for name, (
            path,
            track_count,
            additive,
            baked_track_count,
        ) in EXPECTED_ACTIONS.items():
            action = actions[name]
            if action["bc2_asset_path"] != path:
                raise AssertionError(f"{name} source path is incorrect")
            if int(action["bc2_track_count"]) != track_count:
                raise AssertionError(f"{name} track count is incorrect")
            if bool(action["bc2_additive"]) is not additive:
                raise AssertionError(f"{name} additive metadata is incorrect")
            if bool(action["bc2_source_additive"]) is not additive:
                raise AssertionError(
                    f"{name} source-additive metadata is incorrect"
                )
            if int(action["bc2_baked_track_count"]) != baked_track_count:
                raise AssertionError(
                    f"{name} baked track count is incorrect"
                )
            if additive:
                if not bool(action["bc2_additive_baked"]):
                    raise AssertionError(f"{name} was not baked")
                if action["bc2_baked_base_asset_path"] != FACE_POSE_PATH:
                    raise AssertionError(
                        f"{name} used the wrong additive base pose"
                    )
        action_counts = {
            name: int(action["bc2_track_count"])
            for name, action in actions.items()
        }
        mesh_object_count = len(objects)

        bpy.ops.wm.save_as_mainfile(
            filepath=str(output),
            check_existing=False,
        )
        bpy.ops.wm.open_mainfile(filepath=str(output), load_ui=False)
        reopened_armature = next(
            (
                obj
                for obj in bpy.data.objects
                if obj.type == "ARMATURE"
                and obj.get("bc2_asset_path") == SKELETON_PATH
            ),
            None,
        )
        if reopened_armature is None:
            raise AssertionError("Saved scene lost the Scout armature")
        reopened_actions = {
            action.name
            for action in bpy.data.actions
            if action.get("bc2_asset_path", "")
            in {definition[0] for definition in EXPECTED_ACTIONS.values()}
        }
        if reopened_actions != set(EXPECTED_ACTIONS):
            raise AssertionError("Saved scene lost Scout Actions")

        metrics = {
            "bones": len(reopened_armature.data.bones),
            "mesh_objects": mesh_object_count,
            "actions": dict(sorted(action_counts.items())),
            "catalog_complete": related.catalog_complete,
            "issues": list(related.issues),
            "output": str(output),
        }
        print("SCOUT_INTEGRATION " + json.dumps(metrics, sort_keys=True))
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
