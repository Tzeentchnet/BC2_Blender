# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bc2_frostbite_importer import blender_addon
from bc2_frostbite_importer.core.granny import parse_granny_animation
from bc2_frostbite_importer.core.repository import AssetRepository


ANIMATION_PATH = (
    "Objects/Vehicles/Beach/Stationary_HMG_30cal/animations/fire.res"
)


def _arguments():
    values = sys.argv
    values = values[values.index("--") + 1 :] if "--" in values else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args(values)


def _settings():
    return SimpleNamespace(axis_conversion=True, unit_scale=1.0)


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
                track.scale_shear.evaluate(time),
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
            animation.duration,
            sample_index * animation.time_step,
        )
        bpy.context.scene.frame_set(sample_index + 1)
        bpy.context.view_layer.update()
        expected = _expected_pose_matrices(
            skeleton,
            animation,
            time,
            settings,
        )
        for index, bone in enumerate(skeleton.bones):
            maximum_error = max(
                maximum_error,
                _matrix_error(
                    armature.pose.bones[bone.name].matrix,
                    expected[index],
                ),
            )
    if maximum_error >= 2e-4:
        raise AssertionError(
            f"BF1943 pose matrix error is {maximum_error}"
        )
    return maximum_error


def main():
    arguments = _arguments()
    output = Path(arguments.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    repository = AssetRepository(arguments.source, arguments.cache)
    try:
        if repository.needs_scan():
            failures = repository.scan()
            if failures:
                raise AssertionError(
                    f"BF1943 scan had {len(failures)} failure(s)"
                )
        animation_asset = repository.catalog.find_asset(ANIMATION_PATH)
        if animation_asset is None:
            raise AssertionError(
                f"BF1943 animation is not indexed: {ANIMATION_PATH}"
            )
        resource, raw = repository.read_asset(
            int(animation_asset["id"]),
            max_size=256 << 20,
        )
        animation = parse_granny_animation(
            raw,
            resource["platform"],
            Path(resource["path"]).stem,
        )
        skeleton_asset, skeleton = blender_addon._read_referenced_skeleton(
            repository,
            resource,
        )
        settings = _settings()
        armature, action = blender_addon._import_granny_animation(
            bpy.context,
            repository,
            resource,
            raw,
            settings,
        )

        if len(skeleton.bones) != 9 or len(animation.tracks) != 9:
            raise AssertionError(
                "BF1943 stationary HMG animation did not resolve its "
                "nine-bone skeleton"
            )
        if {
            curve.codec
            for track in animation.tracks
            for curve in (
                track.rotation,
                track.translation,
                track.scale_shear,
            )
        } != {"DaKeyframes32f"}:
            raise AssertionError(
                "BF1943 sampled-float animation codec coverage changed"
            )
        if armature.get("bc2_asset_path") != skeleton_asset["path"]:
            raise AssertionError(
                "BF1943 animation resolved the wrong skeleton armature"
            )
        if action.get("bc2_asset_path") != resource["path"]:
            raise AssertionError("BF1943 Action source metadata is missing")
        sample_count = (
            round(animation.duration / animation.time_step) + 1
        )
        if (
            int(action["bc2_track_count"]) != len(animation.tracks)
            or bpy.context.scene.frame_end != sample_count
        ):
            raise AssertionError(
                "BF1943 Action track or frame metadata is incorrect"
            )
        maximum_error = _validate_pose_samples(
            armature,
            skeleton,
            animation,
            action,
            settings,
        )
        bpy.ops.wm.save_as_mainfile(
            filepath=str(output),
            check_existing=False,
        )
    finally:
        repository.close()

    bpy.ops.wm.open_mainfile(filepath=str(output), load_ui=False)
    reopened_armature = next(
        (
            obj
            for obj in bpy.data.objects
            if obj.type == "ARMATURE"
            and obj.get("bc2_asset_path") == skeleton_asset["path"]
        ),
        None,
    )
    reopened_action = next(
        (
            candidate
            for candidate in bpy.data.actions
            if candidate.get("bc2_asset_path") == ANIMATION_PATH
        ),
        None,
    )
    if reopened_armature is None or reopened_action is None:
        raise AssertionError(
            "Saved BF1943 armature or Action did not survive reopen"
        )
    reopened_armature.animation_data_create()
    reopened_armature.animation_data.action = reopened_action
    bpy.context.scene.frame_set(sample_count // 2 + 1)
    bpy.context.view_layer.update()
    if any(
        not math.isfinite(value)
        for pose_bone in reopened_armature.pose.bones
        for row in pose_bone.matrix
        for value in row
    ):
        raise AssertionError("Reopened BF1943 Action produced a non-finite pose")

    print(
        "BF1943_ANIMATION_INTEGRATION "
        + json.dumps(
            {
                "animation": ANIMATION_PATH,
                "skeleton": skeleton_asset["path"],
                "bones": len(skeleton.bones),
                "tracks": len(animation.tracks),
                "samples": sample_count,
                "maximum_pose_matrix_error": maximum_error,
                "output": str(output),
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
