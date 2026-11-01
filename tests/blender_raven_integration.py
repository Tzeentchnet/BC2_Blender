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


ARCHIVE_PATHS = (
    "Dist/win32/levels/sp_common/level-00.fbrb",
    "Dist/win32/levels/sp_003_B/level-00.fbrb",
)
RAVEN_PATH = "Characters/Animals/Raven/Raven_Mesh.res"
EXPECTED_ACTIONS = {
    "CorpseEating",
    "FlyingAway01",
    "FlyingAway01SlightlyRight",
    "FlyingAway02SlightlyLeft",
    "Idle01",
    "Idle04",
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
        "RavenSettings",
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
        for archive_path in ARCHIVE_PATHS:
            repository.index_archive_path(archive_path)

        raven_asset = repository.catalog.find_asset(
            RAVEN_PATH,
            ARCHIVE_PATHS,
        )
        if raven_asset is None:
            raise AssertionError(f"Raven asset is missing: {RAVEN_PATH}")
        mesh_asset, raw = repository.read_asset(
            int(raven_asset["id"]),
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
            raven_asset,
            parsed,
            mesh_asset,
            _settings(),
        )

        if len(armature.data.bones) != 17:
            raise AssertionError("Raven did not import with 17 bones")
        if not objects:
            raise AssertionError("Raven imported no skinned mesh objects")
        if any(
            obj.modifiers["Armature"].object != armature
            for obj in objects
        ):
            raise AssertionError("Raven mesh is not bound to its armature")
        body = next(
            (
                obj
                for obj in objects
                if obj.name.startswith("00_RavenLeftWingShape_Raven_Body")
            ),
            None,
        )
        if body is None:
            raise AssertionError("Raven body subset is missing")
        if [layer.name for layer in body.data.uv_layers] != ["UVMap", "UV1"]:
            raise AssertionError("Raven body UV layers are incorrect")

        actions = {action.name: action for action in related.actions}
        if set(actions) != EXPECTED_ACTIONS:
            raise AssertionError(
                f"Raven actions are {sorted(actions)}, expected "
                f"{sorted(EXPECTED_ACTIONS)}"
            )
        if any(
            int(action["bc2_track_count"]) != 17
            for action in actions.values()
        ):
            raise AssertionError("A Raven Action does not contain 17 tracks")
        for name in ("CorpseEating", "Idle04"):
            armature.animation_data.action = actions[name]
            bpy.context.scene.frame_set(1)
            bpy.context.view_layer.update()
            scale = tuple(armature.pose.bones["exportAnimation"].scale)
            if max(abs(component - 0.8) for component in scale) >= 1e-5:
                raise AssertionError(
                    f"{name} exportAnimation scale is {scale}, expected 0.8"
                )

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
                and obj.get("bc2_asset_path")
                == "Animations/Animals/Raven/skel_Raven.res"
            ),
            None,
        )
        if reopened_armature is None:
            raise AssertionError("Saved scene lost the Raven armature")
        reopened_actions = {
            action.name
            for action in bpy.data.actions
            if action.get("bc2_asset_path", "").startswith(
                "Animations/Animals/Raven/"
            )
        }
        if reopened_actions != EXPECTED_ACTIONS:
            raise AssertionError("Saved scene lost Raven Actions")

        metrics = {
            "bones": len(reopened_armature.data.bones),
            "mesh_objects": len(objects),
            "actions": sorted(reopened_actions),
            "catalog_complete": related.catalog_complete,
            "issues": list(related.issues),
            "output": str(output),
        }
        print("RAVEN_INTEGRATION " + json.dumps(metrics, sort_keys=True))
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
