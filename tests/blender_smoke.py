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
from bc2_frostbite_importer.core.assembly import AssemblyPart
from bc2_frostbite_importer.core.errors import BC2Error
from bc2_frostbite_importer.core.level import (
    FrostbiteTransform,
    LevelPlacement,
    MeshDefinition,
    SurfaceShader,
)
from bc2_frostbite_importer.core.granny import (
    GrannyAnimation,
    GrannyBone,
    GrannyCurve,
    GrannySkeleton,
    GrannyTransformTrack,
)
from bc2_frostbite_importer.core.mesh import (
    CompositePart,
    MeshAsset,
    MeshSubset,
)
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


def _test_settings_registration(context):
    blender_addon.register()
    try:
        if not hasattr(bpy.types.WindowManager, "bc2_settings"):
            raise AssertionError("WindowManager session settings were not registered")
        if hasattr(bpy.types.Scene, "bc2_settings"):
            raise AssertionError("BC2 settings are still stored on Scene")
        diagnostics_property = (
            blender_addon.BC2AddonPreferences.bl_rna.properties[
                "create_diagnostics"
            ]
        )
        if diagnostics_property.default is not False:
            raise AssertionError("Detailed diagnostics must default to disabled")
        preference_animation_property = (
            blender_addon.BC2AddonPreferences.bl_rna.properties[
                "default_import_related_animations"
            ]
        )
        preference_gameplay_property = (
            blender_addon.BC2AddonPreferences.bl_rna.properties[
                "default_import_core_gameplay_animations"
            ]
        )
        session_animation_property = (
            blender_addon.BC2Settings.bl_rna.properties[
                "import_related_animations"
            ]
        )
        session_gameplay_property = (
            blender_addon.BC2Settings.bl_rna.properties[
                "import_core_gameplay_animations"
            ]
        )
        if (
            preference_animation_property.default is not True
            or session_animation_property.default is not True
            or preference_gameplay_property.default is not True
            or session_gameplay_property.default is not True
        ):
            raise AssertionError(
                "Related animation import must default to enabled"
            )
        size_property = blender_addon.BC2Settings.bl_rna.properties[
            "show_asset_sizes"
        ]
        if size_property.default is not False:
            raise AssertionError("Asset file sizes must default to hidden")
        content_group_property = (
            blender_addon.BC2Settings.bl_rna.properties["content_group"]
        )
        if content_group_property.default != "ALL":
            raise AssertionError(
                "Indexed assets must default to all content groups"
            )
        if (
            blender_addon._asset_type_icon("SkinnedMeshSet", "Mesh.res")
            != "MOD_ARMATURE"
            or blender_addon._asset_type_icon(
                "<non-resource>",
                "Levels/Test.dbx",
            )
            != "FILE_TEXT"
        ):
            raise AssertionError("Asset type icons were not classified")
        scan_properties = bpy.ops.bc2.scan_source.get_rna_type().properties
        if not {
            "target_level",
            "target_archive",
            "index_all",
        } <= {item.identifier for item in scan_properties}:
            raise AssertionError("Background scan operator options were not registered")
        indexing_property = scan_properties["index_all"]
        if indexing_property.default is not False:
            raise AssertionError("PC scans must not index every archive by default")

        settings = context.window_manager.bc2_settings
        source_path = str((ROOT / "Synthetic.iso").resolve())
        cache_path = str((ROOT / "SyntheticCache").resolve())
        preferences = SimpleNamespace(
            source_path=source_path,
            cache_directory=cache_path,
            default_axis_conversion=False,
            default_flip_v=False,
            default_unit_scale=2.5,
            default_include_z_only=True,
            default_terrain_resolution=256,
            default_material_mode="MATERIALS_ONLY",
            default_composite_visibility="SHOW_ALL",
            default_import_related_animations=False,
            default_import_core_gameplay_animations=False,
        )
        blender_addon._apply_preference_defaults(
            settings,
            preferences,
            force=True,
        )
        if (
            settings.axis_conversion is not False
            or settings.flip_v is not False
            or settings.unit_scale != 2.5
            or settings.include_z_only is not True
            or settings.terrain_resolution != 256
            or settings.material_mode != "MATERIALS_ONLY"
            or settings.composite_visibility != "SHOW_ALL"
            or settings.import_related_animations is not False
            or settings.import_core_gameplay_animations is not False
        ):
            raise AssertionError("Preference defaults were not copied")
        settings.unit_scale = 7.0
        blender_addon._apply_preference_defaults(settings, preferences)
        if settings.unit_scale != 7.0:
            raise AssertionError("Initialized session overrides were overwritten")
        blender_addon._apply_preference_defaults(
            settings,
            preferences,
            force=True,
        )
        if settings.unit_scale != 2.5:
            raise AssertionError("Forced default reset did not restore scale")

        class SyntheticCatalog:
            def archives(self, query="", limit=1000, offset=0):
                test_archive = {
                    "id": 1,
                    "path": "Dist/Test.fbrb",
                    "indexed": 1,
                    "asset_count": 1,
                    "size": 128,
                    "error": "",
                }
                if query:
                    return (test_archive,)
                return (
                    test_archive,
                    {"path": "Dist/win32/levels/mp_001/level-00.fbrb"},
                    {"path": "Dist/win32/levels/mp_001/terrain-00.fbrb"},
                )

            def asset_count(
                self,
                category="ALL",
                query="",
                content_group="ALL",
            ):
                return 1

            def search_assets(
                self,
                category,
                query,
                limit,
                offset,
                content_group="ALL",
            ):
                return (
                    {
                        "id": 1,
                        "path": "Objects/Test.res",
                        "asset_type": "MeshData",
                        "archive_path": "Dist/Test.fbrb",
                        "size": 64,
                    },
                )

            def archive_count(self, query=""):
                return 3

        class SyntheticRepository:
            kind = "PC_ISO"

            def __init__(self):
                self.catalog = SyntheticCatalog()
                self.scan_calls = 0
                self.index_all_calls = 0
                self.index_path_calls = []
                self.close_calls = 0

            def needs_scan(self):
                return False

            def scan(self, progress, cancelled=None):
                self.scan_calls += 1
                return ()

            def index_all_archives(self, progress, cancelled=None):
                self.index_all_calls += 1
                progress(1, 1, "Dist/Test.fbrb")
                return ()

            def index_archive_paths(self, paths, progress, cancelled=None):
                self.index_path_calls.append(paths)
                for current, path in enumerate(paths, 1):
                    progress(current, len(paths), path)
                return ()

            def close(self):
                self.close_calls += 1

        lightweight_repository = SyntheticRepository()
        targeted_repository = SyntheticRepository()
        repository = SyntheticRepository()
        settings.archive_search = "Test"
        original_asset_repository = blender_addon.AssetRepository
        original_preferences = blender_addon._preferences
        original_get_repository = blender_addon._get_repository
        try:
            blender_addon.AssetRepository = (
                lambda _source_path, _cache_path: lightweight_repository
            )
            lightweight_events = blender_addon.queue.Queue()
            blender_addon._scan_source_worker(
                source_path,
                cache_path,
                False,
                lightweight_events,
                blender_addon.threading.Event(),
            )
            if (
                lightweight_repository.index_all_calls
                or lightweight_repository.index_path_calls
                or lightweight_repository.close_calls != 1
            ):
                raise AssertionError(
                    "Lightweight PC scan indexed unrelated assets"
                )

            target_archives = (
                "Dist/win32/levels/mp_001/level-00.fbrb",
                "Dist/win32/levels/mp_common/level-00.fbrb",
            )
            blender_addon.AssetRepository = (
                lambda _source_path, _cache_path: targeted_repository
            )
            targeted_events = blender_addon.queue.Queue()
            blender_addon._scan_source_worker(
                source_path,
                cache_path,
                False,
                targeted_events,
                blender_addon.threading.Event(),
                target_archives,
            )
            if (
                targeted_repository.index_path_calls != [target_archives]
                or targeted_repository.index_all_calls
                or targeted_repository.close_calls != 1
            ):
                raise AssertionError(
                    "Targeted map preparation indexed the wrong archives"
                )

            blender_addon.AssetRepository = (
                lambda _source_path, _cache_path: repository
            )
            events = blender_addon.queue.Queue()
            blender_addon._scan_source_worker(
                source_path,
                cache_path,
                True,
                events,
                blender_addon.threading.Event(),
            )
            terminal = None
            while not events.empty():
                message = events.get_nowait()
                if message[0] != "progress":
                    terminal = message
            if terminal is None or terminal[0] != "complete":
                raise AssertionError("Background scan did not complete")
            blender_addon._preferences = lambda _context: preferences
            blender_addon._get_repository = (
                lambda _context, require_scan=True: repository
            )
            reports = []
            operator = SimpleNamespace(
                report=lambda severity, message: reports.append(
                    (severity, message)
                )
            )
            blender_addon._apply_scan_result(
                context,
                operator,
                terminal[1],
                cancelled=False,
            )
        finally:
            blender_addon.AssetRepository = original_asset_repository
            blender_addon._preferences = original_preferences
            blender_addon._get_repository = original_get_repository
        if (
            repository.scan_calls
            or repository.index_all_calls != 1
            or repository.close_calls != 1
            or len(settings.archives) != 1
            or len(settings.assets) != 1
            or settings.indexed_asset_count != 1
            or [item.name for item in settings.available_maps] != ["MP_001"]
            or "existing catalog preserved" not in settings.status
        ):
            raise AssertionError("Scan Disc did not repopulate the cached catalog")

        class MapPanelLayout:
            def __init__(self):
                self.search_call = None
                self.enabled = True

            def label(self, *, text, icon):
                pass

            def prop_search(
                self,
                data,
                property_name,
                search_data,
                search_property,
                *,
                text,
            ):
                self.search_call = (
                    data,
                    property_name,
                    search_data,
                    search_property,
                    text,
                )

            def row(self, *, align=False):
                return self

            def operator(self, operator, *, text, icon):
                return SimpleNamespace()

        map_layout = MapPanelLayout()
        original_settings = blender_addon._settings
        try:
            blender_addon._settings = lambda _context: settings
            blender_addon.BC2_PT_map.draw(
                SimpleNamespace(layout=map_layout),
                context,
            )
        finally:
            blender_addon._settings = original_settings
        if (
            map_layout.search_call is None
            or map_layout.search_call[1:] != (
                "map_name",
                settings,
                "available_maps",
                "Level",
            )
        ):
            raise AssertionError("Discovered-map search control was not drawn")
    finally:
        blender_addon.unregister()
    if hasattr(bpy.types.WindowManager, "bc2_settings"):
        raise AssertionError("WindowManager session settings were not unregistered")


def _test_import_policies(context):
    class SyntheticRepository:
        def __init__(self, needs_scan):
            self._needs_scan = needs_scan
            self.scan_calls = 0

        def needs_scan(self):
            return self._needs_scan

        def scan(self, progress, cancelled=None):
            self.scan_calls += 1
            return [("Archive", "Synthetic failure")]

    existing_repository = SyntheticRepository(False)
    failures, scanned = blender_addon._scan_repository(
        existing_repository,
        None,
    )
    if scanned or failures or existing_repository.scan_calls:
        raise AssertionError("A valid catalog was destructively rescanned")
    fresh_repository = SyntheticRepository(True)
    failures, scanned = blender_addon._scan_repository(
        fresh_repository,
        None,
    )
    if (
        not scanned
        or len(failures) != 1
        or fresh_repository.scan_calls != 1
    ):
        raise AssertionError("A new source was not scanned")

    surface = SurfaceShader(
        "PolicySurface",
        "Objects/Test/Shaders/Policy/GUID",
        "Objects/Test/Shaders/Policy",
    )
    diagnostics = []
    material = blender_addon._surface_material(
        None,
        surface,
        None,
        (),
        {},
        {},
        diagnostics,
        False,
    )
    if (
        material.get("bc2_material_status")
        != "resolved source material without textures"
    ):
        raise AssertionError("Materials-only mode did not create a source material")
    if any(node.type == "TEX_IMAGE" for node in material.node_tree.nodes):
        raise AssertionError("Materials-only mode created an image texture node")

    subset = MeshSubset(
        name="PlaceholderSubset",
        positions=((0.0, 0.0, 0.0),),
        faces=(),
        uvs=((0.0, 0.0),),
        influences=((),),
        vertex_stride=12,
    )
    parsed = MeshAsset(mesh_type=1, endian="<", subsets=(subset,))
    materials, issues, summary = blender_addon._resolve_mesh_subset_materials(
        None,
        None,
        parsed,
        (),
        SimpleNamespace(material_mode="PLACEHOLDER"),
    )
    if (
        len(materials) != 1
        or materials[0].get("bc2_material_status") != "placeholder material"
        or issues
        or summary["source_surfaces"] != 0
    ):
        raise AssertionError("Placeholder material mode resolved source data")

    name = "BC2_Synthetic_Diagnostics.json"
    disabled = blender_addon._create_diagnostics_text(
        context,
        SimpleNamespace(create_diagnostics=False),
        name,
        {"issues": [{"code": "synthetic"}]},
    )
    if disabled is not None or bpy.data.texts.get(name) is not None:
        raise AssertionError("Disabled diagnostics created a Text datablock")
    enabled = blender_addon._create_diagnostics_text(
        context,
        SimpleNamespace(create_diagnostics=True),
        name,
        {"issues": [{"code": "synthetic"}]},
    )
    if enabled is None or "synthetic" not in enabled.as_string():
        raise AssertionError("Enabled diagnostics did not create JSON text")
    bpy.data.texts.remove(enabled)


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


def _test_composite_parts(context):
    subset = MeshSubset(
        name="VehicleSurface",
        positions=(
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (2.0, 0.0, 0.0),
            (3.0, 0.0, 0.0),
            (2.0, 1.0, 0.0),
        ),
        faces=((0, 1, 2), (3, 4, 5)),
        uvs=(
            (0.0, 0.0),
            (1.0, 0.0),
            (0.0, 1.0),
            (0.0, 0.0),
            (1.0, 0.0),
            (0.0, 1.0),
        ),
        influences=((), (), (), (), (), ()),
        vertex_stride=32,
        palette=(0, 1),
        part_indices=(0, 0, 0, 1, 1, 1),
        part_index_offset=28,
    )
    wreck_subset = MeshSubset(
        name="lod0_WreckPartTail_Wreck",
        positions=(
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
        ),
        faces=((0, 1, 2),),
        uvs=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        influences=((), (), ()),
        vertex_stride=32,
        palette=(0,),
        part_indices=(1, 1, 1),
        part_index_offset=28,
    )
    main_wreck_subset = MeshSubset(
        name="lod0_WreckPartTail_Main",
        positions=(
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
        ),
        faces=((0, 1, 2),),
        uvs=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        influences=((), (), ()),
        vertex_stride=32,
        palette=(0,),
        part_indices=(1, 1, 1),
        part_index_offset=28,
    )
    parsed = MeshAsset(
        mesh_type=2,
        endian="<",
        subsets=(subset, wreck_subset, main_wreck_subset),
    )
    settings = SimpleNamespace(
        axis_conversion=True,
        unit_scale=2.0,
        flip_v=True,
    )
    asset = {
        "path": "Objects/Vehicles/Test/Test_Mesh_lod0_data.res",
        "archive_path": "Dist/Test.fbrb",
        "platform": "pc",
    }
    root = blender_addon._new_collection(context, "BC2_TestVehicle")
    components = blender_addon._new_child_collection(root, "Components")
    component = blender_addon._new_child_collection(
        components,
        "00_Test_Mesh",
    )
    composite_parts = (
        CompositePart(
            name="Hull",
            bounds=(-1.0, -1.0, -1.0, 1.0, 1.0, 1.0),
            transform=(
                1.0, 0.0, 0.0,
                0.0, 1.0, 0.0,
                0.0, 0.0, 1.0,
                1.0, 2.0, 3.0,
            ),
            source_offset=0,
        ),
        CompositePart(
            name="DoorWreck",
            bounds=(-2.0, -2.0, -2.0, 2.0, 2.0, 2.0),
            transform=(
                0.0, 0.0, 1.0,
                0.0, 1.0, 0.0,
                -1.0, 0.0, 0.0,
                4.0, 5.0, 6.0,
            ),
            source_offset=1,
        ),
    )
    objects = blender_addon._create_mesh_objects(
        context,
        parsed,
        asset,
        settings,
        collection=component,
        split_parts=True,
        composite_parts=composite_parts,
        part_states=(
            AssemblyPart(
                name="Hull",
                state="DEFAULT",
                source="health_state",
                component_type="PartComponentData",
                component_name="Hull",
            ),
        ),
    )
    if len(objects) != 4:
        raise AssertionError("Composite surfaces were not split by part")
    parts_collection = next(
        (child for child in component.children if child.name == "Parts"),
        None,
    )
    if parts_collection is None:
        raise AssertionError("Composite Parts collection was not created")
    if {child.name for child in parts_collection.children} != {
        "000_Hull",
        "001_DoorWreck",
    }:
        raise AssertionError("Composite source part collections are incorrect")
    part_collections = {
        child["bc2_part_name"]: child
        for child in parts_collection.children
    }
    if (
        part_collections["Hull"].hide_viewport
        or part_collections["Hull"].hide_render
    ):
        raise AssertionError("Default composite part is hidden")
    wreck = part_collections["DoorWreck"]
    if wreck.hide_viewport or wreck.hide_render:
        raise AssertionError("Single-Wreck composite part is hidden")
    if wreck["bc2_part_state"] != "DEFAULT":
        raise AssertionError("Single-Wreck part state metadata is incorrect")
    if not wreck["bc2_part_default_visible"]:
        raise AssertionError(
            "Single-Wreck part visibility metadata is incorrect"
        )
    if wreck["bc2_part_state_source"] != "part_name":
        raise AssertionError("Destroyed part state source is incorrect")
    hull_collection = part_collections["Hull"]
    if (
        hull_collection["bc2_part_state_source"] != "health_state"
        or hull_collection["bc2_source_component_type"]
        != "PartComponentData"
        or hull_collection["bc2_source_component_name"] != "Hull"
    ):
        raise AssertionError("DBX part-state metadata is incorrect")
    if (
        "bc2_part_bounds" not in hull_collection
        or "bc2_part_transform" not in hull_collection
    ):
        raise AssertionError("Composite transform metadata is missing")
    if component["bc2_default_visible_part_count"] != 2:
        raise AssertionError("Default-visible part count is incorrect")
    if component["bc2_hidden_part_count"] != 0:
        raise AssertionError("Hidden part count is incorrect")
    for obj in objects:
        if len(obj.data.polygons) != 1:
            raise AssertionError("Composite part topology is not compact")
        expected_name = ("Hull", "DoorWreck")[obj["bc2_part_index"]]
        if obj["bc2_part_name"] != expected_name:
            raise AssertionError("Composite part metadata is incorrect")
    hull = next(obj for obj in objects if obj["bc2_part_name"] == "Hull")
    wreck_obj = next(
        obj for obj in objects if obj["bc2_part_name"] == "DoorWreck"
    )
    _assert_vector(hull.matrix_basis.translation, (2.0, -6.0, 4.0))
    _assert_vector(
        wreck_obj.matrix_basis.translation,
        (8.0, -12.0, 10.0),
    )
    expected_rotation = (
        (0.0, -1.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    for actual, expected in zip(
        wreck_obj.matrix_basis.to_3x3(),
        expected_rotation,
    ):
        _assert_vector(actual, expected)
    source_wreck = next(
        obj
        for obj in objects
        if "wreckparttail_wreck" in obj.name.casefold()
    )
    if not source_wreck.hide_viewport or not source_wreck.hide_render:
        raise AssertionError("Wreck-named geometry is visible by default")
    if source_wreck.get("bc2_geometry_default_visible") is not False:
        raise AssertionError("Wreck geometry visibility metadata is incorrect")
    if source_wreck.get("bc2_part_name") != "DoorWreck":
        raise AssertionError("Double-Wreck geometry used the wrong part")
    if (
        source_wreck.get("bc2_geometry_visibility_source")
        != "name_contains_wreck_twice"
    ):
        raise AssertionError("Wreck geometry hide reason is missing")
    single_wreck = next(
        obj
        for obj in objects
        if "wreckparttail_main" in obj.name.casefold()
    )
    if single_wreck.hide_viewport or single_wreck.hide_render:
        raise AssertionError("Single-Wreck geometry is hidden")
    if single_wreck.hide_get():
        raise AssertionError(
            "Single-Wreck parent collection hides its geometry"
        )
    if single_wreck.get("bc2_part_name") != "DoorWreck":
        raise AssertionError("Single-Wreck geometry used the wrong part")
    if single_wreck.get("bc2_geometry_wreck_name_count") != 1:
        raise AssertionError("Single-Wreck name count is incorrect")
    if single_wreck.get("bc2_geometry_default_visible") is not True:
        raise AssertionError("Single-Wreck geometry is not default-visible")

    settings.composite_visibility = "SHOW_ALL"
    show_all_root = blender_addon._new_collection(
        context,
        "BC2_TestVehicle_ShowAll",
    )
    show_all_component = blender_addon._new_child_collection(
        blender_addon._new_child_collection(show_all_root, "Components"),
        "00_Test_Mesh",
    )
    show_all_objects = blender_addon._create_mesh_objects(
        context,
        parsed,
        asset,
        settings,
        collection=show_all_component,
        split_parts=True,
        composite_parts=composite_parts,
    )
    show_all_parts = next(
        child
        for child in show_all_component.children
        if child.name.startswith("Parts")
    )
    if any(
        child.hide_viewport or child.hide_render
        for child in show_all_parts.children
    ):
        raise AssertionError("Show All hid a composite part collection")
    if any(
        obj.hide_viewport or obj.hide_render or obj.hide_get()
        for obj in show_all_objects
    ):
        raise AssertionError("Show All hid imported composite geometry")
    show_all_wreck = next(
        obj
        for obj in show_all_objects
        if "wreckparttail_wreck" in obj.name.casefold()
    )
    if (
        show_all_wreck.get("bc2_geometry_default_visible") is not False
        or show_all_wreck.get("bc2_geometry_state") != "DESTROYED"
    ):
        raise AssertionError("Show All discarded default visibility metadata")


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
    layered_surface = SurfaceShader(
        "Layered",
        "objects/test/shaders/layered/reference",
        "Objects/Test/Mesh/Objects/Test/Shaders/Layered",
    )
    uv_maps = tuple(
        blender_addon._surface_binding_uv_map(
            layered_surface,
            binding,
            role,
            layered_static=True,
        )
        for binding, role in zip(layered, roles)
    )
    if uv_maps != ("UVMap", "UV1", "UV1", "UVMap", "UV2"):
        raise AssertionError("Layered static UV routing is incorrect")
    repeated_detail = ShaderTextureBinding(
        5,
        "Textures/_GENERAL/Dt_Rust_001",
        ("texture_Layer2_Detail3",),
    )
    selector_detail = ShaderTextureBinding(
        5,
        "Textures/_GENERAL/Dt_ConcreteEdgecracks",
        ("texture_Texture4",),
    )
    if (
        blender_addon._surface_binding_uv_map(
            layered_surface,
            repeated_detail,
            "DETAIL",
            layered_static=True,
        )
        != "UV1"
        or blender_addon._surface_binding_uv_map(
            layered_surface,
            selector_detail,
            "DETAIL",
            layered_static=True,
        )
        != "UV2"
    ):
        raise AssertionError("Layered t5 coordinate routing is incorrect")
    truck_color = ShaderTextureBinding(
        2,
        "Textures/Vehicles/Truck/Truck_c",
        ("texture_Texture7",),
    )
    if (
        blender_addon._surface_binding_uv_map(
            layered_surface,
            truck_color,
            "COLOR",
        )
        != "UV1"
    ):
        raise AssertionError("Reflected vehicle color does not use UV1")
    for texture, scale in (
        ("Textures/_GENERAL/Dt_WireFence_001", 15.0),
        ("Textures/_GENERAL/Dt_RazorWire", 10.0),
    ):
        binding = ShaderTextureBinding(
            1,
            texture,
            ("texture_Texture",),
        )
        if (
            blender_addon._surface_binding_uv_map(
                layered_surface,
                binding,
                "DETAIL",
            )
            != "UV1"
            or blender_addon._surface_binding_uv_scale(
                layered_surface,
                binding,
            )
            != (scale, scale)
        ):
            raise AssertionError(
                f"Verified cutout repeat is incorrect for {texture}"
            )

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


def _test_verified_vehicle_material_graphs():
    main_surface = SurfaceShader(
        "Main",
        (
            "objects/vehicles/air/ah60/shaders/main/"
            "0ff8511e-ef09-4249-a360-f1c598aed430"
        ),
        (
            "Objects/Vehicles/Air/AH60/AH60_Mesh/"
            "Objects/Vehicles/Air/AH60/Shaders/Main"
        ),
    )
    main_bindings = (
        ShaderTextureBinding(
            1,
            "Textures/Vehicles/AH60/Ah60Blackhawk_C",
            ("texture_Texture7",),
        ),
        ShaderTextureBinding(
            2,
            "Textures/Vehicles/UsSymbols/Symbols_Us",
            ("texture_Texture4",),
        ),
        ShaderTextureBinding(
            3,
            "Textures/Vehicles/Dirt/DirtMaskRGB",
            ("texture_Texture2",),
        ),
        ShaderTextureBinding(
            4,
            "Textures/Vehicles/AH60/Ah60Blackhawk_D",
            ("texture_Texture3",),
        ),
        ShaderTextureBinding(
            5,
            "Textures/_MASK/Msk_Generic",
            ("texture_Texture6",),
        ),
    )
    main_roles = blender_addon._surface_binding_roles(
        main_surface,
        main_bindings,
    )
    if main_roles != ("COLOR", "COLOR", "MASK", "DETAIL", "MASK"):
        raise AssertionError("Verified AH60 Main roles are incorrect")
    main_uv_maps = tuple(
        blender_addon._surface_binding_uv_map(
            main_surface,
            binding,
            role,
        )
        for binding, role in zip(main_bindings, main_roles)
    )
    if main_uv_maps != ("UV1", "UV2", "UV3", "UVMap", "UV1"):
        raise AssertionError("Verified AH60 Main UV routing is incorrect")
    if blender_addon._surface_binding_uv_scale(
        main_surface,
        main_bindings[4],
    ) != (15.0, 15.0):
        raise AssertionError("AH60 Main generic mask is not repeated by 15")

    material = bpy.data.materials.new("BC2 Verified AH60 Main")
    nodes, links, shader = blender_addon._material_nodes(material)
    texture_nodes = []
    for binding, role in zip(main_bindings, main_roles):
        node = nodes.new("ShaderNodeTexImage")
        node.name = f"t{binding.index}"
        texture_nodes.append((binding, role, node))
    if not blender_addon._configure_verified_surface_material(
        material,
        main_surface,
        nodes,
        links,
        shader,
        texture_nodes,
    ):
        raise AssertionError("Verified AH60 Main graph was not created")
    if (
        shader.inputs["Base Color"].links[0].from_node.name
        != "BC2 AH60 main detail modulation"
    ):
        raise AssertionError("AH60 Main source color relationship is missing")

    minigun_surface = SurfaceShader(
        "Minigun_Main",
        (
            "objects/weapons/stationary/minigun/shaders/main/"
            "44665ec2-1eb9-4d51-a6fe-531a694395ca"
        ),
        (
            "Objects/Vehicles/Air/AH60/AH60_Mesh/"
            "Objects/Weapons/Stationary/Minigun/Shaders/Main"
        ),
    )
    minigun_bindings = (
        ShaderTextureBinding(
            2,
            "Textures/Vehicles/UsSymbols/Symbols_Us",
            ("texture_Texture",),
        ),
        ShaderTextureBinding(
            3,
            "Textures/_MASK/Msk_Generic",
            ("texture_Texture10",),
        ),
        ShaderTextureBinding(
            4,
            "Textures/Weapons/Stationary/MiniGun/MiniGun_C",
            ("texture_Color",),
        ),
    )
    roles = blender_addon._surface_binding_roles(
        minigun_surface,
        minigun_bindings,
    )
    if roles != ("COLOR", "MASK", "SPECULAR"):
        raise AssertionError("Verified minigun binding roles are incorrect")
    if blender_addon._surface_binding_uv_map(
        minigun_surface,
        minigun_bindings[0],
        roles[0],
    ) != "UV1":
        raise AssertionError("Minigun symbols do not use UV1")
    if blender_addon._surface_binding_uv_map(
        minigun_surface,
        minigun_bindings[1],
        roles[1],
    ) != "UVMap":
        raise AssertionError("Minigun generic mask does not use UVMap")
    if blender_addon._surface_binding_uv_map(
        minigun_surface,
        minigun_bindings[2],
        roles[2],
    ) != "UVMap":
        raise AssertionError("Minigun control map does not use UVMap")
    if blender_addon._surface_binding_uv_scale(
        minigun_surface,
        minigun_bindings[1],
    ) != (13.0, 13.0):
        raise AssertionError("Minigun generic mask is not repeated by 13")

    material = bpy.data.materials.new("BC2 Verified Minigun")
    nodes, links, shader = blender_addon._material_nodes(material)
    texture_nodes = []
    for binding, role in zip(minigun_bindings, roles):
        node = nodes.new("ShaderNodeTexImage")
        node.name = f"t{binding.index}"
        texture_nodes.append((binding, role, node))
    if not blender_addon._configure_verified_surface_material(
        material,
        minigun_surface,
        nodes,
        links,
        shader,
        texture_nodes,
    ):
        raise AssertionError("Verified minigun graph was not created")
    if not shader.inputs["Base Color"].is_linked:
        raise AssertionError("Verified minigun graph has no Base Color")
    if not shader.inputs["Specular IOR Level"].is_linked:
        raise AssertionError("MiniGun_C is not used as a specular mask")
    if (
        shader.inputs["Base Color"].links[0].from_node.name
        != "BC2 minigun base color"
    ):
        raise AssertionError("MiniGun_C was promoted to minigun Base Color")

    interior_surface = SurfaceShader(
        "Interior",
        (
            "objects/vehicles/air/ah60/shaders/interior/"
            "56ff07f2-c5d8-4e5c-ade3-731e43bfe4fe"
        ),
        (
            "Objects/Vehicles/Air/AH60/AH60_Mesh/"
            "Objects/Vehicles/Air/AH60/Shaders/Interior"
        ),
    )
    material = bpy.data.materials.new("BC2 Verified Interior")
    nodes, links, shader = blender_addon._material_nodes(material)
    binding = ShaderTextureBinding(
        1,
        "Textures/Vehicles/Common/Interior_D",
        ("texture_Texture",),
    )
    image_node = nodes.new("ShaderNodeTexImage")
    if not blender_addon._configure_verified_surface_material(
        material,
        interior_surface,
        nodes,
        links,
        shader,
        ((binding, "COLOR", image_node),),
    ):
        raise AssertionError("Verified interior graph was not created")
    if (
        shader.inputs["Base Color"].links[0].from_node.name
        != "BC2 interior albedo scale"
    ):
        raise AssertionError("Interior source diffuse scale is missing")
    scale_node = nodes["BC2 interior albedo scale"]
    if any(
        abs(value - 0.2) >= 1e-6
        for value in scale_node.inputs["Color2"].default_value[:3]
    ):
        raise AssertionError("Interior source diffuse scale is incorrect")

    glass_surface = SurfaceShader(
        "Glass",
        (
            "objects/vehicles/air/ah60/shaders/glass/"
            "b962700c-79cf-40a0-8f39-1c4850dad51f"
        ),
        (
            "Objects/Vehicles/Air/AH60/AH60_Mesh/"
            "Objects/Vehicles/Air/AH60/Shaders/Glass"
        ),
    )
    material = bpy.data.materials.new("BC2 Verified Glass")
    nodes, links, shader = blender_addon._material_nodes(material)
    binding = ShaderTextureBinding(
        2,
        "Textures/Vehicles/Dirt/DirtMaskRGB",
        ("texture_Texture2",),
    )
    dirt_node = nodes.new("ShaderNodeTexImage")
    if not blender_addon._configure_verified_surface_material(
        material,
        glass_surface,
        nodes,
        links,
        shader,
        ((binding, "MASK", dirt_node),),
    ):
        raise AssertionError("Verified glass graph was not created")
    if not shader.inputs["Alpha"].is_linked:
        raise AssertionError("Verified glass graph has no opacity input")
    if shader.inputs["Transmission Weight"].default_value != 1.0:
        raise AssertionError("Verified glass graph is not transmissive")


def _test_character_material_graph():
    surface = SurfaceShader(
        "ArmStandard",
        "Characters/US/Shaders/ArmStandard/GUID",
        (
            "Characters/US/US_Models/US_01_Body_Mesh/"
            "Characters/US/US_Models/Shaders/US_01_Mesh_ArmStandard"
        ),
    )
    bindings = (
        ShaderTextureBinding(
            1, "Characters/US/US_Textures/ArmStandard_01_Nm"
        ),
        ShaderTextureBinding(
            2, "Characters/US/US_Textures/Cloth_Detail_01_Nm"
        ),
        ShaderTextureBinding(
            3, "Characters/US/US_Textures/DirtMap_01_C"
        ),
        ShaderTextureBinding(
            4, "Characters/US/US_Textures/ArmStandard_01"
        ),
        ShaderTextureBinding(
            5, "Characters/US/US_Textures/Camo_Digital"
        ),
        ShaderTextureBinding(
            6, "Characters/US/US_Textures/Cloth_Detail_01"
        ),
        ShaderTextureBinding(
            7, "Characters/US/US_Textures/DirtMap_01"
        ),
        ShaderTextureBinding(
            8, "Characters/US/US_Textures/Occlusion/Occlusion_Assault"
        ),
    )
    roles = blender_addon._character_binding_roles(surface, bindings)
    if roles != (
        "NORMAL",
        "DETAIL_NORMAL",
        "DIRT_COLOR",
        "BASE_COLOR",
        "CAMO",
        "DETAIL_COLOR",
        "DIRT_MASK",
        "OCCLUSION",
    ):
        raise AssertionError(f"Character binding roles are incorrect: {roles}")
    if (
        blender_addon._character_binding_uv_map(surface, "CAMO") != "UV2"
        or blender_addon._character_binding_uv_map(
            surface, "OCCLUSION"
        )
        != "UV3"
        or blender_addon._character_binding_uv_scale(
            surface, "DETAIL_NORMAL"
        )
        != (7.0, 7.0)
    ):
        raise AssertionError("Character shader UV mapping is incorrect")
    for surface_name, roles_to_check in (
        (
            "LegStandard",
            ("CAMO", "DETAIL_COLOR", "DETAIL_NORMAL", "DIRT_MASK"),
        ),
        ("MOLLE", ("DETAIL_COLOR", "DETAIL_NORMAL", "DIRT_MASK")),
        ("Pouches", ("DETAIL_COLOR", "DETAIL_NORMAL", "DIRT_MASK")),
    ):
        affected_surface = SurfaceShader(
            surface_name,
            f"Characters/US/Shaders/{surface_name}/GUID",
            f"Characters/US/Shaders/{surface_name}",
        )
        if any(
            blender_addon._character_binding_uv_map(
                affected_surface,
                role,
            )
            != "UV2"
            for role in roles_to_check
        ):
            raise AssertionError(
                f"{surface_name} retained the collapsed character coordinate"
            )

    material = bpy.data.materials.new("BC2 Character Graph")
    nodes, links, shader = blender_addon._material_nodes(material)
    texture_nodes = []
    for binding, role in zip(bindings, roles):
        node = nodes.new("ShaderNodeTexImage")
        texture_nodes.append((binding, role, node))
    color_output, _, model = blender_addon._character_color_output(
        nodes,
        links,
        texture_nodes,
    )
    normal_output, normal_model = blender_addon._character_normal_output(
        nodes,
        links,
        texture_nodes,
    )
    links.new(color_output, shader.inputs["Base Color"])
    links.new(normal_output, shader.inputs["Normal"])
    if not shader.inputs["Base Color"].is_linked:
        raise AssertionError("Character material has no Base Color graph")
    if not shader.inputs["Normal"].is_linked:
        raise AssertionError("Character material has no normal graph")
    if "camouflage" not in model or "dirt" not in model:
        raise AssertionError("Character color graph omitted shader layers")
    if "red/green" not in normal_model or "alpha/green" not in normal_model:
        raise AssertionError("Character normal packing is incorrect")
    if not any(
        node.type == "VECT_MATH"
        and node.operation == "NORMALIZE"
        and node.name.startswith("BC2 character normalized normal")
        for node in nodes
    ):
        raise AssertionError("Character detail normal was not combined")


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


def _test_armature_and_action(context):
    identity_3 = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    identity_4 = (
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )
    skeleton = GrannySkeleton(
        (
            GrannyBone(
                "root",
                -1,
                0,
                (0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 1.0),
                identity_3,
                identity_4,
            ),
            GrannyBone(
                "child",
                0,
                0,
                (1.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 1.0),
                identity_3,
                identity_4,
            ),
        )
    )
    settings = SimpleNamespace(
        axis_conversion=True,
        unit_scale=2.0,
        flip_v=True,
        import_related_animations=True,
        import_core_gameplay_animations=True,
    )
    asset = {
        "path": "Characters/Test/Skeleton.res",
        "archive_path": "Dist/Test.fbrb",
        "platform": "pc",
    }
    armature = blender_addon._create_armature_object(
        context, skeleton, asset, settings
    )
    if len(armature.data.bones) != 2:
        raise AssertionError("Synthetic armature has the wrong bone count")
    if armature.data.bones["child"].parent.name != "root":
        raise AssertionError("Synthetic armature hierarchy is incorrect")
    _assert_vector(
        armature.data.bones["child"].matrix_local.translation,
        (2.0, 0.0, 0.0),
    )

    subset = MeshSubset(
        name="Skinned",
        positions=((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        faces=((0, 1, 2),),
        uvs=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        influences=(((0, 1.0),), ((1, 1.0),), ((1, 1.0),)),
        vertex_stride=16,
    )
    objects = blender_addon._create_mesh_objects(
        context,
        MeshAsset(1, "<", (subset,)),
        asset,
        settings,
        bone_names=("root", "child"),
    )
    blender_addon._bind_meshes_to_armature(objects, armature)
    if {group.name for group in objects[0].vertex_groups} != {
        "root",
        "child",
    }:
        raise AssertionError("Skinned mesh groups do not use source bone names")
    if objects[0].modifiers["Armature"].object != armature:
        raise AssertionError("Skinned mesh is not bound to its armature")

    identity_rotation = GrannyCurve(
        "D4Constant32f", 0, (0.0,), ((0.0, 0.0, 0.0, 1.0),)
    )
    identity_scale = GrannyCurve(
        "DaIdentity",
        0,
        (0.0,),
        ((1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),),
    )
    reduced_scale = GrannyCurve(
        "DaConstant32f",
        0,
        (0.0,),
        ((0.8, 0.0, 0.0, 0.0, 0.8, 0.0, 0.0, 0.0, 0.8),),
    )
    root_translation = GrannyCurve(
        "DaK32fC32f",
        1,
        (0.0, 1.0),
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
    )
    child_translation = GrannyCurve(
        "DaK32fC32f",
        1,
        (0.0, 1.0),
        ((1.0, 0.0, 0.0), (1.0, 1.0, 0.0)),
    )
    animation = GrannyAnimation(
        "SyntheticAction",
        1.0,
        0.5,
        1.0,
        (
            GrannyTransformTrack(
                "root", 0, identity_rotation, root_translation, identity_scale
            ),
            GrannyTransformTrack(
                "child", 0, identity_rotation, child_translation, reduced_scale
            ),
        ),
    )
    action = blender_addon._create_animation_action(
        context, armature, skeleton, animation, settings
    )
    if len(action.slots) != 1 or len(action.layers) != 1:
        raise AssertionError("Blender 5.2 Action slots/layers were not created")
    channelbag = action.layers[0].strips[0].channelbags[0]
    if len(channelbag.fcurves) != 20:
        raise AssertionError("Synthetic Action has the wrong F-Curve count")
    context.scene.frame_set(3)
    _assert_vector(armature.pose.bones["root"].matrix.translation, (2.0, 0.0, 0.0))
    _assert_vector(
        armature.pose.bones["child"].matrix.translation,
        (4.0, 0.0, 2.0),
    )
    _assert_vector(armature.pose.bones["child"].scale, (0.8, 0.8, 0.8))

    additive_translation = GrannyCurve(
        "DaK32fC32f",
        1,
        (0.0, 1.0),
        ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    )
    additive_animation = GrannyAnimation(
        "SyntheticAdditive",
        1.0,
        0.5,
        1.0,
        (
            GrannyTransformTrack(
                "child",
                0,
                identity_rotation,
                additive_translation,
                identity_scale,
            ),
        ),
        (1,),
    )
    additive_base = GrannyAnimation(
        "SyntheticFacePose",
        0.5,
        0.5,
        1.0,
        (
            GrannyTransformTrack(
                "child",
                0,
                identity_rotation,
                GrannyCurve(
                    "DaConstant32f",
                    0,
                    (0.0,),
                    ((1.0, 0.0, 0.0),),
                ),
                identity_scale,
            ),
        ),
    )
    try:
        blender_addon._create_animation_action(
            context,
            armature,
            skeleton,
            additive_animation,
            settings,
        )
    except BC2Error as exc:
        if "requires an authored base pose" not in str(exc):
            raise
    else:
        raise AssertionError("Additive Action accepted no authored base pose")
    additive_action = blender_addon._create_animation_action(
        context,
        armature,
        skeleton,
        additive_animation,
        settings,
        additive_base,
    )
    if (
        additive_action["bc2_additive"] is not True
        or additive_action["bc2_additive_baked"] is not True
        or additive_action["bc2_additive_bone_count"] != 1
        or additive_action["bc2_baked_track_count"] != 1
        or additive_action["bc2_additive_base_name"]
        != "SyntheticFacePose"
    ):
        raise AssertionError("Baked additive Action metadata is incorrect")
    context.scene.frame_set(1)
    _assert_vector(armature.pose.bones["child"].location, (0.0, 0.0, 0.0))
    context.scene.frame_set(3)
    _assert_vector(armature.pose.bones["child"].location, (0.0, 0.0, 2.0))

    related_asset = {
        "id": 10,
        "path": "Animations/Test/Idle.res",
        "archive_path": "Dist/Test.fbrb",
        "platform": "pc",
    }
    wrong_asset = {
        "id": 11,
        "path": "Animations/Test/Wrong.res",
        "archive_path": "Dist/Other.fbrb",
        "platform": "pc",
    }
    mesh_folder_asset = {
        "id": 14,
        "path": "Characters/Test/MeshPose.res",
        "archive_path": "Dist/Test.fbrb",
        "platform": "pc",
    }
    entity_asset = {
        "id": 15,
        "path": "Objects/Soldiers/Test/TestSoldier.dbx",
        "archive_path": "Dist/Test.fbrb",
        "platform": "pc",
    }
    tree_asset = {
        "id": 16,
        "path": "Characters/AnimTree/Test.dbx",
        "archive_path": "Dist/Test.fbrb",
        "platform": "pc",
    }
    graph_asset = {
        "id": 17,
        "path": "Characters/Animations/GraphDelta.res",
        "archive_path": "Dist/Test.fbrb",
        "platform": "pc",
    }

    class RelatedCatalog:
        def assets_with_prefix(self, path_prefix, asset_type=None):
            if (
                path_prefix == "Objects/Soldiers/"
                and asset_type is None
            ):
                return (entity_asset,)
            if asset_type != "GrannyAnimation":
                raise AssertionError("Related animation search scope is wrong")
            if path_prefix == "Animations/Test/":
                return (
                    related_asset,
                    {**related_asset, "id": 12},
                    wrong_asset,
                    {
                        "id": 13,
                        "path": "Animations/Test/Nested/Ignored.res",
                        "archive_path": "Dist/Test.fbrb",
                        "platform": "pc",
                    },
                )
            if path_prefix == "Characters/Test/":
                return (mesh_folder_asset,)
            raise AssertionError("Related animation search scope is wrong")

        def archive_index_counts(self):
            return 2, 3

        def find_asset(self, path, preferred_archives=()):
            if path == related_asset["path"]:
                return related_asset
            if path == wrong_asset["path"]:
                return wrong_asset
            if path == mesh_folder_asset["path"]:
                return mesh_folder_asset
            if path == tree_asset["path"]:
                return tree_asset
            if path == graph_asset["path"]:
                return graph_asset
            return None

    class RelatedRepository:
        catalog = RelatedCatalog()

        def materialize_archive_payload(self, archive_path):
            if archive_path != "Dist/Test.fbrb":
                raise AssertionError("Unexpected archive materialization")
            return None

        def read_asset(self, asset_id, max_size=None):
            if asset_id == related_asset["id"]:
                return related_asset, b"synthetic"
            if asset_id == mesh_folder_asset["id"]:
                return mesh_folder_asset, b"synthetic"
            if asset_id == entity_asset["id"]:
                return entity_asset, b"characters/test/skeleton"
            if asset_id == tree_asset["id"]:
                return tree_asset, b"tree"
            if asset_id == graph_asset["id"]:
                return graph_asset, b"synthetic"
            raise AssertionError("Unrelated animation was read")

    original_skeleton_path = blender_addon._skeleton_path_for_resource
    original_document_skeleton_path = (
        blender_addon._skeleton_path_for_document
    )
    original_parse_animation = blender_addon.parse_granny_animation
    original_parse_dbx = blender_addon.parse_dbx
    original_character_links = blender_addon.character_animation_links
    original_tree_references = (
        blender_addon.animation_tree_clip_references
    )
    original_replacement_references = (
        blender_addon.animation_tree_replacement_references
    )
    try:
        blender_addon._skeleton_path_for_resource = (
            lambda _repository, resource: (
                "Animations/Test/skel_Test.res"
                if resource["id"]
                in {
                    related_asset["id"],
                    mesh_folder_asset["id"],
                    graph_asset["id"],
                }
                else "Animations/Other/skel_Other.res"
            )
        )
        blender_addon._skeleton_path_for_document = (
            lambda _document, _path: "Animations/Test/skel_Test.res"
        )
        blender_addon.parse_granny_animation = (
            lambda _raw, _platform, _name: animation
        )
        blender_addon.parse_dbx = lambda raw: raw
        blender_addon.character_animation_links = (
            lambda _document, _path: blender_addon.CharacterAnimationLinks(
                animation_tree_references=(
                    "Characters/AnimTree/Test/TREE-GUID",
                )
            )
        )
        blender_addon.animation_tree_clip_references = (
            lambda _document: (
                "Characters/Animations/GraphDelta/CLIP-GUID",
            )
        )
        blender_addon.animation_tree_replacement_references = (
            lambda _document: ()
        )
        related = blender_addon._import_related_animations(
            context,
            RelatedRepository(),
            asset,
            {
                "path": "Animations/Test/skel_Test.res",
                "archive_path": "Dist/Test.fbrb",
            },
            skeleton,
            armature,
            settings,
        )
    finally:
        blender_addon._skeleton_path_for_resource = original_skeleton_path
        blender_addon._skeleton_path_for_document = (
            original_document_skeleton_path
        )
        blender_addon.parse_granny_animation = original_parse_animation
        blender_addon.parse_dbx = original_parse_dbx
        blender_addon.character_animation_links = original_character_links
        blender_addon.animation_tree_clip_references = (
            original_tree_references
        )
        blender_addon.animation_tree_replacement_references = (
            original_replacement_references
        )
    if len(related.actions) != 3:
        raise AssertionError("Related animations were not deduplicated")
    if armature["bc2_related_animation_count"] != 3:
        raise AssertionError("Related animation metadata is incorrect")
    warning = blender_addon._related_animation_warning(related)
    if "1 archive(s) remain unindexed" not in warning:
        raise AssertionError("Partial related animation coverage was not reported")


def main():
    if blender_addon._np is None:
        raise AssertionError("Blender NumPy is unavailable")
    _test_settings_registration(bpy.context)
    _test_import_policies(bpy.context)
    _test_mesh(bpy.context)
    _test_composite_parts(bpy.context)
    _test_terrain(bpy.context)
    _test_native_image(bpy.context)
    _test_python_fallback()
    _test_linked_map_geometry(bpy.context)
    _test_verified_normal_graph()
    _test_shader_key_alias()
    _test_reflected_material_roles()
    _test_verified_vehicle_material_graphs()
    _test_character_material_graph()
    _test_degenerate_subset_materials()
    _test_armature_and_action(bpy.context)
    print(
        "PASS Blender mesh, composite, rig, action, terrain, map, material, "
        "fallback, and DDS paths",
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
