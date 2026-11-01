# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import os
import struct
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bc2_frostbite_importer import blender_addon
from bc2_frostbite_importer.core.level import (
    FrostbiteTransform,
    LevelPlacement,
    MeshDefinition,
    SurfaceShader,
)
from bc2_frostbite_importer.core.mesh import MeshAsset, MeshSubset
from bc2_frostbite_importer.core.shaderdb import (
    ShaderDatabase,
    ShaderMaterial,
    ShaderTextureBinding,
)
from bc2_frostbite_importer.core.terrain import TerrainMesh


def _assert_vector(actual, expected):
    if len(actual) != len(expected) or any(
        abs(left - right) >= 1e-5
        for left, right in zip(actual, expected)
    ):
        raise AssertionError(f"{tuple(actual)} != {tuple(expected)}")


def _test_mesh(context):
    subset = MeshSubset(
        name="Accelerated",
        positions=(
            (0.0, 0.0, 0.0),
            (1.0, 2.0, 3.0),
            (0.0, 1.0, 0.0),
        ),
        faces=((0, 1, 2),),
        uvs=((0.1, 0.2), (0.3, 0.4), (0.5, 0.6)),
        influences=(
            ((2, 0.25), (3, 0.75)),
            ((2, 1.0),),
            (),
        ),
        vertex_stride=32,
    )
    parsed = MeshAsset(mesh_type=1, endian=">", subsets=(subset,))
    settings = SimpleNamespace(
        axis_conversion=True,
        unit_scale=2.0,
        flip_v=True,
    )
    asset = {
        "path": "Objects/Test/Accelerated.res",
        "archive_path": "Dist/Test.fbrb",
        "platform": "xbox360",
    }
    objects = blender_addon._create_mesh_objects(
        context, parsed, asset, settings
    )
    if len(objects) != 1:
        raise AssertionError("Accelerated mesh object was not created")
    obj = objects[0]
    mesh = obj.data
    if (len(mesh.vertices), len(mesh.polygons), len(mesh.loops)) != (3, 1, 3):
        raise AssertionError("Accelerated mesh topology is incorrect")
    _assert_vector(mesh.vertices[1].co, (2.0, -6.0, 4.0))
    if not mesh.polygons[0].use_smooth:
        raise AssertionError("Smooth shading was not set")
    uv_values = [tuple(value.vector) for value in mesh.uv_layers[0].uv]
    for actual, expected in zip(
        uv_values,
        ((0.1, 0.8), (0.3, 0.6), (0.5, 0.4)),
    ):
        _assert_vector(actual, expected)
    if abs(obj.vertex_groups["Bone_002"].weight(0) - 0.25) >= 1e-5:
        raise AssertionError("Batched vertex weights are incorrect")
    if abs(obj.vertex_groups["Bone_003"].weight(0) - 0.75) >= 1e-5:
        raise AssertionError("Batched vertex weights are incorrect")


def _test_terrain(context):
    parsed = TerrainMesh(
        bounds=(0.0, 0.0, 0.0, 1.0, 2.0, 1.0),
        source_resolution=2,
        sample_endian="<",
        positions=(
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 2.0),
        ),
        faces=((0, 1, 2),),
    )
    settings = SimpleNamespace(
        axis_conversion=True,
        unit_scale=2.0,
        flip_v=True,
    )
    asset = {
        "path": "Levels/Test/Terrain.res",
        "archive_path": "Dist/Test.fbrb",
    }
    obj = blender_addon._create_terrain_object(
        context, parsed, asset, settings
    )
    _assert_vector(obj.data.vertices[2].co, (0.0, -2.0, 4.0))


def _test_native_image(context):
    with tempfile.TemporaryDirectory(
        prefix="bc2-native-image-"
    ) as temporary:
        repository = SimpleNamespace(source_cache=Path(temporary))
        header = bytearray(92)
        struct.pack_into("<I", header, 8, 10)
        struct.pack_into("<II", header, 16, 2, 2)
        struct.pack_into("<I", header, 28, 1)
        struct.pack_into("<I", header, 32, 4)
        asset = {
            "path": "Textures/Test/L8.res",
            "archive_path": "Dist/Test.fbrb",
            "platform": "pc",
        }
        image, _, converted = blender_addon._import_texture(
            context,
            repository,
            asset,
            bytes(header) + b"\x00\x40\x80\xff",
        )
        if image.source != "FILE":
            raise AssertionError(
                "DDS image is not using Blender's FILE source"
            )
        if tuple(image.size) != (2, 2) or converted.format_name != "L8":
            raise AssertionError("Native DDS image dimensions are incorrect")
        if image.packed_file is not None:
            raise AssertionError(
                "Native DDS import unexpectedly packed the image"
            )
        filepath = Path(bpy.path.abspath(image.filepath))
        if filepath.suffix.casefold() != ".dds":
            raise AssertionError(
                "Native image is not backed by the DDS cache"
            )


def _test_python_fallback():
    mesh = bpy.data.meshes.new("FallbackMesh")
    numpy_module = blender_addon._np
    try:
        blender_addon._np = None
        blender_addon._populate_triangle_mesh(
            mesh,
            (
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
            ),
            ((0, 1, 2),),
            uvs=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
            smooth=True,
        )
    finally:
        blender_addon._np = numpy_module
    if (
        len(mesh.vertices),
        len(mesh.polygons),
        len(mesh.uv_layers),
    ) != (3, 1, 1):
        raise AssertionError("Pure-Python Blender mesh fallback failed")


def _map_transform(x, y, z):
    values = [0.0] * 16
    values[0] = 1.0
    values[5] = 1.0
    values[10] = 1.0
    values[12] = x
    values[13] = y
    values[14] = z
    values[15] = 1.0
    return FrostbiteTransform(tuple(values))


def _test_linked_map_geometry(context):
    subsets = (
        MeshSubset(
            name="Body",
            positions=(
                (0.0, 0.0, 0.0),
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
            ),
            faces=((0, 1, 2),),
            uvs=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
            influences=((), (), ()),
            vertex_stride=32,
            secondary_uvs=((0.1, 0.1), (0.9, 0.1), (0.1, 0.9)),
            uv_offsets=(24, 28),
        ),
        MeshSubset(
            name="Glass",
            positions=(
                (0.0, 0.0, 1.0),
                (1.0, 0.0, 1.0),
                (0.0, 1.0, 1.0),
            ),
            faces=((0, 1, 2),),
            uvs=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
            influences=((), (), ()),
            vertex_stride=32,
            secondary_uvs=((0.2, 0.2), (0.8, 0.2), (0.2, 0.8)),
            uv_offsets=(24, 28),
        ),
    )
    parsed = MeshAsset(mesh_type=1, endian="<", subsets=subsets)
    settings = SimpleNamespace(
        axis_conversion=True,
        unit_scale=2.0,
        flip_v=True,
    )
    definition = MeshDefinition(
        key="mesh",
        reference="Objects/Test/Mesh/GUID",
        name="Objects/Test/Mesh",
        mesh_data_path="Objects/Test/Mesh_lod0_data.res",
        shader_set_reference=None,
        surfaces=(
            SurfaceShader("body", "Shaders/Body/GUID", "Mesh/Shaders/Body"),
            SurfaceShader(
                "glass",
                "Shaders/Glass/GUID",
                "Mesh/Shaders/Glass",
            ),
        ),
    )
    assignments = blender_addon._surface_assignments(parsed, definition)
    if [method for _, method in assignments] != ["name", "name"]:
        raise AssertionError("Map subset-to-surface matching failed")
    naming_mesh = MeshAsset(
        mesh_type=1,
        endian="<",
        subsets=(
            MeshSubset(
                name="lod0_Level01_Env_Leaves",
                positions=subsets[0].positions,
                faces=subsets[0].faces,
                uvs=subsets[0].uvs,
                influences=subsets[0].influences,
                vertex_stride=32,
            ),
            MeshSubset(
                name="export_lod0_ruin_WoodPlanks_NoSoot",
                positions=subsets[1].positions,
                faces=subsets[1].faces,
                uvs=subsets[1].uvs,
                influences=subsets[1].influences,
                vertex_stride=32,
            ),
        ),
    )
    naming_definition = MeshDefinition(
        key="naming",
        reference="Objects/Test/Naming/GUID",
        name="Objects/Test/Naming",
        mesh_data_path="Objects/Test/Naming_lod0_data.res",
        shader_set_reference=None,
        surfaces=(
            SurfaceShader("Leaves", "Shaders/Leaves/GUID", "Mesh/Leaves"),
            SurfaceShader(
                "Env_Leaves",
                "Shaders/EnvLeaves/GUID",
                "Mesh/EnvLeaves",
            ),
            SurfaceShader("Wood", "Shaders/Wood/GUID", "Mesh/Wood"),
            SurfaceShader(
                "WoodPlanks",
                "Shaders/WoodPlanks/GUID",
                "Mesh/WoodPlanks",
            ),
        ),
    )
    naming_assignments = blender_addon._surface_assignments(
        naming_mesh,
        naming_definition,
    )
    if [
        (surface.name, method)
        for surface, method in naming_assignments
    ] != [
        ("Env_Leaves", "suffix"),
        ("WoodPlanks", "contained"),
    ]:
        raise AssertionError("Longest surface-name matching failed")
    mesh = blender_addon._combined_level_mesh(
        parsed,
        "LinkedMapMesh",
        settings,
    )
    if [polygon.material_index for polygon in mesh.polygons] != [0, 1]:
        raise AssertionError("Combined map material indices are incorrect")
    if [layer.name for layer in mesh.uv_layers] != ["UVMap", "UV1"]:
        raise AssertionError("Primary and secondary UV layers were not created")
    collection = bpy.data.collections.new("Linked Map Instances")
    context.scene.collection.children.link(collection)
    placements = (
        LevelPlacement(
            guid="A",
            name="Instance",
            source="reference",
            entity_type="Static",
            reference="Blueprint/A",
            mesh_key="mesh",
            transform=_map_transform(1.0, 2.0, 3.0),
            enumeration=1,
        ),
        LevelPlacement(
            guid="B",
            name="Instance",
            source="reference",
            entity_type="Static",
            reference="Blueprint/B",
            mesh_key="mesh",
            transform=_map_transform(4.0, 5.0, 6.0),
            enumeration=2,
        ),
    )
    objects = [
        blender_addon._placement_object(
            collection,
            placement,
            mesh,
            settings,
        )
        for placement in placements
    ]
    if objects[0].data is not objects[1].data:
        raise AssertionError("Map instances do not share mesh data")
    _assert_vector(objects[0].location, (2.0, -6.0, 4.0))
    _assert_vector(objects[1].location, (8.0, -12.0, 10.0))


def _test_verified_normal_graph():
    material = bpy.data.materials.new("BC2 Normal Graph")
    nodes, links, shader = blender_addon._material_nodes(material)
    image = nodes.new("ShaderNodeTexImage")
    output = blender_addon._dxt5nm_output(
        nodes,
        links,
        image,
        uv_scale=8.0,
        detail_weight=0.4,
    )
    links.new(output, shader.inputs["Normal"])
    if not any(node.type == "NORMAL_MAP" for node in nodes):
        raise AssertionError("DXT5nm graph has no tangent-space transform")
    if not any(
        node.type == "VECT_MATH"
        and node.operation == "NORMALIZE"
        for node in nodes
    ):
        raise AssertionError("DXT5nm graph does not normalize the final normal")
    if not any(
        node.type == "VECT_MATH"
        and node.operation == "MULTIPLY"
        and tuple(node.inputs[1].default_value)[:2] == (8.0, 8.0)
        for node in nodes
    ):
        raise AssertionError("DXT5nm graph does not repeat UVs by eight")
    if not any(
        node.type == "VECT_MATH"
        and node.operation == "SCALE"
        and abs(node.inputs["Scale"].default_value - 0.4) < 1e-6
        for node in nodes
    ):
        raise AssertionError("DXT5nm graph does not apply detail weight 0.4")


def _test_shader_key_alias():
    actual_key = "Objects/Test/Mesh/Objects/Test/Shaders/Wood"
    database = ShaderDatabase(
        materials=(ShaderMaterial(actual_key, ()),),
        trailing_bytes=0,
    )
    material, resolved, match = blender_addon._find_shader_material(
        database,
        "Objects/Test/Mesh/Objects/Test/Shaders/cheap_Wood",
    )
    if material is None or resolved != actual_key or match != "cheap_alias":
        raise AssertionError("Verified cheap_ shader alias was not resolved")
    missing, _, missing_match = blender_addon._find_shader_material(
        database,
        "Objects/Test/Mesh/Objects/Test/Shaders/Missing",
    )
    if missing is not None or missing_match != "missing":
        raise AssertionError("Unknown shader key did not remain unresolved")


def _test_reflected_material_roles():
    bark = (
        ShaderTextureBinding(
            1,
            "Vegetation/Jungle/Textures/Bark_2_Nm",
            ("texture_Texture",),
        ),
        ShaderTextureBinding(
            2,
            "Vegetation/Jungle/Textures/Bark_2",
            ("texture_Texture2",),
        ),
        ShaderTextureBinding(
            3,
            "Textures/TestNoise2",
            ("texture_Texture3",),
        ),
    )
    if blender_addon._binding_roles(bark) != (
        "NORMAL",
        "COLOR",
        "NOISE",
    ):
        raise AssertionError("Paired bark color/normal roles are incorrect")

    layered = (
        ShaderTextureBinding(
            2,
            "Objects/Test/Textures/Test_OcRGB",
            ("texture_Texture",),
        ),
        ShaderTextureBinding(
            3,
            "Textures/_GENERAL/Dt_Dirt_001",
            ("texture_Layer2_Detail2",),
        ),
        ShaderTextureBinding(
            4,
            "Textures/_GENERAL/Dt_ConcreteRough",
            ("texture_Texture3",),
        ),
        ShaderTextureBinding(
            5,
            "Textures/_MASK/Msk_Generic",
            ("texture_Texture5",),
        ),
        ShaderTextureBinding(
            6,
            "Textures/_ATLAS/Decal_Atlas_001",
            ("texture_Texture2",),
        ),
    )
    roles = blender_addon._binding_roles(layered)
    if roles != (
        "OCCLUSION",
        "DETAIL",
        "DETAIL",
        "MASK",
        "DECAL",
    ):
        raise AssertionError("Layered material roles are incorrect")

    material = bpy.data.materials.new("BC2 Layered Graph")
    nodes, links, shader = blender_addon._material_nodes(material)
    texture_nodes = []
    for binding, role in zip(layered, roles):
        node = nodes.new("ShaderNodeTexImage")
        texture_nodes.append((binding, role, node))
    output, _, model = blender_addon._layered_color_output(
        nodes,
        links,
        texture_nodes,
    )
    links.new(output, shader.inputs["Base Color"])
    if not shader.inputs["Base Color"].is_linked:
        raise AssertionError("Layered material has no Base Color graph")
    if "OcRGB-controlled" not in model:
        raise AssertionError("Layered material did not use OcRGB controls")
    if not any(
        node.type == "MIX_RGB" and node.blend_type == "MULTIPLY"
        for node in nodes
    ):
        raise AssertionError("Layered material omitted source occlusion")


def _test_degenerate_subset_materials():
    numpy_module = blender_addon._np
    try:
        for label, module in (
            ("accelerated", numpy_module),
            ("fallback", None),
        ):
            blender_addon._np = module
            mesh = bpy.data.meshes.new(f"DegenerateSubsetMesh_{label}")
            blender_addon._populate_triangle_mesh(
                mesh,
                (
                    (0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                ),
                (
                    (0, 0, 0),
                    (0, 1, 2),
                ),
                material_indices=(0, 1),
            )
            if (
                len(mesh.polygons) != 1
                or mesh.polygons[0].material_index != 1
            ):
                raise AssertionError(
                    f"{label} validation lost subset material indices"
                )
    finally:
        blender_addon._np = numpy_module


def main():
    if blender_addon._np is None:
        raise AssertionError("Blender NumPy is unavailable")
    blender_addon.register()
    blender_addon.unregister()
    _test_mesh(bpy.context)
    _test_terrain(bpy.context)
    _test_native_image(bpy.context)
    _test_python_fallback()
    _test_linked_map_geometry(bpy.context)
    _test_verified_normal_graph()
    _test_shader_key_alias()
    _test_reflected_material_roles()
    _test_degenerate_subset_materials()
    print(
        "PASS Blender mesh, terrain, linked map, material, fallback, and DDS paths",
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
