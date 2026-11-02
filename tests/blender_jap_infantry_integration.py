# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import importlib
import json
import math
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
MESH_PATH = "Characters/Beach/3P/Jap_Infantry/Jap_Infantry_Mesh.res"
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
        "Characters/Beach/3P/Jap_Infantry/FacePose.res",
        46,
        False,
        46,
    ),
    "Jap_Infantry_SkeletonPose": (
        "Characters/Beach/3P/Jap_Infantry/"
        "Jap_Infantry_SkeletonPose.res",
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
        "JapaneseInfantrySettings",
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


def _evaluated_positions(objects):
    dependency_graph = bpy.context.evaluated_depsgraph_get()
    positions = {}
    for obj in objects:
        evaluated = obj.evaluated_get(dependency_graph)
        mesh = evaluated.to_mesh()
        try:
            positions[obj.name] = tuple(
                tuple(vertex.co) for vertex in mesh.vertices
            )
        finally:
            evaluated.to_mesh_clear()
    return positions


def _pose_displacements(reference, posed):
    return tuple(
        math.dist(reference[name][index], posed[name][index])
        for name in reference
        for index in range(len(reference[name]))
    )


def _eyelid_vertex_indices(objects):
    eyelid_bones = {
        "LeftEye",
        "LeftLowerLid",
        "LeftUpperLid",
        "RightEye",
        "RightLowerLid",
        "RightUpperLid",
    }
    result = {}
    for obj in objects:
        group_indices = {
            group.index
            for group in obj.vertex_groups
            if group.name in eyelid_bones
        }
        if not group_indices:
            continue
        indices = tuple(
            vertex.index
            for vertex in obj.data.vertices
            if any(
                assignment.group in group_indices
                and assignment.weight > 0.0
                for assignment in vertex.groups
            )
        )
        if indices:
            result[obj.name] = indices
    return result


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
        mesh_set_asset = repository.catalog.find_asset(
            MESH_PATH,
            (ARCHIVE_PATH,),
        )
        if mesh_set_asset is None:
            raise AssertionError(
                f"Japanese infantry asset is missing: {MESH_PATH}"
            )
        mesh_asset, raw = repository.read_asset(
            int(mesh_set_asset["id"]),
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
            mesh_set_asset,
            parsed,
            mesh_asset,
            _settings(),
        )

        if armature.get("bc2_asset_path") != SKELETON_PATH:
            raise AssertionError(
                "Japanese infantry did not resolve the ske01 armature"
            )
        if len(armature.data.bones) != 147:
            raise AssertionError(
                "Japanese infantry did not import with 147 bones"
            )
        if not objects:
            raise AssertionError(
                "Japanese infantry imported no skinned mesh objects"
            )

        actions = {action.name: action for action in related.actions}
        if set(actions) != set(EXPECTED_ACTIONS):
            raise AssertionError(
                f"Japanese infantry actions are {sorted(actions)}, expected "
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
            expected_additive_count = track_count if additive else 0
            if (
                int(action["bc2_additive_bone_count"])
                != expected_additive_count
            ):
                raise AssertionError(
                    f"{name} additive bone count is incorrect"
                )
            if additive:
                if not bool(action["bc2_additive_baked"]):
                    raise AssertionError(f"{name} was not baked")
                if action["bc2_baked_base_asset_path"] != FACE_POSE_PATH:
                    raise AssertionError(
                        f"{name} used the wrong additive base pose"
                    )

        armature.animation_data.action = actions["DeltaBlink"]
        blink = actions["DeltaBlink"]
        middle_frame = (
            round(
                float(blink["bc2_duration_seconds"])
                / float(blink["bc2_time_step_seconds"])
                / 2.0
            )
            + 1
        )
        bpy.context.scene.frame_set(middle_frame)
        bpy.context.view_layer.update()
        eyelids = (
            "LeftLowerLid",
            "LeftUpperLid",
            "RightLowerLid",
            "RightUpperLid",
        )
        maximum_delta = max(
            max(
                abs(pose_bone.rotation_quaternion.w - 1.0),
                abs(pose_bone.rotation_quaternion.x),
                abs(pose_bone.rotation_quaternion.y),
                abs(pose_bone.rotation_quaternion.z),
                *(abs(value) for value in pose_bone.location),
            )
            for pose_bone in (
                armature.pose.bones[name] for name in eyelids
            )
        )
        if maximum_delta <= 1e-5:
            raise AssertionError(
                "DeltaBlink did not produce an additive eyelid pose"
            )

        armature.animation_data.action = actions["FacePose"]
        bpy.context.scene.frame_set(1)
        bpy.context.view_layer.update()
        face_pose_positions = _evaluated_positions(objects)
        eyelid_indices = _eyelid_vertex_indices(objects)
        if not eyelid_indices:
            raise AssertionError(
                "Japanese infantry exposes no eye/lid weighted vertices"
            )

        dead_pose = actions["dead_pose2_Delta"]
        armature.animation_data.action = dead_pose
        dead_pose_frame = (
            round(
                float(dead_pose["bc2_duration_seconds"])
                / float(dead_pose["bc2_time_step_seconds"])
            )
            + 1
        )
        bpy.context.scene.frame_set(dead_pose_frame)
        bpy.context.view_layer.update()
        dead_pose_positions = _evaluated_positions(objects)
        displacements = _pose_displacements(
            face_pose_positions,
            dead_pose_positions,
        )
        sorted_displacements = sorted(displacements)
        maximum_dead_pose_displacement = sorted_displacements[-1]
        p99_dead_pose_displacement = sorted_displacements[
            min(
                len(sorted_displacements) - 1,
                math.ceil(len(sorted_displacements) * 0.99) - 1,
            )
        ]
        mean_dead_pose_displacement = sum(displacements) / len(
            displacements
        )
        eyelid_displacements = tuple(
            math.dist(
                face_pose_positions[name][index],
                dead_pose_positions[name][index],
            )
            for name, indices in eyelid_indices.items()
            for index in indices
        )
        maximum_eyelid_displacement = max(eyelid_displacements)
        if (
            maximum_dead_pose_displacement >= 0.012
            or p99_dead_pose_displacement >= 0.005
            or mean_dead_pose_displacement >= 0.0004
        ):
            raise AssertionError(
                "dead_pose2_Delta is not composed over FacePose: "
                f"max={maximum_dead_pose_displacement}, "
                f"p99={p99_dead_pose_displacement}, "
                f"mean={mean_dead_pose_displacement}"
            )
        if not 1e-5 < maximum_eyelid_displacement < 0.012:
            raise AssertionError(
                "dead_pose2_Delta eye/lid deformation is implausible: "
                f"{maximum_eyelid_displacement}"
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
            raise AssertionError(
                "Saved scene lost the Japanese infantry armature"
            )
        expected_paths = {
            definition[0] for definition in EXPECTED_ACTIONS.values()
        }
        reopened_actions = {
            action.name: action
            for action in bpy.data.actions
            if action.get("bc2_asset_path", "") in expected_paths
        }
        if set(reopened_actions) != set(EXPECTED_ACTIONS):
            raise AssertionError(
                "Saved scene lost Japanese infantry Actions"
            )
        for name, (
            _,
            track_count,
            additive,
            baked_track_count,
        ) in EXPECTED_ACTIONS.items():
            action = reopened_actions[name]
            if (
                int(action["bc2_track_count"]) != track_count
                or bool(action["bc2_additive"]) is not additive
                or int(action["bc2_baked_track_count"])
                != baked_track_count
            ):
                raise AssertionError(
                    f"Saved {name} metadata is incorrect"
                )

        metrics = {
            "bones": len(reopened_armature.data.bones),
            "mesh_objects": mesh_object_count,
            "actions": dict(sorted(action_counts.items())),
            "maximum_blink_delta": maximum_delta,
            "dead_pose2_maximum_displacement": (
                maximum_dead_pose_displacement
            ),
            "dead_pose2_p99_displacement": p99_dead_pose_displacement,
            "dead_pose2_mean_displacement": mean_dead_pose_displacement,
            "dead_pose2_maximum_eyelid_displacement": (
                maximum_eyelid_displacement
            ),
            "catalog_complete": related.catalog_complete,
            "issues": list(related.issues),
            "output": str(output),
        }
        print(
            "JAP_INFANTRY_INTEGRATION "
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
