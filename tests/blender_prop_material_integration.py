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


SAMPLES = (
    {
        "name": "barrack_interior",
        "path": (
            "Objects/Cutscene_Objects/BarrackInterior/"
            "BarrackInterior_Mesh.res"
        ),
        "archive": "Dist/win32/levels/sp_02/level-00.fbrb",
        "asset_type": "RigidMeshSet",
        "objects": 6,
        "triangles": 613,
        "source_surfaces": 6,
        "assigned_surfaces": {
            "Floor",
            "Glass",
            "Roof",
            "WoodPlanks_NoSoot",
            "Metal_NoSoot",
            "TnTShader12",
        },
        "images": 8,
        "shader_archive": "levels/sp_02/",
        "diagnostics": {"shader_uv_route_unavailable": 3},
    },
    {
        "name": "barrack_objects",
        "path": (
            "Objects/Cutscene_Objects/BarrackInterior/"
            "Objects_Mesh.res"
        ),
        "archive": "Dist/win32/levels/sp_02/level-00.fbrb",
        "asset_type": "RigidMeshSet",
        "objects": 4,
        "triangles": 10_746,
        "source_surfaces": 4,
        "assigned_surfaces": {
            "Multi",
            "MultiMaps",
            "Screen",
            "Shadow",
        },
        "images": 3,
        "shader_archive": "levels/sp_02/",
        "diagnostics": {},
    },
    {
        "name": "barrack_tables",
        "path": (
            "Objects/Cutscene_Objects/BarrackInterior/"
            "Tables_Mesh.res"
        ),
        "archive": "Dist/win32/levels/sp_02/level-00.fbrb",
        "asset_type": "RigidMeshSet",
        "objects": 3,
        "triangles": 5_298,
        "source_surfaces": 3,
        "assigned_surfaces": {
            "ChairMetal",
            "ChairPlastic",
            "Tables",
        },
        "images": 2,
        "shader_archive": "levels/sp_02/",
        "diagnostics": {},
    },
    {
        "name": "medkit",
        "path": (
            "Objects/Weapons/Projectiles/MedKit/"
            "MedKit_Mesh.res"
        ),
        "archive": "Dist/win32/levels/mp_common/level-00.fbrb",
        "asset_type": "RigidMeshSet",
        "objects": 1,
        "triangles": 277,
        "source_surfaces": 1,
        "assigned_surfaces": {"plastic"},
        "images": 2,
        "shader_archive": "levels/mp_001/",
        "diagnostics": {},
    },
    {
        "name": "barn_door",
        "path": (
            "Objects/Buildings/Barn/Barn_Door/"
            "Barn_Door_Mesh.res"
        ),
        "archive": "Dist/win32/levels/mp_common/level-00.fbrb",
        "asset_type": "RigidMeshSet",
        "objects": 2,
        "triangles": 206,
        "source_surfaces": 2,
        "assigned_surfaces": {"Wood_Planks", "Metal"},
        "images": 9,
        "shader_archive": "levels/mp_003/",
        "diagnostics": {},
    },
    {
        "name": "barb_wire",
        "path": (
            "Objects/Cutscene_Objects/BarbWire/"
            "BarbWire_Mesh.res"
        ),
        "archive": "Dist/win32/levels/sp_02/level-00.fbrb",
        "asset_type": "RigidMeshSet",
        "objects": 1,
        "triangles": 2_992,
        "source_surfaces": 1,
        "assigned_surfaces": {"BarbWire1"},
        "images": 0,
        "shader_archive": "levels/sp_02/",
        "diagnostics": {},
    },
    {
        "name": "acacia_tree",
        "path": (
            "Objects/Vegetation/Trees/Acacia_01/"
            "Acacia_01_Mesh.res"
        ),
        "archive": "Dist/win32/levels/mp_common/level-00.fbrb",
        "asset_type": "TreeMeshSet",
        "objects": 3,
        "triangles": 1_750,
        "source_surfaces": 5,
        "assigned_surfaces": {"Bark", "Cut", "Leaves"},
        "images": 6,
        "shader_archive": "levels/sp_006/",
        "diagnostics": {"shader_uv_transform_unsupported": 5},
    },
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
        "PropMaterialSettings",
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


def _diagnostic_counts(root):
    text_name = str(root.get("bc2_material_diagnostics_text", ""))
    text = bpy.data.texts.get(text_name)
    if text is None:
        return {}
    issues = json.loads(text.as_string())["issues"]
    counts = {}
    for issue in issues:
        code = str(issue.get("code", ""))
        counts[code] = counts.get(code, 0) + 1
    return counts


def _assert_import(sample, root, objects):
    if root.get("bc2_asset_path") != sample["path"]:
        raise AssertionError(
            f"{sample['name']} root lost its source mesh-set path"
        )
    if root.get("bc2_assembly_kind") != "MESH_COMPONENT":
        raise AssertionError(
            f"{sample['name']} did not use mesh-set import"
        )
    if int(root.get("bc2_source_surface_count", -1)) != sample[
        "source_surfaces"
    ]:
        raise AssertionError(
            f"{sample['name']} source surface count is incorrect"
        )
    if int(root.get("bc2_resolved_material_count", -1)) != sample[
        "objects"
    ]:
        raise AssertionError(
            f"{sample['name']} did not resolve every assigned material"
        )
    if len(objects) != sample["objects"]:
        raise AssertionError(
            f"{sample['name']} created {len(objects)} objects"
        )
    triangles = sum(len(obj.data.polygons) for obj in objects)
    if triangles != sample["triangles"]:
        raise AssertionError(
            f"{sample['name']} created {triangles} triangles"
        )

    materials = []
    for obj in objects:
        if len(obj.data.materials) != 1:
            raise AssertionError(
                f"{sample['name']} subset has no single material"
            )
        materials.append(obj.data.materials[0])
    surfaces = {
        str(material.get("bc2_surface_name", ""))
        for material in materials
    }
    if surfaces != sample["assigned_surfaces"]:
        raise AssertionError(
            f"{sample['name']} assigned surfaces {sorted(surfaces)}"
        )
    for material in materials:
        status = str(material.get("bc2_material_status", ""))
        shader_source = str(
            material.get("bc2_shader_database_source", "")
        ).replace("\\", "/").casefold()
        if not status.startswith("resolved "):
            raise AssertionError(
                f"{sample['name']} has unresolved material {material.name}"
            )
        if sample["shader_archive"] not in shader_source:
            raise AssertionError(
                f"{sample['name']} used unexpected shader source "
                f"{shader_source}"
            )
        if not material.get("bc2_shader_key"):
            raise AssertionError(
                f"{sample['name']} did not retain its requested shader key"
            )

    images = {
        node.image
        for material in materials
        for node in material.node_tree.nodes
        if node.type == "TEX_IMAGE" and node.image is not None
    }
    if len(images) != sample["images"]:
        raise AssertionError(
            f"{sample['name']} created {len(images)} source images"
        )
    if any(image.source != "FILE" for image in images):
        raise AssertionError(
            f"{sample['name']} did not keep images file-backed"
        )
    if _diagnostic_counts(root) != sample["diagnostics"]:
        raise AssertionError(
            f"{sample['name']} material diagnostics changed"
        )


def _import_sample(repository, sample):
    mesh_set_asset = repository.catalog.find_asset(
        sample["path"],
        (sample["archive"],),
    )
    if mesh_set_asset is None:
        raise AssertionError(
            f"Prop sample is not indexed: {sample['path']}"
        )
    if str(mesh_set_asset["asset_type"]) != sample["asset_type"]:
        raise AssertionError(
            f"{sample['name']} has unexpected resource type"
        )
    descriptor = blender_addon._mesh_set_descriptor_asset(
        repository,
        mesh_set_asset,
    )
    if descriptor is None:
        raise AssertionError(
            f"{sample['name']} has no deterministic descriptor"
        )
    mesh_asset, mesh_raw = repository.read_asset(
        int(mesh_set_asset["id"]),
        max_size=1 << 30,
        resolve_mesh=True,
    )
    parsed = parse_meshdata(
        mesh_raw,
        mesh_asset["platform"],
    )
    root, objects = blender_addon._create_mesh_set_import(
        bpy.context,
        repository,
        mesh_set_asset,
        parsed,
        mesh_asset,
        _settings(),
    )
    _assert_import(sample, root, objects)
    return root.name, str(mesh_asset["path"])


def main():
    arguments = _arguments()
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    repository = AssetRepository(arguments.iso, arguments.cache)
    imported = {}
    try:
        for sample in SAMPLES:
            imported[sample["name"]] = _import_sample(
                repository,
                sample,
            )
    finally:
        repository.close()

    bpy.ops.wm.save_as_mainfile(filepath=str(output))
    bpy.ops.wm.open_mainfile(filepath=str(output))

    result = {}
    for sample in SAMPLES:
        root_name, mesh_data_path = imported[sample["name"]]
        root = bpy.data.collections.get(root_name)
        if root is None:
            raise AssertionError(
                f"{sample['name']} root did not survive save/reopen"
            )
        objects = [
            obj
            for obj in bpy.data.objects
            if str(obj.get("bc2_asset_path", "")).casefold()
            == mesh_data_path.casefold()
        ]
        _assert_import(sample, root, objects)
        result[sample["name"]] = {
            "asset_type": sample["asset_type"],
            "diagnostics": int(
                root.get("bc2_diagnostic_count", 0)
            ),
            "images": int(root.get("bc2_source_image_count", 0)),
            "objects": len(objects),
            "resolved_materials": int(
                root.get("bc2_resolved_material_count", 0)
            ),
            "source_surfaces": int(
                root.get("bc2_source_surface_count", 0)
            ),
            "triangles": sum(
                len(obj.data.polygons) for obj in objects
            ),
        }
    print(
        "PROP_MATERIAL_INTEGRATION "
        + json.dumps(
            {
                "output": str(output),
                "samples": result,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        os._exit(1)
