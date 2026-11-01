# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import traceback
from pathlib import Path

import bpy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bc2_frostbite_importer import blender_addon
from bc2_frostbite_importer.core.granny import (
    parse_granny_animation,
)
from bc2_frostbite_importer.core.mesh import parse_meshdata
from bc2_frostbite_importer.core.repository import AssetRepository


ARCHIVE_PATH = "Dist/win32/levels/mp_common/level-00.fbrb"
BODY_PATH = "Characters/US/US_Models/US_01_Body_Mesh.res"
ANIMATION_PATHS = (
    "Characters/Animations/BSet_Rif/3P/StandIdle.res",
    "Characters/Animations/BSet_Rif/3P/RunForward.res",
)


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
        "CharacterSettings",
        (),
        {
            "axis_conversion": True,
            "unit_scale": 1.0,
            "flip_v": True,
            "include_z_only": False,
        },
    )()


def _matrix_error(first, second):
    return max(
        abs(first[row][column] - second[row][column])
        for row in range(4)
        for column in range(4)
    )


def _expected_pose_matrices(skeleton, animation, time, settings):
    tracks = {track.name: track for track in animation.tracks}
    matrices = []
    for bone in skeleton.bones:
        track = tracks.get(bone.name)
        if track is None:
            local = blender_addon._source_transform_matrix(
                bone.translation,
                bone.rotation,
                bone.scale_shear,
            )
        else:
            local = blender_addon._source_transform_matrix(
                track.translation.evaluate(time),
                track.rotation_at(time),
            )
        local = blender_addon._convert_skeleton_matrix(local, settings)
        if bone.parent_index < 0:
            matrices.append(local)
        else:
            matrices.append(matrices[bone.parent_index] @ local)
    return matrices


def _validate_pose_samples(
    armature,
    skeleton,
    animation,
    action,
    settings,
):
    armature.animation_data.action = action
    interval_count = round(animation.duration / animation.time_step)
    sample_indices = sorted({0, interval_count // 2, interval_count})
    maximum_error = 0.0
    for sample_index in sample_indices:
        time = min(
            animation.duration, sample_index * animation.time_step
        )
        bpy.context.scene.frame_set(sample_index + 1)
        bpy.context.view_layer.update()
        expected = _expected_pose_matrices(
            skeleton, animation, time, settings
        )
        for index, bone in enumerate(skeleton.bones):
            error = _matrix_error(
                armature.pose.bones[bone.name].matrix,
                expected[index],
            )
            maximum_error = max(maximum_error, error)
    if maximum_error >= 2e-4:
        raise AssertionError(
            f"{animation.name} pose matrix error is {maximum_error}"
        )
    return maximum_error


def _evaluated_bounds(obj):
    dependency_graph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(dependency_graph)
    mesh = evaluated.to_mesh()
    try:
        coordinates = [
            component
            for vertex in mesh.vertices
            for component in vertex.co
        ]
        if not coordinates or any(
            not math.isfinite(value) for value in coordinates
        ):
            raise AssertionError(f"{obj.name} has invalid evaluated vertices")
        return min(coordinates), max(coordinates)
    finally:
        evaluated.to_mesh_clear()


def main():
    arguments = _arguments()
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    repository = AssetRepository(arguments.iso, arguments.cache)
    try:
        if repository.needs_scan():
            repository.scan()
        archive = repository.catalog.archive_by_path(ARCHIVE_PATH)
        if archive is None:
            raise AssertionError(f"PC archive is missing: {ARCHIVE_PATH}")
        if not archive["indexed"]:
            repository.index_archive(int(archive["id"]))

        body_asset = repository.catalog.find_asset(
            BODY_PATH, (ARCHIVE_PATH,)
        )
        if body_asset is None:
            raise AssertionError(f"Body asset is missing: {BODY_PATH}")
        mesh_asset, mesh_raw = repository.read_asset(
            int(body_asset["id"]),
            max_size=1 << 30,
            resolve_mesh=True,
        )
        parsed_mesh = parse_meshdata(
            mesh_raw,
            mesh_asset["platform"],
            include_z_only=False,
        )
        settings = _settings()
        skeleton_asset, skeleton = blender_addon._read_referenced_skeleton(
            repository, body_asset
        )
        armature, mesh_objects = blender_addon._create_character(
            bpy.context,
            repository,
            body_asset,
            parsed_mesh,
            mesh_asset,
            settings,
        )
        armature.show_in_front = True
        armature.data.display_type = "OCTAHEDRAL"

        if len(skeleton.bones) != 147 or len(armature.data.bones) != 147:
            raise AssertionError("US_01 did not import with 147 source bones")
        if len(mesh_objects) != 13:
            raise AssertionError(
                f"US_01 imported {len(mesh_objects)} subsets instead of 13"
            )
        vertex_count = sum(len(obj.data.vertices) for obj in mesh_objects)
        triangle_count = sum(len(obj.data.polygons) for obj in mesh_objects)
        if (vertex_count, triangle_count) != (10_513, 9_883):
            raise AssertionError(
                f"US_01 topology is {(vertex_count, triangle_count)}"
            )
        skeleton_names = {bone.name for bone in skeleton.bones}
        for obj in mesh_objects:
            if any(
                group.name not in skeleton_names
                for group in obj.vertex_groups
            ):
                raise AssertionError(
                    f"{obj.name} has a non-skeleton vertex group"
                )
            modifiers = [
                modifier
                for modifier in obj.modifiers
                if modifier.type == "ARMATURE"
            ]
            if len(modifiers) != 1 or modifiers[0].object != armature:
                raise AssertionError(f"{obj.name} is not bound to US_01")
        pouches = next(
            obj for obj in mesh_objects if "Pouches" in obj.name
        )
        if len(pouches.vertex_groups) <= 1:
            raise AssertionError(
                "One-influence Pouches vertices ignored their palette indices"
            )

        armature.animation_data_create()
        armature.animation_data.action = None
        bpy.context.scene.frame_set(1)
        bpy.context.view_layer.update()
        rest_error = 0.0
        for obj in mesh_objects:
            dependency_graph = bpy.context.evaluated_depsgraph_get()
            evaluated = obj.evaluated_get(dependency_graph)
            evaluated_mesh = evaluated.to_mesh()
            try:
                for source, deformed in zip(
                    obj.data.vertices, evaluated_mesh.vertices
                ):
                    rest_error = max(
                        rest_error, (source.co - deformed.co).length
                    )
            finally:
                evaluated.to_mesh_clear()
        if rest_error >= 1e-5:
            raise AssertionError(
                f"Armature rest pose deforms the body by {rest_error}"
            )

        actions = {}
        pose_errors = {}
        animations = {}
        for animation_path in ANIMATION_PATHS:
            animation_asset = repository.catalog.find_asset(
                animation_path, (ARCHIVE_PATH,)
            )
            if animation_asset is None:
                raise AssertionError(
                    f"Animation asset is missing: {animation_path}"
                )
            asset, raw = repository.read_asset(
                int(animation_asset["id"]), max_size=256 << 20
            )
            animation = parse_granny_animation(
                raw,
                asset["platform"],
                Path(asset["path"]).stem,
            )
            imported_armature, action = (
                blender_addon._import_granny_animation(
                    bpy.context,
                    repository,
                    asset,
                    raw,
                    settings,
                )
            )
            if imported_armature != armature:
                raise AssertionError(
                    f"{animation.name} created a duplicate armature"
                )
            expected_frames = (
                round(animation.duration / animation.time_step) + 1
            )
            if tuple(round(value) for value in action.frame_range) != (
                1,
                expected_frames,
            ):
                raise AssertionError(
                    f"{animation.name} frame range is {tuple(action.frame_range)}"
                )
            actions[animation.name] = action
            animations[animation.name] = animation
            pose_errors[animation.name] = _validate_pose_samples(
                armature,
                skeleton,
                animation,
                action,
                settings,
            )
            _evaluated_bounds(mesh_objects[0])

        if set(actions) != {"StandIdle", "RunForward"}:
            raise AssertionError("Expected StandIdle and RunForward Actions")
        armature.animation_data.action = actions["RunForward"]
        bpy.context.scene.frame_set(10)
        bpy.context.view_layer.update()
        bpy.ops.wm.save_as_mainfile(
            filepath=str(output), check_existing=False
        )

        bpy.ops.wm.open_mainfile(filepath=str(output), load_ui=False)
        reopened_armature = next(
            (
                obj
                for obj in bpy.data.objects
                if obj.type == "ARMATURE"
                and obj.get("bc2_bone_count") == 147
            ),
            None,
        )
        if reopened_armature is None:
            raise AssertionError("Saved scene lost the US_01 armature")
        if not {"StandIdle", "RunForward"} <= {
            action.name for action in bpy.data.actions
        }:
            raise AssertionError("Saved scene lost an imported Action")
        reopened_meshes = [
            obj
            for obj in bpy.data.objects
            if obj.type == "MESH"
            and obj.get("bc2_asset_path") == mesh_asset["path"]
        ]
        if len(reopened_meshes) != 13:
            raise AssertionError("Saved scene lost skinned mesh subsets")
        if any(
            not any(
                modifier.type == "ARMATURE"
                and modifier.object == reopened_armature
                for modifier in obj.modifiers
            )
            for obj in reopened_meshes
        ):
            raise AssertionError("Saved scene lost an Armature modifier")

        metrics = {
            "armature_bones": len(reopened_armature.data.bones),
            "mesh_subsets": len(reopened_meshes),
            "vertices": vertex_count,
            "triangles": triangle_count,
            "actions": {
                name: {
                    "duration": animations[name].duration,
                    "frames": round(
                        animations[name].duration
                        / animations[name].time_step
                    )
                    + 1,
                    "tracks": len(animations[name].tracks),
                    "maximum_pose_matrix_error": pose_errors[name],
                }
                for name in sorted(actions)
            },
            "rest_deformation_error": rest_error,
            "output": str(output),
        }
        print("CHARACTER_INTEGRATION " + json.dumps(metrics, sort_keys=True))
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
