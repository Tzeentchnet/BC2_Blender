# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import (
    AddonPreferences,
    Menu,
    Operator,
    Panel,
    PropertyGroup,
    UIList,
)
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Quaternion

from .core.assembly import (
    AssemblyComponent,
    AssemblyPart,
    ModularAssembly,
    modular_assembly,
    vehicle_part_state,
)
from .core.dbx import dbx_to_xml, parse_dbx
from .core.errors import BC2Error
from .core.granny import (
    GrannyAnimation,
    GrannyBone,
    GrannySkeleton,
    parse_granny_animation,
    parse_granny_model,
)
from .core.io import safe_cache_name
from .core.level import (
    LevelResolver,
    MeshDefinition,
    PC_CHARACTER_SHADER_PATH,
    SurfaceShader,
    index_pc_character_shader_archive,
    index_pc_level_archives,
)
from .core.mesh import (
    CompositePart,
    MeshAsset,
    MeshSubset,
    parse_composite_lod_parts,
    parse_meshdata,
    parse_watermesh,
    split_mesh_subset_by_parts,
)
from .core.repository import AssetRepository
from .core.shaderdb import (
    ShaderDatabase,
    ShaderTextureBinding,
    parse_shader_database,
)
from .core.terrain import (
    TerrainMaterialMap,
    TerrainMesh,
    parse_heightfield,
    parse_material_map,
)
from .core.texture import convert_texture, create_luminance_dds

try:
    import numpy as _np
except ImportError:
    _np = None


_PACKAGE_PARTS = __package__.split(".")
ADDON_ID = (
    ".".join(_PACKAGE_PARTS[:-1])
    if _PACKAGE_PARTS[:1] == ["bl_ext"] and len(_PACKAGE_PARTS) >= 4
    else __package__
)
RESULTS_PER_PAGE = 150
_PC_MODULAR_SHADER_FALLBACKS = (
    (
        "Dist/win32/levels/mp_004/level-00.fbrb",
        "Levels/mp_004/shaders/Dx11_Single.res",
    ),
)


@dataclass
class _SourceMaterialContext:
    import_textures: bool
    material_archives: tuple[str, ...]
    shader_database: ShaderDatabase | None
    shader_database_path: str | None
    material_cache: dict[str, object]
    image_cache: dict[str, tuple[object, object]]
    diagnostics: list[dict[str, str]]
    loaded_shader_paths: set[str]


@dataclass(frozen=True)
class _ImportedAssemblyComponent:
    definition: AssemblyComponent
    collection: object
    objects: tuple[object, ...]
    mesh_type: int


@dataclass(frozen=True)
class _ImportedAssembly:
    definition: ModularAssembly
    root_collection: object
    armature: object | None
    components: tuple[_ImportedAssemblyComponent, ...]
    diagnostics_text: object


CATEGORIES = (
    ("ALL", "All indexed", "All indexed assets", "ASSET_MANAGER", 0),
    ("MESH", "Meshes", "Meshes and mesh sets", "MESH_DATA", 1),
    ("TEXTURE", "Textures", "Textures and material maps", "IMAGE_DATA", 2),
    ("TERRAIN", "Terrain", "Heightfields and terrain resources", "RNDCURVE", 3),
    ("ANIMATION", "Animation", "Skeleton and animation resources", "ARMATURE_DATA", 4),
    ("LEVEL_DATA", "Levels / DBX", "Level and DBX data", "WORLD_DATA", 5),
    ("AUDIO", "Audio", "Wave and impulse response resources", "SOUND", 6),
    ("UI_VIDEO", "UI / Video", "Flash and Bink resources", "SEQUENCE", 7),
    ("DATA", "Data", "Other non-resource files", "TEXT", 8),
    ("OTHER", "Other", "Other Frostbite resources", "FILE", 9),
)


def _default_cache_directory() -> str:
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    return str(root / "BC2Blender" / "Cache")


class BC2AddonPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    cache_directory: StringProperty(
        name="Asset cache",
        description=(
            "Cabinets, extracted archives, the SQLite catalog, and converted DDS files "
            "are cached here"
        ),
        subtype="DIR_PATH",
        default=_default_cache_directory(),
    )

    def draw(self, context):
        self.layout.prop(self, "cache_directory")


class BC2AssetListItem(PropertyGroup):
    asset_id: IntProperty()
    path: StringProperty()
    asset_type: StringProperty()
    archive_path: StringProperty()
    size: IntProperty()


class BC2ArchiveListItem(PropertyGroup):
    archive_id: IntProperty()
    path: StringProperty()
    indexed: BoolProperty()
    asset_count: IntProperty()
    size: IntProperty()
    error: StringProperty()


class BC2Settings(PropertyGroup):
    source_path: StringProperty(
        name="Disc image",
        description="Battlefield: Bad Company 2 PC or Xbox 360 ISO",
        subtype="FILE_PATH",
    )
    source_kind: StringProperty(name="Detected source")
    status: StringProperty(name="Status", default="Select a disc image, then scan")
    category: EnumProperty(name="Asset type", items=CATEGORIES, default="MESH")
    search: StringProperty(name="Search", description="Filter paths and resource types")
    archive_search: StringProperty(
        name="Archive search", description="Filter Frostbite archives"
    )
    page: IntProperty(default=0, min=0)
    total_results: IntProperty(default=0, min=0)
    assets: CollectionProperty(type=BC2AssetListItem)
    active_asset_index: IntProperty(default=0)
    archives: CollectionProperty(type=BC2ArchiveListItem)
    active_archive_index: IntProperty(default=0)
    axis_conversion: BoolProperty(
        name="Convert Y-up to Blender Z-up",
        default=True,
    )
    flip_v: BoolProperty(
        name="Flip texture V",
        description="Convert DirectX UV orientation to Blender orientation",
        default=True,
    )
    unit_scale: FloatProperty(name="Scale", default=1.0, min=0.0001, max=10000.0)
    include_z_only: BoolProperty(
        name="Include Z-only subsets",
        description="Import shadow/depth-only geometry in addition to visible subsets",
        default=False,
    )
    terrain_resolution: IntProperty(
        name="Terrain mesh resolution",
        description=(
            "Maximum vertices per terrain edge; 1024 preserves the source "
            "resolution of the MP_001 playable heightfield"
        ),
        default=1024,
        min=16,
        max=1024,
    )
    map_name: StringProperty(
        name="PC map",
        description="Retail PC level name, for example MP_001",
        default="MP_001",
    )
    map_import_materials: BoolProperty(
        name="Resolve map materials and textures",
        description=(
            "Read the level DX11 shader database, load referenced textures, "
            "and build conservative Blender materials"
        ),
        default=True,
    )


_repository: AssetRepository | None = None
_repository_key: tuple[str, str] | None = None


def _preferences(context) -> BC2AddonPreferences:
    return context.preferences.addons[ADDON_ID].preferences


def _settings(context) -> BC2Settings:
    return context.scene.bc2_settings


def _close_repository():
    global _repository, _repository_key
    if _repository is not None:
        _repository.close()
    _repository = None
    _repository_key = None


def _get_repository(context, require_scan: bool = True) -> AssetRepository:
    global _repository, _repository_key
    settings = _settings(context)
    source_path = bpy.path.abspath(settings.source_path)
    if not source_path:
        raise BC2Error("Select a PC or Xbox 360 disc image first")
    cache_path = bpy.path.abspath(_preferences(context).cache_directory)
    key = (str(Path(source_path).resolve()), str(Path(cache_path).resolve()))
    if _repository is None or _repository_key != key:
        _close_repository()
        _repository = AssetRepository(key[0], key[1])
        _repository_key = key
    if require_scan and _repository.needs_scan():
        raise BC2Error("The selected disc has not been scanned yet")
    settings.source_kind = _repository.kind
    return _repository


def _basename(path: str) -> str:
    return PurePosixPath(path).name


def _load_archives(context):
    settings = _settings(context)
    repository = _get_repository(context)
    rows = repository.catalog.archives(settings.archive_search, limit=500)
    settings.archives.clear()
    for row in rows:
        item = settings.archives.add()
        item.archive_id = row["id"]
        item.path = row["path"]
        item.indexed = bool(row["indexed"])
        item.asset_count = row["asset_count"]
        item.size = row["size"]
        item.error = row["error"] or ""
    settings.active_archive_index = min(
        settings.active_archive_index, max(0, len(settings.archives) - 1)
    )


def _load_assets(context, reset_page: bool = False):
    settings = _settings(context)
    repository = _get_repository(context)
    if reset_page:
        settings.page = 0
    settings.total_results = repository.catalog.asset_count(
        settings.category, settings.search
    )
    max_page = max(0, (settings.total_results - 1) // RESULTS_PER_PAGE)
    settings.page = min(settings.page, max_page)
    rows = repository.catalog.search_assets(
        settings.category,
        settings.search,
        RESULTS_PER_PAGE,
        settings.page * RESULTS_PER_PAGE,
    )
    settings.assets.clear()
    for row in rows:
        item = settings.assets.add()
        item.asset_id = row["id"]
        item.path = row["path"]
        item.asset_type = row["asset_type"]
        item.archive_path = row["archive_path"]
        item.size = row["size"]
    settings.active_asset_index = min(
        settings.active_asset_index, max(0, len(settings.assets) - 1)
    )


def _selected_asset(settings: BC2Settings) -> BC2AssetListItem:
    if not settings.assets:
        raise BC2Error("No asset is selected")
    index = settings.active_asset_index
    if index < 0 or index >= len(settings.assets):
        raise BC2Error("The selected asset row is no longer available")
    return settings.assets[index]


def _selected_archive(settings: BC2Settings) -> BC2ArchiveListItem:
    if not settings.archives:
        raise BC2Error("No Frostbite archive is selected")
    index = settings.active_archive_index
    if index < 0 or index >= len(settings.archives):
        raise BC2Error("The selected archive row is no longer available")
    return settings.archives[index]


def _mesh_position(
    value: tuple[float, float, float], settings: BC2Settings
) -> tuple[float, float, float]:
    x, y, z = value
    scale = settings.unit_scale
    if settings.axis_conversion:
        return x * scale, -z * scale, y * scale
    return x * scale, y * scale, z * scale


def _terrain_position(
    value: tuple[float, float, float], settings: BC2Settings
) -> tuple[float, float, float]:
    x, z, height = value
    scale = settings.unit_scale
    if settings.axis_conversion:
        return x * scale, -z * scale, height * scale
    return x * scale, height * scale, z * scale


def _new_collection(context, name: str):
    collection = bpy.data.collections.new(name)
    context.collection.children.link(collection)
    return collection


def _new_child_collection(parent, name: str):
    collection = bpy.data.collections.new(name)
    parent.children.link(collection)
    return collection


def _mesh_positions(values, settings: BC2Settings):
    if _np is None:
        return [_mesh_position(value, settings) for value in values]
    source = _np.asarray(values, dtype=_np.float64)
    result = _np.empty_like(source)
    if settings.axis_conversion:
        result[:, 0] = source[:, 0]
        result[:, 1] = -source[:, 2]
        result[:, 2] = source[:, 1]
    else:
        result[:] = source
    result *= settings.unit_scale
    return result


def _terrain_positions(values, settings: BC2Settings):
    if _np is None:
        return [_terrain_position(value, settings) for value in values]
    source = _np.asarray(values, dtype=_np.float64)
    result = _np.empty_like(source)
    result[:, 0] = source[:, 0]
    if settings.axis_conversion:
        result[:, 1] = -source[:, 1]
        result[:, 2] = source[:, 2]
    else:
        result[:, 1] = source[:, 2]
        result[:, 2] = source[:, 1]
    result *= settings.unit_scale
    return result


def _populate_triangle_mesh(
    mesh,
    positions,
    faces,
    uvs=None,
    material_indices=None,
    flip_v: bool = False,
    smooth: bool = False,
):
    if _np is None:
        mesh.from_pydata(positions, [], faces)
        if material_indices is not None:
            if len(material_indices) != len(mesh.polygons):
                raise BC2Error(
                    "Material-index count does not match the polygon count"
                )
            for polygon, material_index in zip(
                mesh.polygons, material_indices
            ):
                polygon.material_index = material_index
        mesh.update(calc_edges=True)
        mesh.validate(verbose=False, clean_customdata=False)
        if uvs is not None and len(uvs) and mesh.loops:
            uv_layer = mesh.uv_layers.new(name="UVMap")
            for loop in mesh.loops:
                u, v = uvs[loop.vertex_index]
                uv_layer.data[loop.index].uv = (
                    u,
                    1.0 - v if flip_v else v,
                )
        if smooth:
            for polygon in mesh.polygons:
                polygon.use_smooth = True
        return

    position_array = _np.ascontiguousarray(
        _np.asarray(positions, dtype=_np.float32).reshape(-1, 3)
    )
    face_array = _np.ascontiguousarray(
        _np.asarray(faces, dtype=_np.int32).reshape(-1, 3)
    )
    loop_vertices = face_array.reshape(-1)
    face_count = len(face_array)

    mesh.vertices.add(len(position_array))
    mesh.vertices.foreach_set("co", position_array.reshape(-1))
    mesh.loops.add(len(loop_vertices))
    mesh.loops.foreach_set("vertex_index", loop_vertices)
    mesh.polygons.add(face_count)
    mesh.polygons.foreach_set(
        "loop_start",
        _np.arange(0, face_count * 3, 3, dtype=_np.int32),
    )
    mesh.polygons.foreach_set(
        "loop_total", _np.full(face_count, 3, dtype=_np.int32)
    )
    if material_indices is not None:
        material_array = _np.ascontiguousarray(
            _np.asarray(material_indices, dtype=_np.int32).reshape(-1)
        )
        if len(material_array) != len(mesh.polygons):
            raise BC2Error(
                "Material-index count does not match the polygon count"
            )
        mesh.polygons.foreach_set("material_index", material_array)
    mesh.update(calc_edges=True)
    mesh.validate(verbose=False, clean_customdata=False)

    if uvs is not None and len(uvs) and mesh.loops:
        actual_loop_vertices = _np.empty(len(mesh.loops), dtype=_np.int32)
        mesh.loops.foreach_get("vertex_index", actual_loop_vertices)
        uv_array = _np.asarray(uvs, dtype=_np.float64).reshape(-1, 2)
        loop_uvs = uv_array[actual_loop_vertices]
        if flip_v:
            loop_uvs[:, 1] = 1.0 - loop_uvs[:, 1]
        loop_uvs = _np.ascontiguousarray(loop_uvs, dtype=_np.float32)
        uv_layer = mesh.uv_layers.new(name="UVMap")
        uv_layer.uv.foreach_set("vector", loop_uvs.reshape(-1))
    if smooth and mesh.polygons:
        mesh.polygons.foreach_set(
            "use_smooth",
            _np.ones(len(mesh.polygons), dtype=_np.bool_),
        )


def _add_uv_layer(
    mesh,
    name: str,
    uvs,
    *,
    flip_v: bool,
):
    if uvs is None or len(uvs) == 0 or not mesh.loops:
        return None
    uv_layer = mesh.uv_layers.new(name=name)
    if _np is None:
        for loop in mesh.loops:
            u, v = uvs[loop.vertex_index]
            uv_layer.data[loop.index].uv = (
                u,
                1.0 - v if flip_v else v,
            )
        return uv_layer
    actual_loop_vertices = _np.empty(len(mesh.loops), dtype=_np.int32)
    mesh.loops.foreach_get("vertex_index", actual_loop_vertices)
    uv_array = _np.asarray(uvs, dtype=_np.float64).reshape(-1, 2)
    loop_uvs = uv_array[actual_loop_vertices]
    if flip_v:
        loop_uvs[:, 1] = 1.0 - loop_uvs[:, 1]
    loop_uvs = _np.ascontiguousarray(loop_uvs, dtype=_np.float32)
    uv_layer.uv.foreach_set("vector", loop_uvs.reshape(-1))
    return uv_layer


def _create_vertex_groups(obj, influences, bone_names=None):
    groups = {}
    for vertex_index, vertex_influences in enumerate(influences):
        for bone, weight in vertex_influences:
            groups.setdefault(bone, {}).setdefault(weight, []).append(
                vertex_index
            )
    for bone, weights in groups.items():
        if bone_names is not None and 0 <= bone < len(bone_names):
            name = bone_names[bone]
        else:
            name = f"Bone_{bone:03d}"
        group = obj.vertex_groups.new(name=name)
        for weight, vertex_indices in weights.items():
            group.add(vertex_indices, weight, "REPLACE")


def _create_mesh_objects(
    context,
    parsed: MeshAsset,
    asset,
    settings: BC2Settings,
    *,
    collection=None,
    bone_names=None,
    subset_materials=None,
    split_parts: bool = False,
    part_names: tuple[str, ...] = (),
    composite_parts: tuple[CompositePart, ...] = (),
    part_states: tuple[AssemblyPart, ...] = (),
):
    if (
        subset_materials is not None
        and len(subset_materials) != len(parsed.subsets)
    ):
        raise ValueError("Subset material count does not match mesh subsets")
    collection = collection or _new_collection(
        context, Path(asset["path"]).stem
    )
    parts_collection = (
        _new_child_collection(collection, "Parts")
        if split_parts
        else None
    )
    part_collections = {}
    part_records = {
        index: part
        for index, part in enumerate(composite_parts)
    }
    states_by_name = {
        part.name.casefold(): part
        for part in part_states
    }
    unresolved_part_subsets = 0

    def target_collection(part_index: int):
        target = part_collections.get(part_index)
        if target is not None:
            return target
        record = part_records.get(part_index)
        part_name = (
            record.name
            if record is not None
            else (
                part_names[part_index]
                if 0 <= part_index < len(part_names)
                else f"Part_{part_index:03d}"
            )
        )
        part_state = states_by_name.get(part_name.casefold())
        state = (
            part_state.state
            if part_state is not None
            else vehicle_part_state(part_name)
        )
        target = _new_child_collection(
            parts_collection,
            f"{part_index:03d}_{part_name}",
        )
        target["bc2_part_index"] = part_index
        target["bc2_part_name"] = part_name
        target["bc2_part_state"] = state
        target["bc2_part_default_visible"] = state == "DEFAULT"
        target["bc2_part_state_source"] = (
            part_state.source
            if part_state is not None
            else "part_name"
        )
        target.hide_viewport = state != "DEFAULT"
        target.hide_render = state != "DEFAULT"
        if record is not None:
            if record.bounds is not None:
                target["bc2_part_bounds"] = json.dumps(
                    record.bounds,
                    separators=(",", ":"),
                )
            if record.transform is not None:
                target["bc2_part_transform"] = json.dumps(
                    record.transform,
                    separators=(",", ":"),
                )
        if part_state is not None:
            target["bc2_source_component_type"] = (
                part_state.component_type
            )
            if part_state.component_name:
                target["bc2_source_component_name"] = (
                    part_state.component_name
                )
        part_collections[part_index] = target
        return target

    def create_object(
        subset_index: int,
        subset: MeshSubset,
        target,
        material,
        part_index: int | None = None,
    ):
        mesh = bpy.data.meshes.new(f"{subset.name}_Mesh")
        positions = _mesh_positions(subset.positions, settings)
        _populate_triangle_mesh(
            mesh,
            positions,
            subset.faces,
            uvs=subset.uvs,
            flip_v=settings.flip_v,
            smooth=True,
        )
        if subset.secondary_uvs:
            _add_uv_layer(
                mesh,
                "UV1",
                subset.secondary_uvs,
                flip_v=settings.flip_v,
            )
        for channel_index, channel in enumerate(
            subset.additional_uvs,
            start=2,
        ):
            _add_uv_layer(
                mesh,
                f"UV{channel_index}",
                channel,
                flip_v=settings.flip_v,
            )
        mesh["bc2_uv_offsets"] = json.dumps(
            subset.uv_offsets,
            separators=(",", ":"),
        )
        mesh["bc2_palette"] = json.dumps(
            subset.palette,
            separators=(",", ":"),
        )
        if subset.part_index_offset is not None:
            mesh["bc2_part_index_offset"] = subset.part_index_offset
        mesh.materials.append(material)
        obj = bpy.data.objects.new(
            f"{subset_index:02d}_{subset.name}",
            mesh,
        )
        target.objects.link(obj)
        if not split_parts:
            _create_vertex_groups(obj, subset.influences, bone_names)
        obj["bc2_asset_path"] = asset["path"]
        obj["bc2_archive_path"] = asset["archive_path"]
        obj["bc2_platform"] = asset["platform"]
        obj["bc2_source_subset"] = subset.name
        if part_index is not None:
            obj["bc2_part_index"] = part_index
            obj["bc2_part_name"] = target.get(
                "bc2_part_name",
                f"Part_{part_index:03d}",
            )
            obj["bc2_part_state"] = target.get(
                "bc2_part_state",
                "DEFAULT",
            )
            obj["bc2_part_default_visible"] = target.get(
                "bc2_part_default_visible",
                True,
            )
            for property_name in (
                "bc2_part_bounds",
                "bc2_part_transform",
                "bc2_part_state_source",
                "bc2_source_component_type",
                "bc2_source_component_name",
            ):
                if property_name in target:
                    obj[property_name] = target[property_name]
        wreck_name_count = max(
            subset.name.casefold().count("wreck"),
            obj.name.casefold().count("wreck"),
            mesh.name.casefold().count("wreck"),
        )
        wreck_named = wreck_name_count >= 2
        part_default_visible = obj.get(
            "bc2_part_default_visible",
            True,
        )
        obj["bc2_geometry_default_visible"] = (
            part_default_visible and not wreck_named
        )
        obj["bc2_geometry_wreck_name_count"] = wreck_name_count
        if wreck_named:
            obj.hide_viewport = True
            obj.hide_render = True
            obj.hide_set(True)
            obj["bc2_geometry_state"] = "DESTROYED"
            obj["bc2_geometry_visibility_source"] = (
                "name_contains_wreck_twice"
            )
        if part_index is not None:
            record = part_records.get(part_index)
            if record is not None and record.transform is not None:
                obj.matrix_basis = _composite_part_matrix(
                    record,
                    settings,
                )
        return obj

    created = []
    for subset_index, subset in enumerate(parsed.subsets):
        if subset_materials is None:
            material = bpy.data.materials.new(subset.name)
            material.diffuse_color = (0.8, 0.8, 0.8, 1.0)
        else:
            material = subset_materials[subset_index]
        pieces = (
            split_mesh_subset_by_parts(subset)
            if split_parts
            else ()
        )
        if pieces:
            for part_index, piece in pieces:
                created.append(
                    create_object(
                        subset_index,
                        piece,
                        target_collection(part_index),
                        material,
                        part_index,
                    )
                )
        else:
            if split_parts:
                unresolved_part_subsets += 1
                target = part_collections.get(-1)
                if target is None:
                    target = _new_child_collection(
                        parts_collection,
                        "Unassigned",
                    )
                    target["bc2_part_index"] = -1
                    target["bc2_part_name"] = "Unassigned"
                    part_collections[-1] = target
            else:
                target = collection
            created.append(
                create_object(
                    subset_index,
                    subset,
                    target,
                    material,
                )
            )
    if split_parts:
        collection["bc2_part_collection_count"] = len(
            [
                part_index
                for part_index in part_collections
                if part_index >= 0
            ]
        )
        collection["bc2_unresolved_part_subset_count"] = (
            unresolved_part_subsets
        )
        state_counts = {}
        for part_index, part_collection in part_collections.items():
            if part_index < 0:
                continue
            state = part_collection.get("bc2_part_state", "DEFAULT")
            state_counts[state] = state_counts.get(state, 0) + 1
        collection["bc2_part_state_counts"] = json.dumps(
            state_counts,
            sort_keys=True,
            separators=(",", ":"),
        )
        collection["bc2_default_visible_part_count"] = state_counts.get(
            "DEFAULT",
            0,
        )
        collection["bc2_hidden_part_count"] = sum(
            count
            for state, count in state_counts.items()
            if state != "DEFAULT"
        )
    if created:
        active = next(
            (
                obj
                for obj in created
                if obj.get("bc2_geometry_default_visible", True)
            ),
            created[0],
        )
        bpy.context.view_layer.objects.active = active
        active.select_set(True)
    return created


def _composite_part_matrix(
    part: CompositePart,
    settings: BC2Settings,
) -> Matrix:
    values = part.transform
    if values is None:
        return Matrix.Identity(4)
    source = Matrix(
        (
            (values[0], values[1], values[2], values[9]),
            (values[3], values[4], values[5], values[10]),
            (values[6], values[7], values[8], values[11]),
            (0.0, 0.0, 0.0, 1.0),
        )
    )
    return _convert_source_matrix(source, settings)


def _source_transform_matrix(
    translation: tuple[float, float, float],
    rotation: tuple[float, float, float, float],
    scale_shear: tuple[float, ...] | None = None,
) -> Matrix:
    x, y, z, w = rotation
    matrix = (
        Matrix.Translation(translation)
        @ Quaternion((w, x, y, z)).to_matrix().to_4x4()
    )
    if scale_shear is not None:
        scale_matrix = Matrix.Identity(4)
        for row in range(3):
            for column in range(3):
                scale_matrix[row][column] = scale_shear[row * 3 + column]
        matrix @= scale_matrix
    return matrix


def _convert_source_matrix(matrix: Matrix, settings: BC2Settings) -> Matrix:
    if settings.axis_conversion:
        basis = Matrix(
            (
                (1.0, 0.0, 0.0, 0.0),
                (0.0, 0.0, -1.0, 0.0),
                (0.0, 1.0, 0.0, 0.0),
                (0.0, 0.0, 0.0, 1.0),
            )
        )
        result = basis @ matrix @ basis.transposed()
    else:
        result = matrix.copy()
    result.translation *= settings.unit_scale
    return result


def _convert_skeleton_matrix(matrix: Matrix, settings: BC2Settings) -> Matrix:
    return _convert_source_matrix(matrix, settings)


def _skeleton_local_matrices(
    skeleton: GrannySkeleton,
    settings: BC2Settings,
) -> tuple[Matrix, ...]:
    return tuple(
        _convert_skeleton_matrix(
            _source_transform_matrix(
                bone.translation,
                bone.rotation,
                bone.scale_shear,
            ),
            settings,
        )
        for bone in skeleton.bones
    )


def _skeleton_world_matrices(
    skeleton: GrannySkeleton,
    local_matrices: tuple[Matrix, ...],
) -> tuple[Matrix, ...]:
    world_matrices = []
    for bone, local_matrix in zip(skeleton.bones, local_matrices):
        if bone.parent_index < 0:
            world_matrices.append(local_matrix)
        else:
            world_matrices.append(
                world_matrices[bone.parent_index] @ local_matrix
            )
    return tuple(world_matrices)


def _bone_display_lengths(
    skeleton: GrannySkeleton,
    world_matrices: tuple[Matrix, ...],
    unit_scale: float,
) -> tuple[float, ...]:
    child_lengths = [[] for _ in skeleton.bones]
    for index, bone in enumerate(skeleton.bones):
        if bone.parent_index >= 0:
            distance = (
                world_matrices[index].translation
                - world_matrices[bone.parent_index].translation
            ).length
            if distance > 1e-6:
                child_lengths[bone.parent_index].append(distance)
    minimum = max(0.005, 0.02 * unit_scale)
    maximum = max(minimum, 0.25 * unit_scale)
    lengths = []
    for index, bone in enumerate(skeleton.bones):
        if child_lengths[index]:
            length = sum(child_lengths[index]) / len(child_lengths[index])
        elif bone.parent_index >= 0:
            length = (
                world_matrices[index].translation
                - world_matrices[bone.parent_index].translation
            ).length * 0.35
        else:
            length = minimum
        lengths.append(max(minimum, min(maximum, length)))
    return tuple(lengths)


def _create_armature_object(
    context,
    skeleton: GrannySkeleton,
    asset,
    settings: BC2Settings,
    *,
    collection=None,
):
    name = Path(asset["path"]).stem
    collection = collection or _new_collection(context, name)
    armature = bpy.data.armatures.new(f"{name}_Armature")
    obj = bpy.data.objects.new(name, armature)
    collection.objects.link(obj)
    local_matrices = _skeleton_local_matrices(skeleton, settings)
    world_matrices = _skeleton_world_matrices(skeleton, local_matrices)
    lengths = _bone_display_lengths(
        skeleton, world_matrices, settings.unit_scale
    )

    for selected in context.selected_objects:
        selected.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        edit_bones = []
        for index, (bone, matrix, length) in enumerate(
            zip(skeleton.bones, world_matrices, lengths)
        ):
            edit_bone = armature.edit_bones.new(bone.name)
            edit_bone.length = length
            edit_bone.matrix = matrix
            edit_bones.append(edit_bone)
        for edit_bone, bone in zip(edit_bones, skeleton.bones):
            if bone.parent_index >= 0:
                edit_bone.parent = edit_bones[bone.parent_index]
                edit_bone.use_connect = False
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")

    for blender_bone, source_bone in zip(
        armature.bones, skeleton.bones
    ):
        blender_bone["bc2_source_flags"] = source_bone.flags
        if source_bone.name.casefold().startswith("physjnt_"):
            blender_bone["bc2_physics_joint"] = True
    obj["bc2_asset_path"] = asset["path"]
    obj["bc2_archive_path"] = asset["archive_path"]
    obj["bc2_platform"] = asset["platform"]
    obj["bc2_bone_count"] = len(skeleton.bones)
    return obj


def _bind_meshes_to_armature(objects, armature_obj):
    for obj in objects:
        modifier = obj.modifiers.new(name="Armature", type="ARMATURE")
        modifier.object = armature_obj
        modifier.use_deform_preserve_volume = False
        obj.parent = armature_obj


def _resource_partition(path: str) -> str:
    lower = path.casefold()
    if lower.endswith(".res"):
        return path[:-4]
    return path


def _reference_resource_path(reference: str) -> str:
    if not reference or reference.casefold() == "null" or "/" not in reference:
        raise BC2Error("DBX skeleton reference is missing or invalid")
    return reference.rsplit("/", 1)[0] + ".res"


def _skeleton_path_for_resource(repository, resource_asset) -> str:
    descriptor_path = _resource_partition(resource_asset["path"]) + ".dbx"
    descriptor = repository.catalog.find_asset(
        descriptor_path,
        (resource_asset["archive_path"],),
    )
    if descriptor is None:
        raise BC2Error(
            f"Skeleton descriptor is not indexed: {descriptor_path}"
        )
    _, raw = repository.read_asset(
        int(descriptor["id"]), max_size=256 << 20
    )
    instance = parse_dbx(raw).primary_instance
    if instance is None:
        raise BC2Error(f"DBX descriptor has no primary instance: {descriptor_path}")
    field = instance.child("field", "Skeleton")
    reference = field.attribute("ref") if field is not None else None
    return _reference_resource_path(reference or "")


def _read_referenced_skeleton(repository, resource_asset):
    skeleton_path = _skeleton_path_for_resource(repository, resource_asset)
    skeleton_asset = repository.catalog.find_asset(
        skeleton_path,
        (resource_asset["archive_path"],),
    )
    if skeleton_asset is None:
        raise BC2Error(
            f"Referenced Granny skeleton is not indexed: {skeleton_path}"
        )
    asset, raw = repository.read_asset(
        int(skeleton_asset["id"]), max_size=256 << 20
    )
    skeleton = parse_granny_model(raw, asset["platform"])
    return asset, skeleton


def _find_skeleton_armature(skeleton_path: str):
    folded = skeleton_path.casefold()
    candidates = []
    if bpy.context.active_object is not None:
        candidates.append(bpy.context.active_object)
    candidates.extend(bpy.context.selected_objects)
    candidates.extend(bpy.data.objects)
    visited = set()
    for obj in candidates:
        if obj.name_full in visited:
            continue
        visited.add(obj.name_full)
        if (
            obj.type == "ARMATURE"
            and str(obj.get("bc2_asset_path", "")).casefold() == folded
        ):
            return obj
    return None


def _create_character(
    context,
    repository,
    mesh_set_asset,
    parsed: MeshAsset,
    mesh_asset,
    settings: BC2Settings,
):
    skeleton_asset, skeleton = _read_referenced_skeleton(
        repository, mesh_set_asset
    )
    (
        subset_materials,
        material_diagnostics,
        material_summary,
    ) = _resolve_mesh_subset_materials(
        repository,
        mesh_set_asset,
        parsed,
        (
            str(mesh_set_asset["archive_path"]),
            str(mesh_asset["archive_path"]),
        ),
        settings,
    )
    root_collection = _new_collection(
        context, Path(mesh_set_asset["path"]).stem
    )
    root_collection["bc2_assembly_kind"] = "CHARACTER_COMPONENT"
    root_collection["bc2_asset_path"] = mesh_set_asset["path"]
    rig_collection = _new_child_collection(root_collection, "Rig")
    components_collection = _new_child_collection(
        root_collection,
        "Components",
    )
    component_collection = _new_child_collection(
        components_collection,
        f"00_{Path(mesh_set_asset['path']).stem}",
    )
    component_collection["bc2_component_path"] = mesh_set_asset["path"]
    component_collection["bc2_component_role"] = "CHARACTER"
    armature_obj = _create_armature_object(
        context,
        skeleton,
        skeleton_asset,
        settings,
        collection=rig_collection,
    )
    objects = _create_mesh_objects(
        context,
        parsed,
        mesh_asset,
        settings,
        collection=component_collection,
        bone_names=tuple(bone.name for bone in skeleton.bones),
        subset_materials=subset_materials,
    )
    _bind_meshes_to_armature(objects, armature_obj)
    diagnostics_text = bpy.data.texts.new(
        f"BC2_{Path(mesh_set_asset['path']).stem}_"
        "Material_Diagnostics.json"
    )
    diagnostics_text.write(
        json.dumps(
            {
                "summary": material_summary,
                "issues": material_diagnostics,
            },
            indent=2,
            sort_keys=True,
        )
    )
    armature_obj["bc2_material_diagnostics_text"] = diagnostics_text.name
    armature_obj["bc2_resolved_material_count"] = material_summary[
        "resolved_materials"
    ]
    context.view_layer.objects.active = armature_obj
    armature_obj.select_set(True)
    return armature_obj, objects


def _mesh_component_assets(
    repository,
    component: AssemblyComponent,
    preferred_archives: tuple[str, ...],
    settings: BC2Settings,
):
    mesh_set_asset = repository.catalog.find_asset(
        component.mesh_set_path,
        preferred_archives,
    )
    if mesh_set_asset is None:
        raise BC2Error(
            f"Linked mesh set is not indexed: {component.mesh_set_path}"
        )
    _, mesh_set_raw = repository.read_asset(
        int(mesh_set_asset["id"]),
        max_size=256 << 20,
    )
    mesh_asset, mesh_raw = repository.read_asset(
        int(mesh_set_asset["id"]),
        max_size=1 << 30,
        resolve_mesh=True,
    )
    parsed = parse_meshdata(
        mesh_raw,
        mesh_asset["platform"],
        include_z_only=settings.include_z_only,
    )
    return mesh_set_asset, mesh_set_raw, mesh_asset, parsed


def _create_mesh_set_import(
    context,
    repository,
    mesh_set_asset,
    parsed: MeshAsset,
    mesh_asset,
    settings: BC2Settings,
):
    preferred_archives = tuple(
        dict.fromkeys(
            (
                str(mesh_set_asset["archive_path"]),
                str(mesh_asset["archive_path"]),
            )
        )
    )
    material_context = _source_material_context(
        repository,
        preferred_archives,
        settings,
    )
    subset_materials, _, material_summary = (
        _resolve_mesh_subset_materials(
            repository,
            mesh_set_asset,
            parsed,
            preferred_archives,
            settings,
            material_context=material_context,
        )
    )
    root_collection = _new_collection(
        context,
        Path(mesh_set_asset["path"]).stem,
    )
    root_collection["bc2_assembly_kind"] = "MESH_COMPONENT"
    root_collection["bc2_asset_path"] = mesh_set_asset["path"]
    components_collection = _new_child_collection(
        root_collection,
        "Components",
    )
    component_collection = _new_child_collection(
        components_collection,
        f"00_{Path(mesh_set_asset['path']).stem}",
    )
    component_collection["bc2_component_path"] = mesh_set_asset["path"]
    component_collection["bc2_component_role"] = (
        "VEHICLE" if parsed.mesh_type == 2 else "MESH"
    )
    split_parts = parsed.mesh_type == 2
    composite_parts = ()
    if split_parts:
        _, mesh_set_raw = repository.read_asset(
            int(mesh_set_asset["id"]),
            max_size=256 << 20,
        )
        composite_parts = parse_composite_lod_parts(
            mesh_set_raw,
            platform=str(mesh_set_asset["platform"]),
        )
        if not composite_parts:
            material_context.diagnostics.append(
                {
                    "code": "composite_part_names_unavailable",
                    "reference": str(mesh_set_asset["path"]),
                    "message": (
                        "Composite LOD0 parts use numbered fallback "
                        "collections because the mesh-set names were not found"
                    ),
                }
            )
    objects = _create_mesh_objects(
        context,
        parsed,
        mesh_asset,
        settings,
        collection=component_collection,
        subset_materials=subset_materials,
        split_parts=split_parts,
        composite_parts=composite_parts,
    )
    diagnostics_text = bpy.data.texts.new(
        f"BC2_{Path(mesh_set_asset['path']).stem}_"
        "Material_Diagnostics.json"
    )
    diagnostics_text.write(
        json.dumps(
            {
                "summary": material_summary,
                "issues": material_context.diagnostics,
            },
            indent=2,
            sort_keys=True,
        )
    )
    root_collection["bc2_material_diagnostics_text"] = (
        diagnostics_text.name
    )
    root_collection["bc2_resolved_material_count"] = material_summary[
        "resolved_materials"
    ]
    return root_collection, objects


def _create_modular_assembly(
    context,
    repository,
    assembly_asset,
    assembly: ModularAssembly,
    settings: BC2Settings,
) -> _ImportedAssembly:
    if (
        assembly.kind == "CHARACTER"
        and str(assembly_asset["platform"]) != "pc"
    ):
        raise BC2Error(
            "Native modular character import currently supports PC retail "
            "assets only"
        )

    preferred = (str(assembly_asset["archive_path"]),)
    loaded = []
    for component in assembly.components:
        loaded.append(
            (
                component,
                *_mesh_component_assets(
                    repository,
                    component,
                    preferred,
                    settings,
                ),
            )
        )
    preferred_archives = tuple(
        dict.fromkeys(
            (
                str(assembly_asset["archive_path"]),
                *(
                    str(mesh_set_asset["archive_path"])
                    for _, mesh_set_asset, _, _, _ in loaded
                ),
                *(
                    str(mesh_asset["archive_path"])
                    for _, _, _, mesh_asset, _ in loaded
                ),
            )
        )
    )
    material_context = _source_material_context(
        repository,
        preferred_archives,
        settings,
    )

    root_collection = _new_collection(
        context,
        f"BC2_{assembly.name}",
    )
    root_collection["bc2_assembly_kind"] = assembly.kind
    root_collection["bc2_assembly_path"] = assembly.source_path
    root_collection["bc2_component_count"] = len(assembly.components)
    components_collection = _new_child_collection(
        root_collection,
        "Components",
    )

    armature_obj = None
    skeleton = None
    if assembly.kind == "CHARACTER":
        skeleton_paths = tuple(
            _skeleton_path_for_resource(repository, mesh_set_asset)
            for _, mesh_set_asset, _, _, _ in loaded
        )
        if len({path.casefold() for path in skeleton_paths}) != 1:
            raise BC2Error(
                "Linked character components do not use the same skeleton"
            )
        skeleton_asset, skeleton = _read_referenced_skeleton(
            repository,
            loaded[0][1],
        )
        rig_collection = _new_child_collection(root_collection, "Rig")
        armature_obj = _create_armature_object(
            context,
            skeleton,
            skeleton_asset,
            settings,
            collection=rig_collection,
        )
        armature_obj["bc2_assembly_path"] = assembly.source_path

    imported_components = []
    component_summaries = []
    resolved_material_count = 0
    for component_index, (
        component,
        mesh_set_asset,
        mesh_set_raw,
        mesh_asset,
        parsed,
    ) in enumerate(loaded):
        component_collection = _new_child_collection(
            components_collection,
            f"{component_index:02d}_{component.name}",
        )
        component_collection["bc2_component_index"] = component_index
        component_collection["bc2_component_path"] = component.mesh_set_path
        component_collection["bc2_component_role"] = component.role
        if component.random_group is not None:
            component_collection["bc2_random_group"] = component.random_group

        subset_materials, _, summary = _resolve_mesh_subset_materials(
            repository,
            mesh_set_asset,
            parsed,
            preferred_archives,
            settings,
            shader_set_override=component.shader_set_reference,
            material_context=material_context,
        )
        split_parts = assembly.kind == "VEHICLE" and parsed.mesh_type == 2
        composite_parts = (
            parse_composite_lod_parts(
                mesh_set_raw,
                platform=str(mesh_set_asset["platform"]),
            )
            if split_parts
            else ()
        )
        if split_parts and not composite_parts:
            material_context.diagnostics.append(
                {
                    "code": "composite_part_names_unavailable",
                    "reference": component.mesh_set_path,
                    "message": (
                        "Composite LOD0 parts use numbered fallback "
                        "collections because mesh-set names were not found"
                    ),
                }
            )
        objects = _create_mesh_objects(
            context,
            parsed,
            mesh_asset,
            settings,
            collection=component_collection,
            bone_names=(
                tuple(bone.name for bone in skeleton.bones)
                if skeleton is not None
                else None
            ),
            subset_materials=subset_materials,
            split_parts=split_parts,
            composite_parts=composite_parts,
            part_states=assembly.parts,
        )
        for obj in objects:
            obj["bc2_assembly_path"] = assembly.source_path
            obj["bc2_component_path"] = component.mesh_set_path
            obj["bc2_component_role"] = component.role
        if armature_obj is not None:
            _bind_meshes_to_armature(objects, armature_obj)
        resolved_material_count += summary["resolved_materials"]
        component_summaries.append(
            {
                "name": component.name,
                "role": component.role,
                "path": component.mesh_set_path,
                **summary,
            }
        )
        imported_components.append(
            _ImportedAssemblyComponent(
                definition=component,
                collection=component_collection,
                objects=tuple(objects),
                mesh_type=parsed.mesh_type,
            )
        )

    diagnostics_text = bpy.data.texts.new(
        f"BC2_{assembly.name}_Assembly_Diagnostics.json"
    )
    diagnostics_text.write(
        json.dumps(
            {
                "assembly": {
                    "name": assembly.name,
                    "kind": assembly.kind,
                    "path": assembly.source_path,
                },
                "components": component_summaries,
                "issues": material_context.diagnostics,
            },
            indent=2,
            sort_keys=True,
        )
    )
    root_collection["bc2_material_diagnostics_text"] = diagnostics_text.name
    root_collection["bc2_resolved_material_count"] = (
        resolved_material_count
    )
    if armature_obj is not None:
        armature_obj["bc2_material_diagnostics_text"] = diagnostics_text.name
        armature_obj["bc2_resolved_material_count"] = (
            resolved_material_count
        )
        context.view_layer.objects.active = armature_obj
        armature_obj.select_set(True)
    return _ImportedAssembly(
        definition=assembly,
        root_collection=root_collection,
        armature=armature_obj,
        components=tuple(imported_components),
        diagnostics_text=diagnostics_text,
    )


def _new_action_channelbag(action, armature_obj):
    slot = action.slots.new(id_type="OBJECT", name=armature_obj.name)
    layer = action.layers.new(name="Animation")
    strip = layer.strips.new(type="KEYFRAME")
    return strip.channelbag(slot, ensure=True)


def _add_action_curve(
    channelbag,
    data_path: str,
    array_index: int,
    group_name: str,
    frames: tuple[float, ...],
    values: tuple[float, ...],
):
    curve = channelbag.fcurves.new(
        data_path,
        index=array_index,
        group_name=group_name,
    )
    curve.keyframe_points.add(len(frames))
    for point, frame, value in zip(
        curve.keyframe_points, frames, values
    ):
        point.co = (frame, value)
        point.interpolation = "LINEAR"
    curve.update()


def _create_animation_action(
    context,
    armature_obj,
    skeleton: GrannySkeleton,
    animation: GrannyAnimation,
    settings: BC2Settings,
):
    source_names = {bone.name for bone in skeleton.bones}
    armature_names = {bone.name for bone in armature_obj.data.bones}
    missing_source = [
        track.name for track in animation.tracks
        if track.name not in source_names
    ]
    missing_armature = [
        track.name for track in animation.tracks
        if track.name not in armature_names
    ]
    if missing_source or missing_armature:
        missing = sorted(set(missing_source + missing_armature))
        raise BC2Error(
            "Animation tracks do not match the selected skeleton: "
            + ", ".join(missing[:8])
        )

    source_fps = max(1, round(1.0 / animation.time_step))
    interval_count = max(1, round(animation.duration / animation.time_step))
    sample_count = interval_count + 1
    if sample_count > 1_000_000:
        raise BC2Error(
            f"Animation sample count is unsafe: {sample_count:,}"
        )
    times = tuple(
        min(animation.duration, index * animation.time_step)
        for index in range(sample_count)
    )
    frames = tuple(float(index + 1) for index in range(sample_count))
    local_rest = _skeleton_local_matrices(skeleton, settings)
    rest_by_name = {
        bone.name: local_rest[index]
        for index, bone in enumerate(skeleton.bones)
    }

    action = bpy.data.actions.new(animation.name)
    action.use_fake_user = True
    channelbag = _new_action_channelbag(action, armature_obj)
    for track in animation.tracks:
        pose_bone = armature_obj.pose.bones[track.name]
        pose_bone.rotation_mode = "QUATERNION"
        locations = []
        rotations = []
        previous_rotation = None
        for time in times:
            animated_local = _convert_skeleton_matrix(
                _source_transform_matrix(
                    track.translation.evaluate(time),
                    track.rotation_at(time),
                ),
                settings,
            )
            basis = rest_by_name[track.name].inverted_safe() @ animated_local
            location, rotation, _ = basis.decompose()
            if (
                previous_rotation is not None
                and rotation.dot(previous_rotation) < 0.0
            ):
                rotation.negate()
            locations.append(tuple(location))
            rotations.append(tuple(rotation))
            previous_rotation = rotation.copy()
        location_path = pose_bone.path_from_id("location")
        rotation_path = pose_bone.path_from_id("rotation_quaternion")
        for axis in range(3):
            _add_action_curve(
                channelbag,
                location_path,
                axis,
                track.name,
                frames,
                tuple(value[axis] for value in locations),
            )
        for axis in range(4):
            _add_action_curve(
                channelbag,
                rotation_path,
                axis,
                track.name,
                frames,
                tuple(value[axis] for value in rotations),
            )

    action["bc2_duration_seconds"] = animation.duration
    action["bc2_time_step_seconds"] = animation.time_step
    action["bc2_source_fps"] = source_fps
    action["bc2_track_count"] = len(animation.tracks)
    armature_obj.animation_data_create()
    armature_obj.animation_data.action = action
    context.scene.render.fps = source_fps
    context.scene.render.fps_base = 1.0
    context.scene.frame_start = 1
    context.scene.frame_end = sample_count
    context.scene.frame_set(1)
    return action


def _import_granny_animation(
    context,
    repository,
    resource_asset,
    raw: bytes,
    settings: BC2Settings,
):
    animation = parse_granny_animation(
        raw,
        resource_asset["platform"],
        Path(resource_asset["path"]).stem,
    )
    skeleton_asset, skeleton = _read_referenced_skeleton(
        repository, resource_asset
    )
    armature_obj = _find_skeleton_armature(skeleton_asset["path"])
    if armature_obj is None:
        armature_obj = _create_armature_object(
            context,
            skeleton,
            skeleton_asset,
            settings,
        )
    action = _create_animation_action(
        context,
        armature_obj,
        skeleton,
        animation,
        settings,
    )
    action["bc2_asset_path"] = resource_asset["path"]
    action["bc2_archive_path"] = resource_asset["archive_path"]
    return armature_obj, action


def _create_terrain_object(
    context,
    parsed: TerrainMesh,
    asset,
    settings: BC2Settings,
    collection=None,
    activate: bool = True,
    terrain_bounds: tuple[float, float, float, float] | None = None,
):
    name = Path(asset["path"]).stem
    mesh = bpy.data.meshes.new(f"{name}_Mesh")
    positions = _terrain_positions(parsed.positions, settings)
    uvs = None
    if terrain_bounds is not None:
        min_x, min_z, max_x, max_z = terrain_bounds
        width = max_x - min_x
        depth = max_z - min_z
        if width <= 0.0 or depth <= 0.0:
            raise BC2Error("Terrain material bounds are invalid")
        if _np is None:
            uvs = tuple(
                (
                    (float(position[0]) - min_x) / width,
                    (float(position[1]) - min_z) / depth,
                )
                for position in parsed.positions
            )
        else:
            source_positions = _np.asarray(
                parsed.positions,
                dtype=_np.float64,
            )
            uvs = _np.empty((len(source_positions), 2), dtype=_np.float32)
            uvs[:, 0] = (source_positions[:, 0] - min_x) / width
            uvs[:, 1] = (source_positions[:, 1] - min_z) / depth
    _populate_triangle_mesh(
        mesh,
        positions,
        parsed.faces,
        uvs=uvs,
        flip_v=settings.flip_v,
    )
    obj = bpy.data.objects.new(name, mesh)
    (collection or context.collection).objects.link(obj)
    obj["bc2_asset_path"] = asset["path"]
    obj["bc2_archive_path"] = asset["archive_path"]
    obj["bc2_source_resolution"] = parsed.source_resolution
    obj["bc2_header_endian"] = parsed.header_endian
    obj["bc2_sample_endian"] = parsed.sample_endian
    if activate:
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
    return obj


def _load_texture_image(
    repository,
    asset,
    raw: bytes,
    *,
    non_color: bool = False,
):
    converted = convert_texture(raw, asset["platform"])
    texture_directory = repository.source_cache / "textures"
    texture_directory.mkdir(parents=True, exist_ok=True)
    dds_path = texture_directory / safe_cache_name(asset["path"], ".dds")
    if not dds_path.exists() or dds_path.stat().st_size != len(converted.dds):
        temporary = dds_path.with_name(dds_path.name + ".partial")
        temporary.write_bytes(converted.dds)
        os.replace(temporary, dds_path)
    image = bpy.data.images.load(str(dds_path), check_existing=True)
    image.name = Path(asset["path"]).stem
    image["bc2_asset_path"] = asset["path"]
    image["bc2_archive_path"] = asset["archive_path"]
    image["bc2_texture_format"] = converted.format_name
    if non_color:
        image.colorspace_settings.name = "Non-Color"
    return image, converted


def _load_terrain_material_map_image(
    repository,
    asset,
    material_map: TerrainMaterialMap,
):
    dds = create_luminance_dds(
        material_map.side,
        material_map.side,
        material_map.indices,
    )
    texture_directory = repository.source_cache / "textures"
    texture_directory.mkdir(parents=True, exist_ok=True)
    dds_path = texture_directory / safe_cache_name(
        asset["path"] + "|material-indices",
        ".dds",
    )
    if not dds_path.exists() or dds_path.stat().st_size != len(dds):
        temporary = dds_path.with_name(dds_path.name + ".partial")
        temporary.write_bytes(dds)
        os.replace(temporary, dds_path)
    image = bpy.data.images.load(str(dds_path), check_existing=True)
    image.name = Path(asset["path"]).stem + " Material Indices"
    image.colorspace_settings.name = "Non-Color"
    image["bc2_asset_path"] = asset["path"]
    image["bc2_archive_path"] = asset["archive_path"]
    image["bc2_texture_format"] = "TerrainMaterialMap L8"
    image["bc2_material_names"] = json.dumps(
        material_map.names,
        separators=(",", ":"),
    )
    return image


def _import_texture(context, repository, asset, raw: bytes):
    image, converted = _load_texture_image(repository, asset, raw)
    material = bpy.data.materials.new(Path(asset["path"]).stem)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    shader = next(
        (node for node in nodes if node.type == "BSDF_PRINCIPLED"), None
    )
    texture_node = nodes.new("ShaderNodeTexImage")
    texture_node.image = image
    if shader:
        base_color = shader.inputs.get("Base Color")
        if base_color:
            material.node_tree.links.new(texture_node.outputs["Color"], base_color)
        alpha = shader.inputs.get("Alpha")
        if alpha:
            material.node_tree.links.new(texture_node.outputs["Alpha"], alpha)
    active = context.active_object
    if active and active.type == "MESH":
        active.data.materials.append(material)
    return image, material, converted


def _normalized_surface_name(name: str) -> str:
    return "".join(character for character in name.casefold() if character.isalnum())


def _combined_level_mesh(
    parsed: MeshAsset,
    name: str,
    settings: BC2Settings,
):
    positions = []
    faces = []
    uvs = []
    secondary_uvs = []
    has_secondary_uvs = False
    additional_uvs = [
        []
        for _ in range(
            max(
                (len(subset.additional_uvs) for subset in parsed.subsets),
                default=0,
            )
        )
    ]
    has_additional_uvs = [False] * len(additional_uvs)
    material_indices = []
    for subset_index, subset in enumerate(parsed.subsets):
        vertex_offset = len(positions)
        converted_positions = _mesh_positions(subset.positions, settings)
        positions.extend(converted_positions)
        uvs.extend(subset.uvs)
        if subset.secondary_uvs:
            secondary_uvs.extend(subset.secondary_uvs)
            has_secondary_uvs = True
        else:
            secondary_uvs.extend(
                (0.0, 0.0) for _ in subset.positions
            )
        for channel_index, channel_values in enumerate(additional_uvs):
            if channel_index < len(subset.additional_uvs):
                channel_values.extend(
                    subset.additional_uvs[channel_index]
                )
                has_additional_uvs[channel_index] = True
            else:
                channel_values.extend(
                    (0.0, 0.0) for _ in subset.positions
                )
        faces.extend(
            tuple(vertex_offset + vertex for vertex in face)
            for face in subset.faces
        )
        material_indices.extend([subset_index] * len(subset.faces))
    mesh = bpy.data.meshes.new(name)
    _populate_triangle_mesh(
        mesh,
        positions,
        faces,
        uvs=uvs,
        material_indices=material_indices,
        flip_v=settings.flip_v,
        smooth=True,
    )
    if has_secondary_uvs:
        _add_uv_layer(
            mesh,
            "UV1",
            secondary_uvs,
            flip_v=settings.flip_v,
        )
    for channel_index, (channel_values, present) in enumerate(
        zip(additional_uvs, has_additional_uvs),
        start=2,
    ):
        if present:
            _add_uv_layer(
                mesh,
                f"UV{channel_index}",
                channel_values,
                flip_v=settings.flip_v,
            )
    mesh["bc2_uv_offsets"] = json.dumps(
        [subset.uv_offsets for subset in parsed.subsets],
        separators=(",", ":"),
    )
    return mesh


def _surface_assignments(
    parsed: MeshAsset,
    definition: MeshDefinition,
):
    normalized_surfaces = []
    by_name = {}
    for surface in definition.surfaces:
        normalized = _normalized_surface_name(surface.name)
        normalized_surfaces.append((surface, normalized))
        by_name.setdefault(normalized, []).append(surface)

    def longest_match(candidates):
        if not candidates:
            return None
        longest = max(len(normalized) for _, normalized in candidates)
        winners = [
            surface
            for surface, normalized in candidates
            if len(normalized) == longest
        ]
        return winners[0] if len(winners) == 1 else None

    assignments = []
    same_count = len(parsed.subsets) == len(definition.surfaces)
    for index, subset in enumerate(parsed.subsets):
        subset_name = _normalized_surface_name(subset.name)
        matches = by_name.get(subset_name, ())
        if len(matches) == 1:
            assignments.append((matches[0], "name"))
            continue
        suffix = longest_match(
            [
                (surface, normalized)
                for surface, normalized in normalized_surfaces
                if normalized and subset_name.endswith(normalized)
            ]
        )
        if suffix is not None:
            assignments.append((suffix, "suffix"))
            continue
        contained = longest_match(
            [
                (surface, normalized)
                for surface, normalized in normalized_surfaces
                if normalized and normalized in subset_name
            ]
        )
        if contained is not None:
            assignments.append((contained, "contained"))
        elif len(parsed.subsets) == 1 and len(definition.surfaces) == 1:
            assignments.append((definition.surfaces[0], "single"))
        elif same_count:
            assignments.append((definition.surfaces[index], "index"))
        else:
            assignments.append((None, "unresolved"))
    return tuple(assignments)


def _material_nodes(material):
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (900, 0)
    shader = nodes.new("ShaderNodeBsdfPrincipled")
    shader.location = (600, 0)
    shader.inputs["Roughness"].default_value = 0.6
    material.node_tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
    return nodes, material.node_tree.links, shader


def _diagnostic_material(
    cache: dict[str, object],
    key: str,
    name: str,
    message: str,
):
    cache_key = "diagnostic|" + key.casefold()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    material = bpy.data.materials.new(name)
    material.diffuse_color = (0.8, 0.05, 0.8, 1.0)
    _, _, shader = _material_nodes(material)
    shader.inputs["Base Color"].default_value = (0.8, 0.05, 0.8, 1.0)
    material["bc2_diagnostic"] = message
    cache[cache_key] = material
    return material


def _texture_role(
    path: str,
    parameters: tuple[str, ...] = (),
) -> str:
    stem = PurePosixPath(path).name.casefold()
    if stem.endswith(".res"):
        stem = stem[:-4]
    if (
        stem.startswith("nm_")
        or stem.endswith("_nm")
        or stem.endswith("_n")
        or stem.endswith("_normal")
        or stem.endswith("_normalmap")
    ):
        return "NORMAL"
    if (
        stem.startswith("msk_")
        or "mask" in stem
    ):
        return "MASK"
    if "decal" in stem and "atlas" in stem:
        return "DECAL"
    if any(
        value in stem
        for value in (
            "ambientocclusion",
            "occlusion",
            "_oc_",
            "_ocrgb",
            "_oc_rbg",
            "skyoc",
        )
    ):
        return "OCCLUSION"
    if stem.startswith("dt_") or "detail" in stem:
        return "DETAIL"
    if "noise" in stem:
        return "NOISE"
    if stem.endswith(("_rough", "_roughness")):
        return "ROUGHNESS"
    if stem.endswith(("_s", "_sp", "_spec", "_specular")):
        return "SPECULAR"
    if stem.endswith(
        (
            "_d",
            "_diff",
            "_diffuse",
            "_albedo",
            "_color",
            "_c",
        )
    ):
        return "COLOR"
    reflected = " ".join(parameters).casefold()
    if "normal" in reflected:
        return "NORMAL"
    if "roughness" in reflected:
        return "ROUGHNESS"
    if "specular" in reflected:
        return "SPECULAR"
    if "occlusion" in reflected:
        return "OCCLUSION"
    if "mask" in reflected:
        return "MASK"
    if "detail" in reflected:
        return "DETAIL"
    if "decal" in reflected:
        return "DECAL"
    if "color" in reflected:
        return "COLOR"
    return "UNKNOWN"


def _normal_texture_identity(path: str) -> str:
    source = PurePosixPath(path)
    stem = source.name.casefold().removesuffix(".res")
    if stem.startswith("nm_"):
        stem = stem[3:]
    else:
        for suffix in ("_normalmap", "_normal", "_nm", "_n"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
    return f"{source.parent.as_posix().casefold()}/{stem}"


def _binding_uv_map(path: str, role: str) -> str:
    return "UVMap"


def _verified_detail_repeat(
    binding: ShaderTextureBinding,
) -> tuple[float, float]:
    stem = (
        PurePosixPath(binding.path)
        .name.casefold()
        .removesuffix(".res")
    )
    scale = {
        "dt_wirefence_001": 15.0,
        "dt_razorwire": 10.0,
    }.get(stem, 1.0)
    return scale, scale


def _binding_roles(bindings):
    roles = [
        _texture_role(binding.path, binding.parameters)
        for binding in bindings
    ]
    normal_identities = {
        _normal_texture_identity(binding.path)
        for binding, role in zip(bindings, roles)
        if role == "NORMAL"
    }
    for index, (binding, role) in enumerate(zip(bindings, roles)):
        if (
            role == "UNKNOWN"
            and _normal_texture_identity(binding.path) in normal_identities
        ):
            roles[index] = "COLOR"

    if "COLOR" not in roles:
        for index, (binding, role) in enumerate(zip(bindings, roles)):
            if role != "UNKNOWN":
                continue
            parameters = {
                value.casefold()
                for value in binding.parameters
            }
            if "texture_texture" in parameters:
                roles[index] = "COLOR"

    if "COLOR" not in roles:
        candidates = {}
        for index, (binding, role) in enumerate(zip(bindings, roles)):
            if role == "UNKNOWN":
                candidates.setdefault(binding.path.casefold(), index)
        if len(candidates) == 1:
            roles[next(iter(candidates.values()))] = "COLOR"
    return tuple(roles)


def _verified_surface_family(surface: SurfaceShader) -> str | None:
    partition = surface.reference.rsplit("/", 1)[0].casefold()
    return {
        "objects/vehicles/air/ah60/shaders/main": "AH60_MAIN",
        "objects/vehicles/air/ah60/shaders/glass": "AH60_GLASS",
        "objects/vehicles/air/ah60/shaders/interior": "AH60_INTERIOR",
        "objects/vehicles/air/ah60/shaders/cockpitinterior": (
            "AH60_COCKPIT_INTERIOR"
        ),
        "objects/weapons/stationary/minigun/shaders/main": "MINIGUN_MAIN",
    }.get(partition)


def _surface_binding_roles(surface: SurfaceShader, bindings):
    roles = list(_binding_roles(bindings))
    family = _verified_surface_family(surface)
    if family == "AH60_MAIN":
        verified_roles = {
            1: "COLOR",
            2: "COLOR",
            3: "MASK",
            4: "DETAIL",
            5: "MASK",
        }
        for index, binding in enumerate(bindings):
            roles[index] = verified_roles.get(binding.index, roles[index])
    elif family == "MINIGUN_MAIN":
        for index, binding in enumerate(bindings):
            if binding.index == 2:
                roles[index] = "COLOR"
            elif binding.index == 3:
                roles[index] = "MASK"
            elif binding.index == 4:
                roles[index] = "SPECULAR"
    return tuple(roles)


def _surface_binding_uv_map(
    surface: SurfaceShader,
    binding: ShaderTextureBinding,
    role: str,
    layered_static: bool = False,
) -> str:
    family = _verified_surface_family(surface)
    if family == "AH60_MAIN":
        verified_uv_maps = {
            1: "UV1",
            2: "UV2",
            3: "UV3",
            4: "UVMap",
            5: "UV1",
        }
        if binding.index in verified_uv_maps:
            return verified_uv_maps[binding.index]
    if family == "MINIGUN_MAIN":
        if binding.index == 2:
            return "UV1"
        if binding.index in {3, 4}:
            return "UVMap"
    if role == "DETAIL" and _verified_detail_repeat(binding) != (1.0, 1.0):
        return "UV1"
    parameters = {value.casefold() for value in binding.parameters}
    if role == "COLOR" and "texture_texture7" in parameters:
        return "UV1"
    if "dirtmaskrgb" in PurePosixPath(binding.path).name.casefold():
        return "UV2"
    if layered_static:
        if role == "OCCLUSION":
            return "UVMap"
        if role == "DETAIL":
            reflected = {
                value.casefold() for value in binding.parameters
            }
            if any("detail" in value for value in reflected):
                return "UV1"
            return "UV2" if binding.index == 5 else "UV1"
        if role == "DECAL":
            return "UV2"
        if role == "NORMAL":
            if "/_atlas/" in binding.path.replace("\\", "/").casefold():
                return "UV3"
            return "UV1"
    return _binding_uv_map(binding.path, role)


def _surface_binding_uv_scale(
    surface: SurfaceShader,
    binding: ShaderTextureBinding,
) -> tuple[float, float]:
    family = _verified_surface_family(surface)
    if family == "AH60_MAIN" and binding.index == 5:
        return (15.0, 15.0)
    if family == "MINIGUN_MAIN" and binding.index == 3:
        return (13.0, 13.0)
    return _verified_detail_repeat(binding)


_CHARACTER_NON_COLOR_ROLES = {
    "NORMAL",
    "DETAIL_NORMAL",
    "DIRT_NORMAL",
    "DIRT_MASK",
    "MASK",
    "OCCLUSION",
    "ROUGHNESS",
    "SPECULAR",
}


def _texture_stem(path: str) -> str:
    return PurePosixPath(path).name.casefold().removesuffix(".res")


def _character_binding_roles(
    surface: SurfaceShader,
    bindings,
) -> tuple[str, ...]:
    roles = []
    for binding in bindings:
        stem = _texture_stem(binding.path)
        if "occlusion" in stem or "ambientocclusion" in stem:
            role = "OCCLUSION"
        elif "dirtmap" in stem:
            if stem.endswith(("_nm", "_n", "_normal")):
                role = "DIRT_NORMAL"
            elif stem.endswith(("_c", "_color")):
                role = "DIRT_COLOR"
            else:
                role = "DIRT_MASK"
        elif stem.endswith(("_nm", "_n", "_normal", "_normalmap")):
            role = (
                "DETAIL_NORMAL"
                if "detail" in stem
                else "NORMAL"
            )
        elif "detail" in stem:
            role = "DETAIL_COLOR"
        elif "camo" in stem:
            role = "CAMO"
        elif "mask" in stem:
            role = "MASK"
        elif stem.endswith(("_s", "_sp", "_spec", "_specular")):
            role = "SPECULAR"
        else:
            role = "COLOR_CANDIDATE"
        roles.append(role)

    normal_stems = {
        _texture_stem(binding.path).removesuffix("_normalmap")
        .removesuffix("_normal")
        .removesuffix("_nm")
        .removesuffix("_n")
        for binding, role in zip(bindings, roles)
        if role == "NORMAL"
    }
    surface_tokens = tuple(
        token
        for token in surface.name.casefold().replace("-", "_").split("_")
        if len(token) >= 3 and token not in {"mesh", "body", "standard"}
    )
    candidates = [
        index
        for index, role in enumerate(roles)
        if role == "COLOR_CANDIDATE"
    ]
    if candidates:
        def score(index: int) -> tuple[int, int]:
            stem = _texture_stem(bindings[index].path)
            normalized = stem.removesuffix("_low").removesuffix("_c")
            value = sum(
                len(token) * 10
                for token in surface_tokens
                if token in normalized
            )
            if any(
                normalized == normal
                or normalized.startswith(normal + "_")
                or normal.startswith(normalized + "_")
                for normal in normal_stems
            ):
                value += 500
            if stem.endswith("_c"):
                value += 5
            return value, -index

        primary = max(candidates, key=score)
        for index in candidates:
            roles[index] = (
                "BASE_COLOR" if index == primary else "AUX_COLOR"
            )
    return tuple(roles)


def _is_character_surface(surface: SurfaceShader) -> bool:
    return surface.database_key.casefold().startswith("characters/")


def _character_binding_uv_map(
    surface: SurfaceShader,
    role: str,
) -> str:
    name = _normalized_surface_name(surface.name)
    collapsed_coordinate_surfaces = (
        "legstandard",
        "molle",
        "pouches",
    )
    if role == "OCCLUSION":
        return "UV3"
    if role == "CAMO":
        return (
            "UV2"
            if any(
                value in name
                for value in ("armstandard", "legstandard")
            )
            else "UV1"
        )
    if role == "DIRT_MASK":
        return "UV2"
    if role in {"DETAIL_COLOR", "DETAIL_NORMAL"} and any(
        value in name
        for value in collapsed_coordinate_surfaces
    ):
        return "UV2"
    if (
        role in {"BASE_COLOR", "DIRT_COLOR"}
        and name.endswith("meshberetta")
        and "holster" not in name
    ):
        return "UV1"
    return "UVMap"


def _character_binding_uv_scale(
    surface: SurfaceShader,
    role: str,
) -> tuple[float, float]:
    name = _normalized_surface_name(surface.name)
    if role == "DIRT_COLOR":
        return 15.0, 15.0
    if role == "CAMO":
        if "armstandard" in name:
            return 2.0, 3.0
        if "legstandard" in name:
            return 2.0, 4.0
    if role in {"DETAIL_COLOR", "DETAIL_NORMAL"}:
        repeats = (
            ("armstandard", 7.0),
            ("legstandard", 8.0),
            ("gloves", 10.0),
            ("molle", 5.0),
            ("pouches", 5.0),
            ("kits", 15.0),
        )
        for marker, scale in repeats:
            if marker in name:
                return scale, scale
    return 1.0, 1.0


def _unique_role_nodes(texture_nodes, role: str):
    result = []
    seen = set()
    for binding, binding_role, node in texture_nodes:
        identity = binding.path.casefold()
        if binding_role == role and identity not in seen:
            result.append((binding, node))
            seen.add(identity)
    return result


def _layered_color_output(nodes, links, texture_nodes):
    colors = _unique_role_nodes(texture_nodes, "COLOR")
    details = _unique_role_nodes(texture_nodes, "DETAIL")
    occlusion = _unique_role_nodes(texture_nodes, "OCCLUSION")
    if colors:
        color_output = colors[0][1].outputs["Color"]
        color_node = colors[0][1]
        model = "reflected primary color"
    elif details:
        color_output = details[0][1].outputs["Color"]
        color_node = details[0][1]
        model = "reflected detail color"
    else:
        return None, None, "no color-producing binding"

    if not colors and len(details) > 1:
        control_outputs = ()
        if occlusion:
            separate = nodes.new("ShaderNodeSeparateColor")
            separate.name = "BC2 OcRGB layer controls"
            separate.label = "OcRGB layer controls"
            separate.location = (0, 500)
            links.new(
                occlusion[0][1].outputs["Color"],
                separate.inputs["Color"],
            )
            control_outputs = (
                separate.outputs["Red"],
                separate.outputs["Green"],
                separate.outputs["Blue"],
            )
        for index, (_, detail_node) in enumerate(details[1:]):
            mix = nodes.new("ShaderNodeMixRGB")
            mix.name = f"BC2 detail layer {index + 2}"
            mix.label = f"BC2 detail layer {index + 2}"
            mix.blend_type = "MIX"
            mix.location = (120 + index * 180, 300)
            mix.inputs["Fac"].default_value = 0.5
            links.new(color_output, mix.inputs["Color1"])
            links.new(detail_node.outputs["Color"], mix.inputs["Color2"])
            if index < len(control_outputs):
                links.new(control_outputs[index], mix.inputs["Fac"])
            color_output = mix.outputs["Color"]
        model = "OcRGB-controlled reflected detail layers"

    if occlusion:
        occlusion_binding, occlusion_node = occlusion[0]
        stem = PurePosixPath(
            occlusion_binding.path
        ).name.casefold()
        if "ocrgb" in stem or "oc_rgb" in stem or "_oc_" in stem:
            occlusion_output = occlusion_node.outputs["Alpha"]
        else:
            separate = nodes.new("ShaderNodeSeparateColor")
            separate.name = "BC2 ambient occlusion"
            separate.label = "Ambient occlusion"
            separate.location = (180, 80)
            links.new(
                occlusion_node.outputs["Color"],
                separate.inputs["Color"],
            )
            occlusion_output = separate.outputs["Red"]
        multiply = nodes.new("ShaderNodeMixRGB")
        multiply.name = "BC2 color x ambient occlusion"
        multiply.label = "Color x ambient occlusion"
        multiply.blend_type = "MULTIPLY"
        multiply.location = (420, 200)
        multiply.inputs["Fac"].default_value = 1.0
        links.new(color_output, multiply.inputs["Color1"])
        links.new(occlusion_output, multiply.inputs["Color2"])
        color_output = multiply.outputs["Color"]
        model += " with source ambient occlusion"
    return color_output, color_node, model


def _signed_channel(nodes, links, source, name: str, location):
    multiply = nodes.new("ShaderNodeMath")
    multiply.name = f"{name} x 2"
    multiply.label = f"{name} x 2"
    multiply.operation = "MULTIPLY"
    multiply.location = location
    multiply.inputs[1].default_value = 2.0
    links.new(source, multiply.inputs[0])
    subtract = nodes.new("ShaderNodeMath")
    subtract.name = f"{name} signed"
    subtract.label = f"{name} - 1"
    subtract.operation = "SUBTRACT"
    subtract.location = (location[0] + 160, location[1])
    subtract.inputs[1].default_value = 1.0
    links.new(multiply.outputs[0], subtract.inputs[0])
    return subtract.outputs[0]


def _packed_normal_output(
    nodes,
    links,
    image_node,
    *,
    x_channel: str,
    y_channel: str,
    label: str,
    uv_scale: float = 1.0,
    detail_weight: float | None = None,
):
    if uv_scale != 1.0:
        coordinates = nodes.new("ShaderNodeTexCoord")
        coordinates.location = (-1000, 260)
        scale = nodes.new("ShaderNodeVectorMath")
        scale.operation = "MULTIPLY"
        scale.location = (-820, 260)
        scale.inputs[1].default_value = (uv_scale, uv_scale, 1.0)
        links.new(coordinates.outputs["UV"], scale.inputs[0])
        links.new(scale.outputs["Vector"], image_node.inputs["Vector"])

    separate = nodes.new("ShaderNodeSeparateColor")
    separate.location = (-480, -100)
    links.new(image_node.outputs["Color"], separate.inputs["Color"])
    channels = {
        "RED": separate.outputs["Red"],
        "GREEN": separate.outputs["Green"],
        "BLUE": separate.outputs["Blue"],
        "ALPHA": image_node.outputs["Alpha"],
    }
    x_output = _signed_channel(
        nodes,
        links,
        channels[x_channel],
        f"{label} X",
        (-280, 120),
    )
    y_output = _signed_channel(
        nodes,
        links,
        channels[y_channel],
        f"{label} Y",
        (-280, -120),
    )

    x_square = nodes.new("ShaderNodeMath")
    x_square.operation = "MULTIPLY"
    x_square.location = (50, 140)
    links.new(x_output, x_square.inputs[0])
    links.new(x_output, x_square.inputs[1])
    y_square = nodes.new("ShaderNodeMath")
    y_square.operation = "MULTIPLY"
    y_square.location = (50, -80)
    links.new(y_output, y_square.inputs[0])
    links.new(y_output, y_square.inputs[1])
    sum_xy = nodes.new("ShaderNodeMath")
    sum_xy.operation = "ADD"
    sum_xy.location = (220, 40)
    links.new(x_square.outputs[0], sum_xy.inputs[0])
    links.new(y_square.outputs[0], sum_xy.inputs[1])
    one_minus = nodes.new("ShaderNodeMath")
    one_minus.operation = "SUBTRACT"
    one_minus.location = (380, 40)
    one_minus.inputs[0].default_value = 1.0
    links.new(sum_xy.outputs[0], one_minus.inputs[1])
    maximum = nodes.new("ShaderNodeMath")
    maximum.operation = "MAXIMUM"
    maximum.location = (540, 40)
    maximum.inputs[1].default_value = 0.0
    links.new(one_minus.outputs[0], maximum.inputs[0])
    square_root = nodes.new("ShaderNodeMath")
    square_root.operation = "SQRT"
    square_root.location = (700, 40)
    links.new(maximum.outputs[0], square_root.inputs[0])

    combine = nodes.new("ShaderNodeCombineXYZ")
    combine.location = (860, 40)
    links.new(x_output, combine.inputs["X"])
    links.new(y_output, combine.inputs["Y"])
    links.new(square_root.outputs[0], combine.inputs["Z"])
    encode_scale = nodes.new("ShaderNodeVectorMath")
    encode_scale.operation = "SCALE"
    encode_scale.location = (1020, 40)
    encode_scale.inputs["Scale"].default_value = 0.5
    links.new(combine.outputs["Vector"], encode_scale.inputs[0])
    encode_add = nodes.new("ShaderNodeVectorMath")
    encode_add.operation = "ADD"
    encode_add.location = (1180, 40)
    encode_add.inputs[1].default_value = (0.5, 0.5, 0.5)
    links.new(encode_scale.outputs["Vector"], encode_add.inputs[0])
    normal_map = nodes.new("ShaderNodeNormalMap")
    normal_map.location = (1340, 40)
    normal_map.space = "TANGENT"
    normal_map.inputs["Strength"].default_value = 1.0
    links.new(encode_add.outputs["Vector"], normal_map.inputs["Color"])
    if detail_weight is None:
        return normal_map.outputs["Normal"]

    detail_scale = nodes.new("ShaderNodeVectorMath")
    detail_scale.operation = "SCALE"
    detail_scale.location = (1500, 40)
    detail_scale.inputs["Scale"].default_value = detail_weight
    links.new(normal_map.outputs["Normal"], detail_scale.inputs[0])
    geometry = nodes.new("ShaderNodeNewGeometry")
    geometry.location = (1500, -160)
    add = nodes.new("ShaderNodeVectorMath")
    add.operation = "ADD"
    add.location = (1660, 40)
    links.new(geometry.outputs["Normal"], add.inputs[0])
    links.new(detail_scale.outputs["Vector"], add.inputs[1])
    normalize = nodes.new("ShaderNodeVectorMath")
    normalize.operation = "NORMALIZE"
    normalize.location = (1820, 40)
    links.new(add.outputs["Vector"], normalize.inputs[0])
    return normalize.outputs["Vector"]


def _dxt5nm_output(
    nodes,
    links,
    image_node,
    *,
    uv_scale: float = 1.0,
    detail_weight: float | None = None,
):
    return _packed_normal_output(
        nodes,
        links,
        image_node,
        x_channel="ALPHA",
        y_channel="GREEN",
        label="DXT5nm",
        uv_scale=uv_scale,
        detail_weight=detail_weight,
    )


def _rg_normal_output(nodes, links, image_node):
    return _packed_normal_output(
        nodes,
        links,
        image_node,
        x_channel="RED",
        y_channel="GREEN",
        label="Character RG normal",
    )


def _standard_normal_output(nodes, links, image_node):
    normal_map = nodes.new("ShaderNodeNormalMap")
    normal_map.location = (300, -260)
    normal_map.space = "TANGENT"
    normal_map.inputs["Strength"].default_value = 1.0
    links.new(image_node.outputs["Color"], normal_map.inputs["Color"])
    return normal_map.outputs["Normal"]


def _character_color_output(nodes, links, texture_nodes):
    base_colors = _unique_role_nodes(texture_nodes, "BASE_COLOR")
    if not base_colors:
        return None, None, "no character base-color binding"
    base_node = base_colors[0][1]
    color_output = base_node.outputs["Color"]
    model_parts = ["shader-selected base color"]

    camo = _unique_role_nodes(texture_nodes, "CAMO")
    detail = _unique_role_nodes(texture_nodes, "DETAIL_COLOR")
    if camo:
        multiply = nodes.new("ShaderNodeMixRGB")
        multiply.name = "BC2 character base x camouflage"
        multiply.label = "Base x camouflage"
        multiply.blend_type = "MULTIPLY"
        multiply.inputs["Fac"].default_value = 1.0
        links.new(color_output, multiply.inputs["Color1"])
        links.new(camo[0][1].outputs["Color"], multiply.inputs["Color2"])
        color_output = multiply.outputs["Color"]
        model_parts.append("camouflage modulation")

    if detail:
        combine = nodes.new("ShaderNodeMixRGB")
        combine.name = "BC2 character cloth detail"
        combine.label = "Repeated cloth detail"
        if camo:
            combine.blend_type = "MULTIPLY"
            combine.inputs["Fac"].default_value = 1.0
        else:
            combine.blend_type = "OVERLAY"
            combine.inputs["Fac"].default_value = 0.35
        links.new(color_output, combine.inputs["Color1"])
        links.new(detail[0][1].outputs["Color"], combine.inputs["Color2"])
        color_output = combine.outputs["Color"]
        if camo:
            scale = nodes.new("ShaderNodeVectorMath")
            scale.name = "BC2 character detail x2"
            scale.label = "Shader detail gain x2"
            scale.operation = "SCALE"
            scale.inputs["Scale"].default_value = 2.0
            links.new(color_output, scale.inputs[0])
            color_output = scale.outputs["Vector"]
        model_parts.append("repeated cloth detail")
    elif camo:
        scale = nodes.new("ShaderNodeVectorMath")
        scale.name = "BC2 character camouflage x2"
        scale.label = "Shader camouflage gain x2"
        scale.operation = "SCALE"
        scale.inputs["Scale"].default_value = 2.0
        links.new(color_output, scale.inputs[0])
        color_output = scale.outputs["Vector"]

    dirt_colors = _unique_role_nodes(texture_nodes, "DIRT_COLOR")
    dirt_masks = _unique_role_nodes(texture_nodes, "DIRT_MASK")
    if dirt_colors and dirt_masks:
        dirt_scale = nodes.new("ShaderNodeVectorMath")
        dirt_scale.name = "BC2 character dirt x2.5"
        dirt_scale.label = "Shader dirt gain x2.5"
        dirt_scale.operation = "SCALE"
        dirt_scale.inputs["Scale"].default_value = 2.5
        links.new(dirt_colors[0][1].outputs["Color"], dirt_scale.inputs[0])
        separate = nodes.new("ShaderNodeSeparateColor")
        separate.name = "BC2 character dirt mask"
        separate.label = "Dirt mask red"
        links.new(dirt_masks[0][1].outputs["Color"], separate.inputs["Color"])
        mix = nodes.new("ShaderNodeMixRGB")
        mix.name = "BC2 character dirt blend"
        mix.label = "Dirt to clean surface"
        mix.blend_type = "MIX"
        links.new(separate.outputs["Red"], mix.inputs["Fac"])
        links.new(dirt_scale.outputs["Vector"], mix.inputs["Color1"])
        links.new(color_output, mix.inputs["Color2"])
        color_output = mix.outputs["Color"]
        model_parts.append("UV-masked dirt blend")

    occlusion = _unique_role_nodes(texture_nodes, "OCCLUSION")
    if occlusion:
        separate = nodes.new("ShaderNodeSeparateColor")
        separate.name = "BC2 character occlusion"
        separate.label = "Character occlusion red"
        links.new(occlusion[0][1].outputs["Color"], separate.inputs["Color"])
        multiply = nodes.new("ShaderNodeMixRGB")
        multiply.name = "BC2 character color x occlusion"
        multiply.label = "Color x baked occlusion"
        multiply.blend_type = "MULTIPLY"
        multiply.inputs["Fac"].default_value = 1.0
        links.new(color_output, multiply.inputs["Color1"])
        links.new(separate.outputs["Red"], multiply.inputs["Color2"])
        color_output = multiply.outputs["Color"]
        model_parts.append("baked occlusion")
    return color_output, base_node, ", ".join(model_parts)


def _character_normal_output(nodes, links, texture_nodes):
    primary = _unique_role_nodes(texture_nodes, "NORMAL")
    if not primary:
        return None, "no character normal binding"
    normal_output = _rg_normal_output(nodes, links, primary[0][1])
    model = "verified PC character primary normal in red/green"
    detail = _unique_role_nodes(texture_nodes, "DETAIL_NORMAL")
    if detail:
        detail_output = _dxt5nm_output(nodes, links, detail[0][1])
        geometry = nodes.new("ShaderNodeNewGeometry")
        geometry.name = "BC2 character geometric normal"
        geometry.label = "Geometric normal"
        subtract = nodes.new("ShaderNodeVectorMath")
        subtract.name = "BC2 character detail normal delta"
        subtract.label = "Detail normal delta"
        subtract.operation = "SUBTRACT"
        links.new(detail_output, subtract.inputs[0])
        links.new(geometry.outputs["Normal"], subtract.inputs[1])
        add = nodes.new("ShaderNodeVectorMath")
        add.name = "BC2 character combined normal"
        add.label = "Primary + detail delta"
        add.operation = "ADD"
        links.new(normal_output, add.inputs[0])
        links.new(subtract.outputs["Vector"], add.inputs[1])
        normalize = nodes.new("ShaderNodeVectorMath")
        normalize.name = "BC2 character normalized normal"
        normalize.label = "Normalize combined normal"
        normalize.operation = "NORMALIZE"
        links.new(add.outputs["Vector"], normalize.inputs[0])
        normal_output = normalize.outputs["Vector"]
        model += ", alpha/green repeated detail approximation"
    return normal_output, model


def _configure_character_principled(
    surface: SurfaceShader,
    nodes,
    links,
    shader,
    texture_nodes,
):
    name = _normalized_surface_name(surface.name)
    roughness = 0.62
    metallic = 0.0
    if any(value in name for value in ("buckles", "beretta")):
        roughness = 0.3
        metallic = 0.35
    elif any(
        value in name
        for value in ("boots", "gloves", "holster", "pads", "grenades")
    ):
        roughness = 0.48
    shader.inputs["Roughness"].default_value = roughness
    shader.inputs["Metallic"].default_value = metallic

    specular = _unique_role_nodes(texture_nodes, "SPECULAR")
    specular_input = shader.inputs.get("Specular IOR Level")
    if specular and specular_input is not None:
        grayscale = nodes.new("ShaderNodeRGBToBW")
        grayscale.name = "BC2 character specular level"
        grayscale.label = "Source specular level"
        links.new(specular[0][1].outputs["Color"], grayscale.inputs["Color"])
        links.new(grayscale.outputs["Val"], specular_input)


def _texture_asset(repository, path: str, preferred_archives):
    candidates = (path,) if path.casefold().endswith(".res") else (
        path + ".res",
        path,
    )
    for candidate in candidates:
        asset = repository.catalog.find_asset(
            candidate,
            preferred_archives,
        )
        if asset is not None:
            return asset
    return None


def _binding_texture_node(texture_nodes, binding_index: int):
    return next(
        (
            node
            for binding, _, node in texture_nodes
            if binding.index == binding_index
        ),
        None,
    )


def _configure_verified_surface_material(
    material,
    surface: SurfaceShader,
    nodes,
    links,
    shader,
    texture_nodes,
) -> bool:
    family = _verified_surface_family(surface)
    if family == "AH60_MAIN":
        primary = _binding_texture_node(texture_nodes, 1)
        if primary is None:
            return False
        color_output = primary.outputs["Color"]

        symbols = _binding_texture_node(texture_nodes, 2)
        if symbols is not None:
            symbols_mix = nodes.new("ShaderNodeMixRGB")
            symbols_mix.name = "BC2 AH60 main symbols"
            symbols_mix.label = "Main color to Symbols_Us by alpha"
            symbols_mix.blend_type = "MIX"
            symbols_mix.location = (-20, 180)
            links.new(symbols.outputs["Alpha"], symbols_mix.inputs["Fac"])
            links.new(color_output, symbols_mix.inputs["Color1"])
            links.new(symbols.outputs["Color"], symbols_mix.inputs["Color2"])
            color_output = symbols_mix.outputs["Color"]

        dirt = _binding_texture_node(texture_nodes, 3)
        if dirt is not None:
            channels = nodes.new("ShaderNodeSeparateColor")
            channels.name = "BC2 AH60 main dirt channels"
            channels.label = "DirtMaskRGB blue"
            channels.location = (-220, -80)
            links.new(dirt.outputs["Color"], channels.inputs["Color"])
            dirt_scale = nodes.new("ShaderNodeMath")
            dirt_scale.name = "BC2 AH60 main dirt scale"
            dirt_scale.label = "Dirt blue x 0.1"
            dirt_scale.operation = "MULTIPLY"
            dirt_scale.inputs[1].default_value = 0.1
            links.new(channels.outputs["Blue"], dirt_scale.inputs[0])
            dirt_bias = nodes.new("ShaderNodeMath")
            dirt_bias.name = "BC2 AH60 main dirt bias"
            dirt_bias.label = "Dirt term + 0.045"
            dirt_bias.operation = "ADD"
            dirt_bias.inputs[1].default_value = 0.045
            links.new(dirt_scale.outputs[0], dirt_bias.inputs[0])
            dirt_add = nodes.new("ShaderNodeMixRGB")
            dirt_add.name = "BC2 AH60 main dirt"
            dirt_add.label = "Add source dirt term"
            dirt_add.blend_type = "ADD"
            dirt_add.inputs["Fac"].default_value = 1.0
            links.new(color_output, dirt_add.inputs["Color1"])
            links.new(dirt_bias.outputs[0], dirt_add.inputs["Color2"])
            color_output = dirt_add.outputs["Color"]

        detail = _binding_texture_node(texture_nodes, 4)
        if detail is not None:
            detail_double = nodes.new("ShaderNodeMixRGB")
            detail_double.name = "BC2 AH60 main detail double"
            detail_double.label = "Ah60Blackhawk_D x 2"
            detail_double.blend_type = "MULTIPLY"
            detail_double.inputs["Fac"].default_value = 1.0
            detail_double.inputs["Color2"].default_value = (
                2.0,
                2.0,
                2.0,
                1.0,
            )
            links.new(
                detail.outputs["Color"],
                detail_double.inputs["Color1"],
            )
            detail_mix = nodes.new("ShaderNodeMixRGB")
            detail_mix.name = "BC2 AH60 main detail modulation"
            detail_mix.label = "Main x lerp(1, detail x 2, 0.482)"
            detail_mix.blend_type = "MULTIPLY"
            detail_mix.inputs["Fac"].default_value = 0.482
            links.new(color_output, detail_mix.inputs["Color1"])
            links.new(
                detail_double.outputs["Color"],
                detail_mix.inputs["Color2"],
            )
            color_output = detail_mix.outputs["Color"]

        links.new(color_output, shader.inputs["Base Color"])
        material["bc2_material_family"] = "AH60 main"
        material["bc2_color_model"] = (
            "Ah60Blackhawk_C blended to Symbols_Us by alpha, "
            "DirtMaskRGB blue x0.1 + 0.045, then Ah60Blackhawk_D "
            "detail modulation x0.482"
        )
        material["bc2_lighting_mask_model"] = (
            "Msk_Generic retained at source UV x15 for the outdoor-light "
            "term, which remains Blender-native"
        )
        material["bc2_material_evidence"] = (
            "MP_004 vertex shaders 428/554/748/1310 and "
            "pixel shaders 1229/1792"
        )
        material["bc2_material_status"] = (
            "resolved verified AH60 main graph"
        )
        return True

    if family == "AH60_GLASS":
        material.diffuse_color = (0.06, 0.08, 0.10, 0.2)
        shader.inputs["Base Color"].default_value = (
            0.06,
            0.08,
            0.10,
            1.0,
        )
        shader.inputs["Roughness"].default_value = 0.12
        shader.inputs["IOR"].default_value = 1.45
        transmission = shader.inputs.get("Transmission Weight")
        if transmission is not None:
            transmission.default_value = 1.0

        fresnel = nodes.new("ShaderNodeFresnel")
        fresnel.name = "BC2 glass Fresnel"
        fresnel.label = "Source view-angle opacity"
        fresnel.inputs["IOR"].default_value = 1.45
        fresnel.location = (100, -260)
        opacity = fresnel.outputs["Fac"]
        dirt_node = _binding_texture_node(texture_nodes, 2)
        if dirt_node is None:
            dirt_node = _binding_texture_node(texture_nodes, 0)
        if dirt_node is not None:
            channels = nodes.new("ShaderNodeSeparateColor")
            channels.name = "BC2 glass dirt channels"
            channels.label = "DirtMaskRGB red"
            channels.location = (-180, -300)
            links.new(dirt_node.outputs["Color"], channels.inputs["Color"])
            add = nodes.new("ShaderNodeMath")
            add.name = "BC2 glass opacity"
            add.label = "Fresnel + dirt red"
            add.operation = "ADD"
            add.use_clamp = True
            add.location = (320, -240)
            links.new(fresnel.outputs["Fac"], add.inputs[0])
            links.new(channels.outputs["Red"], add.inputs[1])
            opacity = add.outputs[0]
        links.new(opacity, shader.inputs["Alpha"])
        material.surface_render_method = "DITHERED"
        material["bc2_material_family"] = "AH60 glass"
        material["bc2_color_model"] = (
            "transparent reflective glass with Fresnel plus "
            "DirtMaskRGB red opacity"
        )
        material["bc2_material_evidence"] = (
            "MP_004 pixel shaders 978, 1135, and 2110"
        )
        material["bc2_material_status"] = (
            "resolved verified AH60 glass graph"
        )
        return True

    if family in {"AH60_INTERIOR", "AH60_COCKPIT_INTERIOR"}:
        color_node = _binding_texture_node(texture_nodes, 1)
        if color_node is None:
            return False
        factor = 0.2 if family == "AH60_INTERIOR" else 0.05
        multiply = nodes.new("ShaderNodeMixRGB")
        multiply.name = "BC2 interior albedo scale"
        multiply.label = f"Source diffuse scale {factor:g}"
        multiply.blend_type = "MULTIPLY"
        multiply.inputs["Fac"].default_value = 1.0
        multiply.inputs["Color2"].default_value = (
            factor,
            factor,
            factor,
            1.0,
        )
        multiply.location = (300, 120)
        links.new(color_node.outputs["Color"], multiply.inputs["Color1"])
        links.new(multiply.outputs["Color"], shader.inputs["Base Color"])
        material.diffuse_color = (
            factor * 0.55,
            factor * 0.55,
            factor * 0.55,
            1.0,
        )
        material["bc2_material_family"] = (
            "AH60 interior"
            if family == "AH60_INTERIOR"
            else "AH60 cockpit interior"
        )
        material["bc2_color_model"] = (
            f"Interior_D color scaled by verified diffuse factor {factor:g}"
        )
        material["bc2_material_evidence"] = (
            "MP_004 Interior pixel shaders 174/910 and "
            "CockpitInterior pixel shaders 1538/1589"
        )
        material["bc2_material_status"] = (
            "resolved verified AH60 interior graph"
        )
        return True

    if family == "MINIGUN_MAIN":
        symbols = _binding_texture_node(texture_nodes, 2)
        if symbols is None:
            return False
        symbols_mix = nodes.new("ShaderNodeMixRGB")
        symbols_mix.name = "BC2 minigun symbols"
        symbols_mix.label = "0.072 gray to Symbols_Us by alpha"
        symbols_mix.blend_type = "MIX"
        symbols_mix.inputs["Color1"].default_value = (
            0.072,
            0.072,
            0.072,
            1.0,
        )
        symbols_mix.location = (20, 160)
        links.new(symbols.outputs["Alpha"], symbols_mix.inputs["Fac"])
        links.new(symbols.outputs["Color"], symbols_mix.inputs["Color2"])
        color_output = symbols_mix.outputs["Color"]

        generic_mask = _binding_texture_node(texture_nodes, 3)
        if generic_mask is not None:
            mask_scale = nodes.new("ShaderNodeMixRGB")
            mask_scale.name = "BC2 minigun generic mask scale"
            mask_scale.label = "Msk_Generic x 0.006"
            mask_scale.blend_type = "MULTIPLY"
            mask_scale.inputs["Fac"].default_value = 1.0
            mask_scale.inputs["Color2"].default_value = (
                0.006,
                0.006,
                0.006,
                1.0,
            )
            mask_scale.location = (20, -40)
            links.new(
                generic_mask.outputs["Color"],
                mask_scale.inputs["Color1"],
            )
            add = nodes.new("ShaderNodeMixRGB")
            add.name = "BC2 minigun base color"
            add.label = "Symbols color + generic mask"
            add.blend_type = "ADD"
            add.use_clamp = True
            add.inputs["Fac"].default_value = 1.0
            add.location = (300, 120)
            links.new(color_output, add.inputs["Color1"])
            links.new(mask_scale.outputs["Color"], add.inputs["Color2"])
            color_output = add.outputs["Color"]
        links.new(color_output, shader.inputs["Base Color"])

        specular_mask = _binding_texture_node(texture_nodes, 4)
        specular_input = shader.inputs.get("Specular IOR Level")
        if specular_mask is not None and specular_input is not None:
            channels = nodes.new("ShaderNodeSeparateColor")
            channels.name = "BC2 minigun specular channels"
            channels.label = "MiniGun_C green specular mask"
            channels.location = (280, -180)
            links.new(
                specular_mask.outputs["Color"],
                channels.inputs["Color"],
            )
            links.new(channels.outputs["Green"], specular_input)
        shader.inputs["Roughness"].default_value = 0.3
        material.diffuse_color = (0.072, 0.072, 0.072, 1.0)
        material["bc2_material_family"] = "Minigun main"
        material["bc2_color_model"] = (
            "Symbols_Us RGB/alpha over 0.072 gray plus "
            "Msk_Generic x0.006"
        )
        material["bc2_specular_model"] = (
            "MiniGun_C green channel with source exponent 20 approximation"
        )
        material["bc2_material_evidence"] = (
            "MP_004 pixel shaders 23 and 1869"
        )
        material["bc2_material_status"] = (
            "resolved verified minigun material graph"
        )
        return True
    return False


def _surface_uses_alpha(surface: SurfaceShader) -> bool:
    value = f"{surface.name}/{surface.database_key}".casefold()
    return any(
        keyword in value
        for keyword in (
            "foliage",
            "leaf",
            "leaves",
            "grass",
            "branch",
            "background_tree",
            "treeline",
            "barbwire",
        )
    )


def _find_shader_material(
    shader_database: ShaderDatabase,
    key: str,
):
    material = shader_database.material(key)
    if material is not None:
        return material, material.key, "exact"
    prefix, separator, name = key.rpartition("/")
    if separator and name.casefold().startswith("cheap_"):
        alias = prefix + "/" + name[len("cheap_") :]
        material = shader_database.material(alias)
        if material is not None:
            return material, material.key, "cheap_alias"
    return None, key, "missing"


def _surface_material(
    repository,
    surface: SurfaceShader,
    shader_database: ShaderDatabase | None,
    preferred_archives,
    material_cache: dict[str, object],
    image_cache: dict[str, tuple[object, object]],
    diagnostics: list[dict[str, str]],
    import_textures: bool,
):
    cache_key = "surface|" + surface.database_key.casefold()
    cached = material_cache.get(cache_key)
    if cached is not None:
        return cached

    material = bpy.data.materials.new(
        f"BC2 {PurePosixPath(surface.name).name}"
    )
    material.diffuse_color = (0.55, 0.55, 0.55, 1.0)
    nodes, links, shader = _material_nodes(material)
    shader.inputs["Base Color"].default_value = (0.55, 0.55, 0.55, 1.0)
    material["bc2_surface_name"] = surface.name
    material["bc2_shader_reference"] = surface.reference
    material["bc2_shader_key"] = surface.database_key
    material_cache[cache_key] = material

    if not import_textures:
        material["bc2_material_status"] = "texture resolution disabled"
        return material
    if shader_database is None:
        material["bc2_material_status"] = "shader database unavailable"
        diagnostics.append(
            {
                "code": "shader_database_unavailable",
                "reference": surface.database_key,
                "message": "Material was created without shader bindings",
            }
        )
        return material

    shader_material, resolved_shader_key, key_match = _find_shader_material(
        shader_database,
        surface.database_key,
    )
    if shader_material is None:
        material.diffuse_color = (0.8, 0.05, 0.8, 1.0)
        shader.inputs["Base Color"].default_value = (0.8, 0.05, 0.8, 1.0)
        material["bc2_material_status"] = "shader key not found"
        diagnostics.append(
            {
                "code": "missing_shader_key",
                "reference": surface.database_key,
                "message": "DX11 shader database has no matching material key",
            }
        )
        return material
    material["bc2_shader_key_match"] = key_match
    if resolved_shader_key != surface.database_key:
        material["bc2_resolved_shader_key"] = resolved_shader_key

    character_surface = _is_character_surface(surface)
    roles = (
        _character_binding_roles(surface, shader_material.textures)
        if character_surface
        else _surface_binding_roles(surface, shader_material.textures)
    )
    layered_static = not character_surface and any(
        role == "OCCLUSION"
        and any(
            marker in PurePosixPath(binding.path).name.casefold()
            for marker in ("ocrgb", "oc_rgb", "oc-rgb")
        )
        for binding, role in zip(shader_material.textures, roles)
    )
    material["bc2_texture_bindings"] = json.dumps(
        [
            {
                "index": binding.index,
                "path": binding.path,
                "parameters": list(binding.parameters),
                "role": role,
            }
            for binding, role in zip(shader_material.textures, roles)
        ],
        separators=(",", ":"),
    )
    texture_nodes = []
    normal_node = None
    normal_format = None
    normal_path = None
    uv_nodes = {}
    for position, (binding, role) in enumerate(
        zip(shader_material.textures, roles)
    ):
        asset = _texture_asset(
            repository,
            binding.path,
            preferred_archives,
        )
        if asset is None:
            diagnostics.append(
                {
                    "code": "missing_texture",
                    "reference": binding.path,
                    "message": (
                        f"Texture binding t{binding.index} was not found in "
                        "the indexed level archives"
                    ),
                }
            )
            continue
        image_key = str(asset["path"]).casefold()
        loaded = image_cache.get(image_key)
        if loaded is None:
            try:
                texture_asset, raw = repository.read_asset(
                    int(asset["id"]),
                    max_size=1 << 30,
                )
                loaded = _load_texture_image(
                    repository,
                    texture_asset,
                    raw,
                    non_color=(
                        role in _CHARACTER_NON_COLOR_ROLES
                        if character_surface
                        else role in {
                            "NORMAL",
                            "MASK",
                            "NOISE",
                            "OCCLUSION",
                            "ROUGHNESS",
                            "SPECULAR",
                        }
                    ),
                )
                image_cache[image_key] = loaded
            except (BC2Error, OSError, RuntimeError, ValueError) as exc:
                image_cache[image_key] = (None, None)
                diagnostics.append(
                    {
                        "code": "texture_import_failed",
                        "reference": binding.path,
                        "message": str(exc),
                    }
                )
                continue
        image, converted = loaded
        if image is None or converted is None:
            continue
        if (
            role in _CHARACTER_NON_COLOR_ROLES
            if character_surface
            else role in {
                "NORMAL",
                "MASK",
                "NOISE",
                "OCCLUSION",
                "ROUGHNESS",
                "SPECULAR",
            }
        ):
            image.colorspace_settings.name = "Non-Color"
        node = nodes.new("ShaderNodeTexImage")
        node.image = image
        node.name = f"t{binding.index} {PurePosixPath(binding.path).name}"
        node.label = f"t{binding.index} {role}"
        node.location = (-800, 400 - position * 220)
        node["bc2_binding_index"] = binding.index
        node["bc2_asset_path"] = binding.path
        node["bc2_parameters"] = json.dumps(
            binding.parameters,
            separators=(",", ":"),
        )
        uv_map = (
            _character_binding_uv_map(surface, role)
            if character_surface
            else _surface_binding_uv_map(
                surface,
                binding,
                role,
                layered_static=layered_static,
            )
        )
        uv_node = uv_nodes.get(uv_map)
        if uv_node is None:
            uv_node = nodes.new("ShaderNodeUVMap")
            uv_node.uv_map = uv_map
            uv_node.name = f"BC2 {uv_map}"
            uv_node.label = uv_map
            uv_node.location = (-1050, 400 - len(uv_nodes) * 180)
            uv_nodes[uv_map] = uv_node
        uv_scale = (
            _character_binding_uv_scale(surface, role)
            if character_surface
            else _surface_binding_uv_scale(surface, binding)
        )
        if uv_scale == (1.0, 1.0):
            links.new(uv_node.outputs["UV"], node.inputs["Vector"])
        else:
            scale_node = nodes.new("ShaderNodeVectorMath")
            scale_node.name = (
                f"BC2 {role} UV x{uv_scale[0]:g},x{uv_scale[1]:g}"
            )
            scale_node.label = (
                f"{role} UV x{uv_scale[0]:g}, x{uv_scale[1]:g}"
            )
            scale_node.operation = "MULTIPLY"
            scale_node.inputs[1].default_value = (
                uv_scale[0],
                uv_scale[1],
                1.0,
            )
            links.new(uv_node.outputs["UV"], scale_node.inputs[0])
            links.new(scale_node.outputs["Vector"], node.inputs["Vector"])
        node["bc2_uv_map"] = uv_map
        node["bc2_uv_scale"] = uv_scale
        texture_nodes.append((binding, role, node))
        if role == "NORMAL" and normal_node is None:
            normal_node = node
            normal_format = converted.format_name
            normal_path = binding.path

    if character_surface:
        _configure_character_principled(
            surface,
            nodes,
            links,
            shader,
            texture_nodes,
        )
        color_output, _, color_model = _character_color_output(
            nodes,
            links,
            texture_nodes,
        )
        material["bc2_color_model"] = color_model
        if color_output is not None:
            links.new(color_output, shader.inputs["Base Color"])
        normal_output, normal_model = _character_normal_output(
            nodes,
            links,
            texture_nodes,
        )
        material["bc2_normal_model"] = normal_model
        if normal_output is not None:
            links.new(normal_output, shader.inputs["Normal"])
        material["bc2_material_family"] = "PC skinned character"
        material["bc2_material_evidence"] = (
            "PC DX11 bindings and representative retail shader disassembly"
        )
        material["bc2_material_status"] = (
            "resolved character material graph"
            if color_output is not None
            else "resolved character bindings without base color"
        )
        return material

    if _configure_verified_surface_material(
        material,
        surface,
        nodes,
        links,
        shader,
        texture_nodes,
    ):
        return material

    color_output, color_node, color_model = _layered_color_output(
        nodes,
        links,
        texture_nodes,
    )
    material["bc2_color_model"] = color_model
    if color_output is not None:
        links.new(color_output, shader.inputs["Base Color"])
        if _surface_uses_alpha(surface):
            links.new(color_node.outputs["Alpha"], shader.inputs["Alpha"])
            material.surface_render_method = "DITHERED"
            material["bc2_alpha_mode"] = (
                "source color alpha for reflected foliage/cutout family"
            )

    if normal_node is not None:
        normalized_path = normal_path.casefold().removesuffix(".res")
        if normalized_path == "textures/_general/nm_general_001":
            normal_output = _dxt5nm_output(
                nodes,
                links,
                normal_node,
                uv_scale=8.0,
                detail_weight=0.4,
            )
            material["bc2_normal_model"] = (
                "verified DXT5nm alpha/green, UV x8, base + detail x0.4"
            )
        elif normal_format == "DXT5":
            normal_output = _dxt5nm_output(
                nodes,
                links,
                normal_node,
            )
            material["bc2_normal_model"] = (
                "heuristic DXT5nm alpha/green, unit UV scale"
            )
        else:
            normal_output = _standard_normal_output(
                nodes,
                links,
                normal_node,
            )
            material["bc2_normal_model"] = "heuristic RGB tangent normal"
        links.new(normal_output, shader.inputs["Normal"])

    material["bc2_material_status"] = (
        "resolved material graph"
        if color_output is not None
        else "resolved bindings without color graph"
    )
    return material


def _source_material_context(
    repository,
    preferred_archives,
    settings: BC2Settings,
) -> _SourceMaterialContext:
    import_textures = getattr(settings, "import_textures", True)
    material_archives = tuple(preferred_archives)
    shader_database = None
    shader_database_path = None
    diagnostics = []
    if import_textures and repository.kind == "PC_ISO":
        shader_archive = index_pc_character_shader_archive(repository)
        material_archives = tuple(
            dict.fromkeys((*preferred_archives, shader_archive))
        )
        _, shader_raw = repository.read_asset_path(
            PC_CHARACTER_SHADER_PATH,
            max_size=1 << 30,
            preferred_archives=material_archives,
        )
        shader_database = parse_shader_database(shader_raw)
        shader_database_path = PC_CHARACTER_SHADER_PATH
        if shader_database.trailing_bytes:
            diagnostics.append(
                {
                    "code": "shader_database_trailing_bytes",
                    "reference": PC_CHARACTER_SHADER_PATH,
                    "message": (
                        f"{shader_database.trailing_bytes} unparsed byte(s) "
                        "remain after the shader database"
                    ),
                }
            )
    return _SourceMaterialContext(
        import_textures=import_textures,
        material_archives=material_archives,
        shader_database=shader_database,
        shader_database_path=shader_database_path,
        material_cache={},
        image_cache={},
        diagnostics=diagnostics,
        loaded_shader_paths=(
            {PC_CHARACTER_SHADER_PATH.casefold()}
            if shader_database_path
            else set()
        ),
    )


def _merge_shader_databases(
    first: ShaderDatabase | None,
    second: ShaderDatabase,
) -> ShaderDatabase:
    if first is None:
        return second
    materials = list(first.materials)
    existing = {material.key.casefold() for material in materials}
    materials.extend(
        material
        for material in second.materials
        if material.key.casefold() not in existing
    )
    return ShaderDatabase(
        tuple(materials),
        first.trailing_bytes + second.trailing_bytes,
    )


def _extend_source_shader_database(
    repository,
    material_context: _SourceMaterialContext,
    archive_path: str,
    shader_path: str,
) -> bool:
    key = shader_path.casefold()
    if key in material_context.loaded_shader_paths:
        return False
    material_context.loaded_shader_paths.add(key)
    if repository.catalog.archive_by_path(archive_path) is None:
        return False
    repository.index_archive_path(archive_path)
    shader_asset = repository.catalog.find_asset(
        shader_path,
        (archive_path,),
    )
    if shader_asset is None:
        return False
    _, shader_raw = repository.read_asset(
        int(shader_asset["id"]),
        max_size=1 << 30,
    )
    database = parse_shader_database(shader_raw)
    material_context.shader_database = _merge_shader_databases(
        material_context.shader_database,
        database,
    )
    material_context.material_archives = tuple(
        dict.fromkeys(
            (*material_context.material_archives, archive_path)
        )
    )
    material_context.shader_database_path = ";".join(
        (
            *(
                path
                for path in (
                    material_context.shader_database_path or ""
                ).split(";")
                if path
            ),
            shader_path,
        )
    )
    if database.trailing_bytes:
        material_context.diagnostics.append(
            {
                "code": "shader_database_trailing_bytes",
                "reference": shader_path,
                "message": (
                    f"{database.trailing_bytes} unparsed byte(s) remain "
                    "after the shader database"
                ),
            }
        )
    return True


def _ensure_source_shader_materials(
    repository,
    material_context: _SourceMaterialContext,
    shader_keys: tuple[str, ...],
):
    if (
        repository.kind != "PC_ISO"
        or not material_context.import_textures
        or not shader_keys
    ):
        return

    def unresolved():
        database = material_context.shader_database
        return (
            database is None
            or any(
                _find_shader_material(database, key)[0] is None
                for key in shader_keys
            )
        )

    if not unresolved():
        return
    indexed = repository.catalog.search_assets(
        query="Dx11_Single.res",
        limit=500,
    )
    candidates = [
        (str(asset["archive_path"]), str(asset["path"]))
        for asset in indexed
    ]
    candidates.extend(_PC_MODULAR_SHADER_FALLBACKS)
    seen = set()
    for archive_path, shader_path in candidates:
        key = shader_path.casefold()
        if key in seen:
            continue
        seen.add(key)
        _extend_source_shader_database(
            repository,
            material_context,
            archive_path,
            shader_path,
        )
        if not unresolved():
            return


def _resolve_mesh_subset_materials(
    repository,
    mesh_set_asset,
    parsed: MeshAsset,
    preferred_archives,
    settings: BC2Settings,
    *,
    shader_set_override: str | None = None,
    material_context: _SourceMaterialContext | None = None,
):
    created_context = material_context is None
    material_context = material_context or _source_material_context(
        repository,
        preferred_archives,
        settings,
    )
    diagnostic_start = 0 if created_context else len(
        material_context.diagnostics
    )
    descriptor_path = _resource_partition(mesh_set_asset["path"]) + ".dbx"
    descriptor_asset = repository.catalog.find_asset(
        descriptor_path,
        preferred_archives,
    )
    if descriptor_asset is None:
        raise BC2Error(
            f"Mesh descriptor is not indexed: {descriptor_path}"
        )
    _, descriptor_raw = repository.read_asset(
        int(descriptor_asset["id"]),
        max_size=256 << 20,
    )
    descriptor = parse_dbx(descriptor_raw)
    mesh_instance = descriptor.primary_instance
    mesh_guid = (
        mesh_instance.attribute("guid")
        if mesh_instance is not None
        else None
    )
    if not mesh_guid:
        raise BC2Error(
            f"Descriptor has no primary mesh instance: "
            f"{descriptor_path}"
        )
    mesh_reference = (
        descriptor_path.removesuffix(".dbx") + "/" + mesh_guid
    )
    resolver = LevelResolver(
        repository,
        "MODULAR",
        tuple(preferred_archives),
    )
    definition = resolver.resolve_mesh(
        mesh_reference,
        shader_set_override,
    )
    _ensure_source_shader_materials(
        repository,
        material_context,
        tuple(surface.database_key for surface in definition.surfaces),
    )
    material_context.diagnostics.extend(
        {
            "code": issue.code,
            "message": issue.message,
            "reference": issue.reference or "",
        }
        for issue in resolver.issues
    )

    assignments = _surface_assignments(parsed, definition)
    materials = []
    for subset, (surface, match) in zip(parsed.subsets, assignments):
        if surface is None:
            material_context.diagnostics.append(
                {
                    "code": "unresolved_mesh_surface",
                    "reference": subset.name,
                    "message": (
                        "Mesh subset could not be matched to its "
                        "resolved MeshShaderSet"
                    ),
                }
            )
            material = _diagnostic_material(
                material_context.material_cache,
                subset.name,
                f"BC2 Missing {PurePosixPath(subset.name).name}",
                "Mesh subset has no resolved source surface",
            )
            material["bc2_material_status"] = "unresolved mesh surface"
        else:
            material = _surface_material(
                repository,
                surface,
                material_context.shader_database,
                material_context.material_archives,
                material_context.material_cache,
                material_context.image_cache,
                material_context.diagnostics,
                material_context.import_textures,
            )
            material["bc2_subset_match"] = match
        materials.append(material)

    resolved_materials = sum(
        str(material.get("bc2_material_status", "")).startswith(
            "resolved "
        )
        for material in materials
    )
    summary = {
        "mesh_reference": mesh_reference,
        "shader_set_reference": definition.shader_set_reference or "",
        "shader_database": material_context.shader_database_path or "",
        "subsets": len(parsed.subsets),
        "source_surfaces": len(definition.surfaces),
        "resolved_materials": resolved_materials,
        "source_images": sum(
            image is not None
            for image, _ in material_context.image_cache.values()
        ),
        "diagnostics": (
            len(material_context.diagnostics) - diagnostic_start
        ),
    }
    return (
        tuple(materials),
        tuple(material_context.diagnostics[diagnostic_start:]),
        summary,
    )


_TERRAIN_LAYER_PATTERNS = {
    "defaultmaterial": ("jungleground", "grasscloseup", "burnedground"),
    "grass": ("grasscloseup", "grass"),
    "dirt": ("soil", "ground_mudstones", "flatdirt", "dirt"),
    "tarmac": ("asphalt", "tarmac", "pavement"),
    "gravel": ("gravel",),
    "stone": ("cliffface", "stone", "rock", "rubble"),
    "seabed": ("rubblesand", "seabed", "mudstones"),
}

_TERRAIN_LAYER_COLORS = {
    "defaultmaterial": (0.18, 0.28, 0.12, 1.0),
    "grass": (0.16, 0.32, 0.12, 1.0),
    "dirt": (0.24, 0.16, 0.09, 1.0),
    "tarmac": (0.10, 0.11, 0.11, 1.0),
    "gravel": (0.34, 0.31, 0.26, 1.0),
    "stone": (0.29, 0.30, 0.27, 1.0),
    "seabed": (0.25, 0.23, 0.16, 1.0),
}


def _terrain_shader_texture_paths(
    shader_database: ShaderDatabase | None,
    level_name: str,
) -> dict[str, int]:
    if shader_database is None:
        return {}
    level = level_name.casefold()
    prefix = f"terrains/{level}/{level}/"
    counts = {}
    for material in shader_database.materials:
        if not material.key.casefold().startswith(prefix):
            continue
        for binding in material.textures:
            role = _texture_role(binding.path, binding.parameters)
            if role in {
                "NORMAL",
                "MASK",
                "NOISE",
                "ROUGHNESS",
                "SPECULAR",
            }:
                continue
            path = binding.path
            counts[path] = counts.get(path, 0) + 1
    return counts


def _terrain_layer_texture(
    name: str,
    candidates: dict[str, int],
) -> str | None:
    patterns = _TERRAIN_LAYER_PATTERNS.get(
        name.casefold(),
        _TERRAIN_LAYER_PATTERNS["defaultmaterial"],
    )
    for pattern in patterns:
        matches = [
            (count, path)
            for path, count in candidates.items()
            if pattern in PurePosixPath(path).name.casefold()
        ]
        if matches:
            return max(matches, key=lambda item: (item[0], item[1]))[1]
    return None


def _terrain_material(
    repository,
    level_scene,
    material_map_asset,
    material_map_raw: bytes,
    shader_database: ShaderDatabase | None,
    preferred_archives,
    image_cache: dict[str, tuple[object, object]],
    diagnostics: list[dict[str, str]],
    terrain_bounds: tuple[float, float, float, float],
):
    material_map = parse_material_map(
        material_map_raw,
        platform=material_map_asset["platform"],
    )
    material = bpy.data.materials.new(
        f"BC2 {level_scene.name} Terrain"
    )
    material.diffuse_color = (0.22, 0.32, 0.16, 1.0)
    nodes, links, shader = _material_nodes(material)
    shader.inputs["Roughness"].default_value = 0.9

    image_key = (
        "terrain-material-map|"
        + str(material_map_asset["path"]).casefold()
    )
    loaded_map = image_cache.get(image_key)
    if loaded_map is None:
        map_image = _load_terrain_material_map_image(
            repository,
            material_map_asset,
            material_map,
        )
        loaded_map = (map_image, material_map)
        image_cache[image_key] = loaded_map
    map_image = loaded_map[0]

    uv_node = nodes.new("ShaderNodeUVMap")
    uv_node.uv_map = "UVMap"
    uv_node.name = "BC2 terrain world UV"
    uv_node.label = "Terrain world UV"
    uv_node.location = (-1250, 300)

    map_node = nodes.new("ShaderNodeTexImage")
    map_node.image = map_image
    map_node.name = "BC2 terrain material indices"
    map_node.label = "Terrain material indices"
    map_node.interpolation = "Closest"
    map_node.extension = "CLIP"
    map_node.location = (-1020, 520)
    links.new(uv_node.outputs["UV"], map_node.inputs["Vector"])

    separate = nodes.new("ShaderNodeSeparateColor")
    separate.location = (-790, 520)
    links.new(map_node.outputs["Color"], separate.inputs["Color"])
    index_scale = nodes.new("ShaderNodeMath")
    index_scale.operation = "MULTIPLY"
    index_scale.inputs[1].default_value = 255.0
    index_scale.location = (-600, 520)
    links.new(separate.outputs["Red"], index_scale.inputs[0])

    min_x, min_z, max_x, max_z = terrain_bounds
    detail_scale = nodes.new("ShaderNodeVectorMath")
    detail_scale.operation = "MULTIPLY"
    detail_scale.inputs[1].default_value = (
        max(1.0, (max_x - min_x) / 16.0),
        max(1.0, (max_z - min_z) / 16.0),
        1.0,
    )
    detail_scale.location = (-1020, 260)
    links.new(uv_node.outputs["UV"], detail_scale.inputs[0])

    candidates = _terrain_shader_texture_paths(
        shader_database,
        level_scene.name,
    )
    layer_records = []
    color_output = None
    for index, name in enumerate(material_map.names):
        path = _terrain_layer_texture(name, candidates)
        layer_output = None
        if path is not None:
            asset = _texture_asset(
                repository,
                path,
                preferred_archives,
            )
            if asset is not None:
                image_asset_key = str(asset["path"]).casefold()
                loaded = image_cache.get(image_asset_key)
                if loaded is None:
                    try:
                        texture_asset, raw = repository.read_asset(
                            int(asset["id"]),
                            max_size=1 << 30,
                        )
                        loaded = _load_texture_image(
                            repository,
                            texture_asset,
                            raw,
                        )
                        image_cache[image_asset_key] = loaded
                    except (
                        BC2Error,
                        OSError,
                        RuntimeError,
                        ValueError,
                    ) as exc:
                        image_cache[image_asset_key] = (None, None)
                        diagnostics.append(
                            {
                                "code": "terrain_texture_import_failed",
                                "reference": path,
                                "message": str(exc),
                            }
                        )
                        loaded = (None, None)
                image = loaded[0]
                if image is not None:
                    texture_node = nodes.new("ShaderNodeTexImage")
                    texture_node.image = image
                    texture_node.name = f"Terrain {index} {name}"
                    texture_node.label = f"{index}: {name}"
                    texture_node.extension = "REPEAT"
                    texture_node.location = (
                        -360,
                        300 - index * 180,
                    )
                    links.new(
                        detail_scale.outputs["Vector"],
                        texture_node.inputs["Vector"],
                    )
                    layer_output = texture_node.outputs["Color"]
            else:
                diagnostics.append(
                    {
                        "code": "missing_terrain_texture",
                        "reference": path,
                        "message": (
                            f"Terrain layer {name!r} texture was not found"
                        ),
                    }
                )
        if layer_output is None:
            fallback = nodes.new("ShaderNodeRGB")
            fallback.outputs["Color"].default_value = (
                _TERRAIN_LAYER_COLORS.get(
                    name.casefold(),
                    _TERRAIN_LAYER_COLORS["defaultmaterial"],
                )
            )
            fallback.name = f"Terrain {index} {name} fallback"
            fallback.label = f"{index}: {name} fallback"
            fallback.location = (-360, 300 - index * 180)
            layer_output = fallback.outputs["Color"]

        layer_records.append(
            {
                "index": index,
                "name": name,
                "texture": path or "",
            }
        )
        if color_output is None:
            color_output = layer_output
            continue
        compare = nodes.new("ShaderNodeMath")
        compare.operation = "COMPARE"
        compare.inputs[1].default_value = float(index)
        compare.inputs[2].default_value = 0.25
        compare.location = (20, 420 - index * 130)
        links.new(index_scale.outputs[0], compare.inputs[0])
        mix = nodes.new("ShaderNodeMixRGB")
        mix.blend_type = "MIX"
        mix.name = f"BC2 terrain layer {index} {name}"
        mix.label = f"{index}: {name}"
        mix.location = (230, 300 - index * 100)
        links.new(compare.outputs[0], mix.inputs["Fac"])
        links.new(color_output, mix.inputs["Color1"])
        links.new(layer_output, mix.inputs["Color2"])
        color_output = mix.outputs["Color"]

    if color_output is None:
        raise BC2Error("TerrainMaterialMap contains no material names")
    links.new(color_output, shader.inputs["Base Color"])
    material["bc2_material_status"] = "resolved terrain material map"
    material["bc2_terrain_material_map"] = material_map_asset["path"]
    material["bc2_terrain_material_side"] = material_map.side
    material["bc2_terrain_material_endian"] = material_map.endian
    material["bc2_terrain_layers"] = json.dumps(
        layer_records,
        separators=(",", ":"),
    )
    return material, material_map


def _child_collection(parent, name: str):
    collection = bpy.data.collections.new(name)
    parent.children.link(collection)
    return collection


def _simple_material(
    name: str,
    color: tuple[float, float, float, float],
    *,
    roughness: float,
    transmission: float = 0.0,
):
    material = bpy.data.materials.new(name)
    material.diffuse_color = color
    _, _, shader = _material_nodes(material)
    shader.inputs["Base Color"].default_value = color
    shader.inputs["Roughness"].default_value = roughness
    shader.inputs["Alpha"].default_value = color[3]
    shader.inputs["Transmission Weight"].default_value = transmission
    if color[3] < 1.0:
        material.surface_render_method = "DITHERED"
    return material


def _placement_object(
    collection,
    placement,
    mesh,
    settings: BC2Settings,
):
    suffix = (
        f"{placement.enumeration:04d}"
        if placement.enumeration is not None
        else placement.guid[-8:]
    )
    base_name = (placement.name or "Object").replace("/", "_")
    obj = bpy.data.objects.new(f"{base_name}_{suffix}", mesh)
    collection.objects.link(obj)
    obj.matrix_world = Matrix(
        placement.transform.blender_matrix(
            axis_conversion=settings.axis_conversion,
            unit_scale=settings.unit_scale,
        )
    )
    obj["bc2_guid"] = placement.guid
    obj["bc2_source"] = placement.source
    obj["bc2_entity_type"] = placement.entity_type
    obj["bc2_reference"] = placement.reference
    obj["bc2_mesh_key"] = placement.mesh_key
    obj["bc2_transform"] = list(placement.transform.values)
    if placement.enumeration is not None:
        obj["bc2_enumeration"] = placement.enumeration
    return obj


def _deduplicate_diagnostics(diagnostics):
    unique = []
    seen = set()
    for item in diagnostics:
        identity = (
            item.get("code", ""),
            item.get("reference", ""),
            item.get("message", ""),
        )
        if identity not in seen:
            unique.append(item)
            seen.add(identity)
    return unique


def _create_level_scene(
    context,
    repository,
    level_scene,
    preferred_archives,
    settings: BC2Settings,
    progress=None,
):
    root = bpy.data.collections.new(f"BC2_{level_scene.name}")
    context.scene.collection.children.link(root)
    collections = {
        "reference": _child_collection(root, "Direct"),
        "GameSharedResources.ForestEntityData": _child_collection(
            root, "Forest"
        ),
        "GameSharedResources.MiddlegrowthModelGroupEntityData": (
            _child_collection(root, "Middlegrowth")
        ),
        "GameSharedResources.RigidModelGroupEntityData": _child_collection(
            root, "Rigid Group"
        ),
        "GameSharedResources.StaticModelGroupEntityData": _child_collection(
            root, "Static Group"
        ),
        "terrain": _child_collection(root, "Terrain"),
        "water": _child_collection(root, "Water"),
        "diagnostics": _child_collection(root, "Diagnostics"),
    }
    diagnostics = [
        {
            "code": issue.code,
            "message": issue.message,
            "reference": issue.reference or "",
        }
        for issue in level_scene.issues
    ]

    shader_database = None
    if settings.map_import_materials and level_scene.shader_database_path:
        if progress:
            progress(0.01, "Reading level shader database")
        _, raw = repository.read_asset_path(
            level_scene.shader_database_path,
            max_size=1 << 30,
            preferred_archives=preferred_archives,
        )
        shader_database = parse_shader_database(raw)
        if shader_database.trailing_bytes:
            diagnostics.append(
                {
                    "code": "shader_database_trailing_bytes",
                    "reference": level_scene.shader_database_path,
                    "message": (
                        f"{shader_database.trailing_bytes} unparsed byte(s) "
                        "remain after the shader database"
                    ),
                }
            )

    material_cache = {}
    image_cache = {}
    geometry_cache = {}
    mesh_by_key = {}
    mesh_count = max(1, len(level_scene.meshes))
    for index, definition in enumerate(level_scene.meshes):
        if progress:
            progress(
                0.05 + 0.35 * (index / mesh_count),
                f"Creating mesh {index + 1:,}/{len(level_scene.meshes):,}",
            )
        geometry_key = definition.mesh_data_path.casefold()
        cached_geometry = geometry_cache.get(geometry_key)
        if cached_geometry is None:
            asset, raw = repository.read_asset_path(
                definition.mesh_data_path,
                max_size=1 << 30,
                preferred_archives=preferred_archives,
            )
            parsed = parse_meshdata(
                raw,
                asset["platform"],
                include_z_only=settings.include_z_only,
            )
            mesh = _combined_level_mesh(
                parsed,
                f"BC2_{index:03d}_{PurePosixPath(definition.name).name}",
                settings,
            )
            geometry_cache[geometry_key] = (parsed, mesh)
        else:
            parsed, source_mesh = cached_geometry
            mesh = source_mesh.copy()
            mesh.name = (
                f"BC2_{index:03d}_{PurePosixPath(definition.name).name}"
            )
            mesh.materials.clear()
            if "bc2_shader_set_reference" in mesh:
                del mesh["bc2_shader_set_reference"]

        assignments = _surface_assignments(parsed, definition)
        assignment_records = []
        for subset_index, (subset, assignment) in enumerate(
            zip(parsed.subsets, assignments)
        ):
            surface, match_method = assignment
            if surface is None:
                message = (
                    f"No shader surface matched subset {subset.name!r} in "
                    f"{definition.name}"
                )
                material = _diagnostic_material(
                    material_cache,
                    f"{definition.key}|{subset_index}",
                    f"BC2 Unresolved {subset.name}",
                    message,
                )
                diagnostics.append(
                    {
                        "code": "unresolved_subset_surface",
                        "reference": definition.reference,
                        "message": message,
                    }
                )
                shader_key = ""
            else:
                material = _surface_material(
                    repository,
                    surface,
                    shader_database,
                    preferred_archives,
                    material_cache,
                    image_cache,
                    diagnostics,
                    settings.map_import_materials,
                )
                shader_key = surface.database_key
            mesh.materials.append(material)
            assignment_records.append(
                {
                    "subset": subset.name,
                    "surface": surface.name if surface is not None else "",
                    "shader_key": shader_key,
                    "match": match_method,
                }
            )
        mesh["bc2_mesh_reference"] = definition.reference
        mesh["bc2_mesh_data_path"] = definition.mesh_data_path
        mesh["bc2_surface_assignments"] = json.dumps(
            assignment_records,
            separators=(",", ":"),
        )
        if definition.shader_set_reference:
            mesh["bc2_shader_set_reference"] = (
                definition.shader_set_reference
            )
        mesh_by_key[definition.key] = mesh

    source_collections = {
        "reference": collections["reference"],
        "GameSharedResources.ForestEntityData": collections[
            "GameSharedResources.ForestEntityData"
        ],
        "GameSharedResources.MiddlegrowthModelGroupEntityData": collections[
            "GameSharedResources.MiddlegrowthModelGroupEntityData"
        ],
        "GameSharedResources.RigidModelGroupEntityData": collections[
            "GameSharedResources.RigidModelGroupEntityData"
        ],
        "GameSharedResources.StaticModelGroupEntityData": collections[
            "GameSharedResources.StaticModelGroupEntityData"
        ],
    }
    placement_count = max(1, len(level_scene.placements))
    created_objects = []
    for index, placement in enumerate(level_scene.placements):
        if progress and (index % 100 == 0 or index + 1 == placement_count):
            progress(
                0.4 + 0.4 * (index / placement_count),
                (
                    f"Placing object {index + 1:,}/"
                    f"{len(level_scene.placements):,}"
                ),
            )
        collection = source_collections.get(placement.source)
        if collection is None:
            raise BC2Error(
                f"No Blender collection is mapped for {placement.source}"
            )
        mesh = mesh_by_key.get(placement.mesh_key)
        if mesh is None:
            raise BC2Error(
                f"Placement references an unresolved mesh: {placement.mesh_key}"
            )
        created_objects.append(
            _placement_object(
                collection,
                placement,
                mesh,
                settings,
            )
        )

    terrain_header_bounds = []
    for path in level_scene.heightfield_paths:
        asset, raw = repository.read_asset_path(
            path,
            max_size=1 << 30,
            preferred_archives=preferred_archives,
        )
        header = parse_heightfield(
            raw,
            2,
            platform=asset["platform"],
        )
        terrain_header_bounds.append(header.bounds)
    terrain_bounds = (
        (
            min(bounds[0] for bounds in terrain_header_bounds),
            min(bounds[2] for bounds in terrain_header_bounds),
            max(bounds[3] for bounds in terrain_header_bounds),
            max(bounds[5] for bounds in terrain_header_bounds),
        )
        if terrain_header_bounds
        else (-1.0, -1.0, 1.0, 1.0)
    )

    terrain_material = None
    terrain_material_map = None
    if (
        settings.map_import_materials
        and level_scene.terrain_material_map_path
    ):
        material_map_asset, material_map_raw = repository.read_asset_path(
            level_scene.terrain_material_map_path,
            max_size=1 << 30,
            preferred_archives=preferred_archives,
        )
        terrain_material, terrain_material_map = _terrain_material(
            repository,
            level_scene,
            material_map_asset,
            material_map_raw,
            shader_database,
            preferred_archives,
            image_cache,
            diagnostics,
            terrain_bounds,
        )
        root["bc2_terrain_material_map_path"] = (
            level_scene.terrain_material_map_path
        )
        root["bc2_terrain_material_map_side"] = terrain_material_map.side
        root["bc2_terrain_material_names"] = json.dumps(
            terrain_material_map.names,
            separators=(",", ":"),
        )
    if terrain_material is None:
        terrain_material = _simple_material(
            "BC2 Terrain Material Unavailable",
            (0.22, 0.32, 0.16, 1.0),
            roughness=0.9,
        )
        terrain_material["bc2_material_status"] = (
            "terrain material resolution disabled"
            if not settings.map_import_materials
            else "terrain material map unavailable"
        )
    terrain_objects = []
    terrain_count = max(1, len(level_scene.heightfield_paths))
    for index, path in enumerate(level_scene.heightfield_paths):
        if progress:
            progress(
                0.8 + 0.12 * (index / terrain_count),
                (
                    f"Creating terrain tile {index + 1:,}/"
                    f"{len(level_scene.heightfield_paths):,}"
                ),
            )
        asset, raw = repository.read_asset_path(
            path,
            max_size=1 << 30,
            preferred_archives=preferred_archives,
        )
        parsed = parse_heightfield(
            raw,
            settings.terrain_resolution,
            platform=asset["platform"],
        )
        obj = _create_terrain_object(
            context,
            parsed,
            asset,
            settings,
            collection=collections["terrain"],
            activate=False,
            terrain_bounds=terrain_bounds,
        )
        obj.data.materials.append(terrain_material)
        if terrain_material_map is not None:
            obj["bc2_terrain_material_map_path"] = (
                level_scene.terrain_material_map_path
            )
            obj["bc2_terrain_material_map_side"] = (
                terrain_material_map.side
            )
        if level_scene.terrain_transform is not None:
            obj["bc2_source_terrain_translation"] = (
                level_scene.terrain_transform.blender_translation(
                    axis_conversion=settings.axis_conversion,
                    unit_scale=settings.unit_scale,
                )
            )
            obj["bc2_source_terrain_transform_applied"] = False
        terrain_objects.append(obj)

    water_object = None
    if level_scene.water_mesh_path:
        if progress:
            progress(0.93, "Creating level water mesh")
        asset, raw = repository.read_asset_path(
            level_scene.water_mesh_path,
            max_size=1 << 30,
            preferred_archives=preferred_archives,
        )
        parsed = parse_watermesh(raw, asset["platform"])
        mesh = _combined_level_mesh(
            parsed,
            f"BC2_{level_scene.name}_Water",
            settings,
        )
        water_material = _simple_material(
            "BC2 Water Approximation",
            (0.04, 0.18, 0.24, 0.65),
            roughness=0.12,
            transmission=0.25,
        )
        water_material["bc2_material_status"] = (
            "Approximation; WaterMesh shader arithmetic is not decoded"
        )
        mesh.materials.append(water_material)
        water_object = bpy.data.objects.new(
            f"{level_scene.name}_Water",
            mesh,
        )
        collections["water"].objects.link(water_object)
        water_object["bc2_asset_path"] = asset["path"]
        water_object["bc2_archive_path"] = asset["archive_path"]
        if level_scene.terrain_transform is not None:
            water_object["bc2_source_terrain_translation"] = (
                level_scene.terrain_transform.blender_translation(
                    axis_conversion=settings.axis_conversion,
                    unit_scale=settings.unit_scale,
                )
            )
            water_object["bc2_source_terrain_transform_applied"] = False

    diagnostics = _deduplicate_diagnostics(diagnostics)
    summary = {
        "level": level_scene.name,
        "placements": len(created_objects),
        "mesh_variants": len(mesh_by_key),
        "unique_mesh_resources": len(geometry_cache),
        "heightfields": len(terrain_objects),
        "terrain_material_maps": int(terrain_material_map is not None),
        "water_meshes": int(water_object is not None),
        "materials": len(material_cache),
        "images": sum(
            image is not None for image, _ in image_cache.values()
        ),
        "diagnostics": len(diagnostics),
    }
    text = bpy.data.texts.new(f"BC2_{level_scene.name}_Diagnostics.json")
    text.write(
        json.dumps(
            {
                "summary": summary,
                "issues": diagnostics,
            },
            indent=2,
            sort_keys=True,
        )
    )
    diagnostic_object = bpy.data.objects.new(
        f"{level_scene.name}_Import_Diagnostics",
        None,
    )
    collections["diagnostics"].objects.link(diagnostic_object)
    diagnostic_object["bc2_diagnostics_text"] = text.name
    for key, value in summary.items():
        diagnostic_object[f"bc2_{key}"] = value

    root["bc2_level"] = level_scene.name
    root["bc2_placement_count"] = len(created_objects)
    root["bc2_mesh_variant_count"] = len(mesh_by_key)
    root["bc2_unique_mesh_count"] = len(geometry_cache)
    root["bc2_heightfield_count"] = len(terrain_objects)
    root["bc2_diagnostics_text"] = text.name
    if level_scene.terrain_transform is not None:
        root["bc2_source_terrain_translation"] = (
            level_scene.terrain_transform.blender_translation(
                axis_conversion=settings.axis_conversion,
                unit_scale=settings.unit_scale,
            )
        )
        root["bc2_source_terrain_transform_applied"] = False
    if progress:
        progress(1.0, "Map scene created")
    return {
        "root": root,
        "objects": tuple(created_objects),
        "meshes": mesh_by_key,
        "terrain": tuple(terrain_objects),
        "water": water_object,
        "diagnostics": tuple(diagnostics),
        "summary": summary,
    }


class BC2_UL_assets(UIList):
    def draw_item(
        self, context, layout, data, item, icon, active_data, active_property, index
    ):
        row = layout.row(align=True)
        row.label(text=_basename(item.path), icon="FILE")
        row.label(text=item.asset_type)
        row.label(text=f"{item.size / 1024:.1f} KiB")


class BC2_UL_archives(UIList):
    def draw_item(
        self, context, layout, data, item, icon, active_data, active_property, index
    ):
        status_icon = "CHECKMARK" if item.indexed else "PACKAGE"
        row = layout.row(align=True)
        row.label(text=item.path, icon=status_icon)
        if item.indexed:
            row.label(text=f"{item.asset_count:,}")
        if item.error:
            row.label(text="", icon="ERROR")


class BC2_OT_choose_source(Operator, ImportHelper):
    bl_idname = "bc2.choose_source"
    bl_label = "Choose Bad Company 2 Disc"
    bl_description = "Select a PC or Xbox 360 Battlefield: Bad Company 2 ISO"

    filename_ext = ".iso"
    filter_glob: StringProperty(default="*.iso", options={"HIDDEN"})

    def execute(self, context):
        settings = _settings(context)
        settings.source_path = self.filepath
        settings.status = "Disc selected; click Scan Disc"
        _close_repository()
        return {"FINISHED"}


class BC2_OT_scan_source(Operator):
    bl_idname = "bc2.scan_source"
    bl_label = "Scan Disc"
    bl_description = (
        "Catalog Frostbite archives; Xbox assets are indexed immediately, while PC "
        "archives can be indexed on demand"
    )

    def execute(self, context):
        settings = _settings(context)
        window_manager = context.window_manager
        try:
            repository = _get_repository(context, require_scan=False)
            window_manager.progress_begin(0, 1000)

            def progress(current, total, label):
                fraction = current / max(1, total)
                window_manager.progress_update(int(fraction * 1000))
                settings.status = f"Scanning {current:,}/{total:,}: {label}"

            failures = repository.scan(progress)
            settings.source_kind = repository.kind
            _load_archives(context)
            _load_assets(context, reset_page=True)
            summary = (
                f"{repository.kind}: {repository.catalog.archive_count():,} archives, "
                f"{settings.total_results:,} indexed assets"
            )
            if failures:
                settings.status = f"{summary}; {len(failures)} archive(s) failed"
                self.report({"WARNING"}, settings.status)
            else:
                settings.status = summary
                self.report({"INFO"}, settings.status)
            return {"FINISHED"}
        except (BC2Error, OSError) as exc:
            settings.status = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            window_manager.progress_end()


class BC2_OT_refresh_archives(Operator):
    bl_idname = "bc2.refresh_archives"
    bl_label = "Refresh Archives"

    def execute(self, context):
        try:
            _load_archives(context)
            return {"FINISHED"}
        except (BC2Error, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class BC2_OT_index_archive(Operator):
    bl_idname = "bc2.index_archive"
    bl_label = "Index Selected Archive"
    bl_description = (
        "Extract the selected PC Frostbite archive from its cabinet and index its assets"
    )

    def execute(self, context):
        settings = _settings(context)
        try:
            archive = _selected_archive(settings)
            repository = _get_repository(context)
            settings.status = f"Indexing {archive.path}"
            count = repository.index_archive(archive.archive_id)
            _load_archives(context)
            _load_assets(context, reset_page=True)
            settings.status = f"Indexed {count:,} assets from {archive.path}"
            self.report({"INFO"}, settings.status)
            return {"FINISHED"}
        except (BC2Error, OSError) as exc:
            settings.status = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class BC2_OT_import_map(Operator):
    bl_idname = "bc2.import_map"
    bl_label = "Import Complete PC Map"
    bl_description = (
        "Resolve a retail PC level, shared multiplayer assets, grouped "
        "placements, terrain, water, and DX11 shader bindings"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = _settings(context)
        window_manager = context.window_manager
        level_name = settings.map_name.strip().upper()
        if not level_name or any(
            not (character.isalnum() or character == "_")
            for character in level_name
        ):
            self.report(
                {"ERROR"},
                "Map name may contain only letters, digits, and underscores",
            )
            return {"CANCELLED"}

        window_manager.progress_begin(0, 1000)
        try:
            repository = _get_repository(context)
            if repository.kind != "PC_ISO":
                raise BC2Error(
                    "Complete-map import currently supports PC retail ISOs"
                )
            settings.map_name = level_name
            settings.status = f"Indexing archives required by {level_name}"

            def index_progress(current, total, label):
                window_manager.progress_update(
                    int(200 * current / max(1, total))
                )
                settings.status = (
                    f"Indexing map archive {current:,}/{total:,}: {label}"
                )

            preferred_archives = index_pc_level_archives(
                repository,
                level_name,
                index_progress,
            )
            window_manager.progress_update(200)
            settings.status = f"Resolving {level_name} placement graph"

            def resolve_progress(fraction, label):
                window_manager.progress_update(
                    200 + int(max(0.0, min(1.0, fraction)) * 200)
                )
                settings.status = label

            level_scene = LevelResolver(
                repository,
                level_name,
                preferred_archives,
            ).resolve(resolve_progress)

            def create_progress(fraction, label):
                window_manager.progress_update(
                    400 + int(max(0.0, min(1.0, fraction)) * 600)
                )
                settings.status = label

            result = _create_level_scene(
                context,
                repository,
                level_scene,
                preferred_archives,
                settings,
                create_progress,
            )
            summary = result["summary"]
            settings.status = (
                f"Imported {level_name}: {summary['placements']:,} objects, "
                f"{summary['unique_mesh_resources']:,} unique meshes, "
                f"{summary['heightfields']:,} terrain tiles"
            )
            severity = (
                {"WARNING"} if summary["diagnostics"] else {"INFO"}
            )
            self.report(
                severity,
                (
                    f"{settings.status}; "
                    f"{summary['diagnostics']:,} diagnostic(s)"
                ),
            )
            return {"FINISHED"}
        except (BC2Error, OSError, RuntimeError, ValueError) as exc:
            settings.status = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        finally:
            window_manager.progress_end()


class BC2_OT_refresh_assets(Operator):
    bl_idname = "bc2.refresh_assets"
    bl_label = "Refresh Assets"

    reset_page: BoolProperty(default=True, options={"HIDDEN"})

    def execute(self, context):
        try:
            _load_assets(context, reset_page=self.reset_page)
            return {"FINISHED"}
        except (BC2Error, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class BC2_OT_change_page(Operator):
    bl_idname = "bc2.change_page"
    bl_label = "Change Asset Page"

    delta: IntProperty(default=1)

    def execute(self, context):
        settings = _settings(context)
        settings.page = max(0, settings.page + self.delta)
        try:
            _load_assets(context)
            return {"FINISHED"}
        except (BC2Error, OSError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class BC2_OT_open_browser(Operator):
    bl_idname = "bc2.open_browser"
    bl_label = "Browse Bad Company 2 Assets"

    category: EnumProperty(items=CATEGORIES, default="MESH")

    def invoke(self, context, event):
        settings = _settings(context)
        settings.category = self.category
        try:
            _load_assets(context, reset_page=True)
        except BC2Error as exc:
            settings.status = str(exc)
        return context.window_manager.invoke_props_dialog(self, width=900)

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        row = layout.row(align=True)
        row.prop(settings, "category")
        row.prop(settings, "search", text="")
        refresh = row.operator(
            BC2_OT_refresh_assets.bl_idname, text="", icon="FILE_REFRESH"
        )
        refresh.reset_page = True
        layout.template_list(
            BC2_UL_assets.__name__,
            "menu_browser",
            settings,
            "assets",
            settings,
            "active_asset_index",
            rows=14,
        )
        row = layout.row(align=True)
        row.operator(BC2_OT_import_selected.bl_idname, icon="IMPORT")
        row.operator(BC2_OT_extract_selected.bl_idname, icon="EXPORT")

    def execute(self, context):
        return {"FINISHED"}


class BC2_OT_import_selected(Operator):
    bl_idname = "bc2.import_selected"
    bl_label = "Import Selected"
    bl_description = (
        "Create Blender data for supported assets; unsupported resources are extracted "
        "to the cache"
    )

    def execute(self, context):
        settings = _settings(context)
        try:
            selected = _selected_asset(settings)
            repository = _get_repository(context)
            asset_type = selected.asset_type
            if asset_type in {
                "MeshData",
                "RigidMeshSet",
                "SkinnedMeshSet",
                "CompositeMeshSet",
                "TreeMeshSet",
            }:
                source_asset = repository.catalog.asset(selected.asset_id)
                asset, raw = repository.read_asset(
                    selected.asset_id, max_size=1 << 30, resolve_mesh=True
                )
                parsed = parse_meshdata(
                    raw,
                    asset["platform"],
                    include_z_only=settings.include_z_only,
                )
                if (
                    asset_type == "SkinnedMeshSet"
                    and source_asset["platform"] == "pc"
                ):
                    armature, objects = _create_character(
                        context,
                        repository,
                        source_asset,
                        parsed,
                        asset,
                        settings,
                    )
                    self.report(
                        {"INFO"},
                        f"Imported {len(objects)} skinned mesh object(s) "
                        f"on {len(armature.data.bones)} bones",
                    )
                elif asset_type == "CompositeMeshSet":
                    _, objects = _create_mesh_set_import(
                        context,
                        repository,
                        source_asset,
                        parsed,
                        asset,
                        settings,
                    )
                    self.report(
                        {"INFO"},
                        f"Imported {len(objects)} modular mesh object(s)",
                    )
                else:
                    objects = _create_mesh_objects(
                        context, parsed, asset, settings
                    )
                    self.report(
                        {"INFO"}, f"Imported {len(objects)} mesh object(s)"
                    )
            elif asset_type == "GrannyModel":
                asset, raw = repository.read_asset(
                    selected.asset_id, max_size=256 << 20
                )
                skeleton = parse_granny_model(raw, asset["platform"])
                armature = _create_armature_object(
                    context, skeleton, asset, settings
                )
                self.report(
                    {"INFO"},
                    f"Imported armature with {len(armature.data.bones)} bones",
                )
            elif asset_type == "GrannyAnimation":
                asset, raw = repository.read_asset(
                    selected.asset_id, max_size=256 << 20
                )
                armature, action = _import_granny_animation(
                    context,
                    repository,
                    asset,
                    raw,
                    settings,
                )
                self.report(
                    {"INFO"},
                    f"Imported {action.name} on {armature.name} "
                    f"({int(action['bc2_track_count'])} tracks)",
                )
            elif asset_type == "WaterMesh":
                asset, raw = repository.read_asset(
                    selected.asset_id, max_size=1 << 30
                )
                parsed = parse_watermesh(raw, asset["platform"])
                objects = _create_mesh_objects(context, parsed, asset, settings)
                self.report({"INFO"}, f"Imported {len(objects)} water mesh object(s)")
            elif asset_type in {"ITexture", "XenonTexture", "Ps3Texture"}:
                asset, raw = repository.read_asset(
                    selected.asset_id, max_size=1 << 30
                )
                _, _, converted = _import_texture(context, repository, asset, raw)
                self.report(
                    {"INFO"},
                    f"Imported {converted.width}x{converted.height} {converted.format_name}",
                )
            elif asset_type == "TerrainHeightfield":
                asset, raw = repository.read_asset(
                    selected.asset_id, max_size=1 << 30
                )
                parsed = parse_heightfield(raw, settings.terrain_resolution)
                _create_terrain_object(context, parsed, asset, settings)
                self.report(
                    {"INFO"},
                    f"Imported terrain preview from {parsed.source_resolution}x"
                    f"{parsed.source_resolution} samples",
                )
            elif asset_type == "<non-resource>" and selected.path.casefold().endswith(
                ".dbx"
            ):
                asset, raw = repository.read_asset(
                    selected.asset_id, max_size=256 << 20
                )
                document = parse_dbx(raw)
                assembly = modular_assembly(document, str(asset["path"]))
                if assembly is not None:
                    imported = _create_modular_assembly(
                        context,
                        repository,
                        asset,
                        assembly,
                        settings,
                    )
                    object_count = sum(
                        len(component.objects)
                        for component in imported.components
                    )
                    self.report(
                        {"INFO"},
                        f"Imported {assembly.kind.lower()} assembly with "
                        f"{len(imported.components)} component(s) and "
                        f"{object_count} mesh object(s)",
                    )
                else:
                    xml = dbx_to_xml(raw)
                    text = bpy.data.texts.new(
                        Path(asset["path"]).stem + ".xml"
                    )
                    text.write(xml)
                    text["bc2_asset_path"] = asset["path"]
                    text["bc2_archive_path"] = asset["archive_path"]
                    self.report(
                        {"INFO"},
                        f"Imported DBX as text: {text.name}",
                    )
            else:
                _, path = repository.extract_asset(selected.asset_id)
                self.report({"INFO"}, f"Extracted unsupported resource to {path}")
            return {"FINISHED"}
        except (BC2Error, OSError, ValueError) as exc:
            settings.status = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class BC2_OT_extract_selected(Operator):
    bl_idname = "bc2.extract_selected"
    bl_label = "Extract Raw"
    bl_description = "Extract the selected asset to the configured cache"

    def execute(self, context):
        settings = _settings(context)
        try:
            selected = _selected_asset(settings)
            _, path = _get_repository(context).extract_asset(selected.asset_id)
            settings.status = f"Extracted to {path}"
            self.report({"INFO"}, settings.status)
            return {"FINISHED"}
        except (BC2Error, OSError) as exc:
            settings.status = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class BC2_MT_import(Menu):
    bl_idname = "BC2_MT_import"
    bl_label = "Bad Company 2 / Frostbite 1"

    def draw(self, context):
        layout = self.layout
        layout.operator(BC2_OT_choose_source.bl_idname, icon="DISK_DRIVE")
        layout.operator(BC2_OT_scan_source.bl_idname, icon="VIEWZOOM")
        layout.operator(BC2_OT_import_map.bl_idname, icon="WORLD_DATA")
        layout.separator()
        for category, label, description, icon, _ in CATEGORIES[1:]:
            operator = layout.operator(
                BC2_OT_open_browser.bl_idname,
                text=f"Browse {label}",
                icon=icon,
            )
            operator.category = category


class BC2_PT_asset_browser(Panel):
    bl_idname = "BC2_PT_asset_browser"
    bl_label = "Bad Company 2 Assets"
    bl_category = "Frostbite"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)

        source = layout.box()
        source.label(text="Source", icon="DISK_DRIVE")
        source.prop(settings, "source_path", text="")
        row = source.row(align=True)
        row.operator(BC2_OT_choose_source.bl_idname, text="Choose", icon="FILE_FOLDER")
        row.operator(BC2_OT_scan_source.bl_idname, text="Scan", icon="VIEWZOOM")
        if settings.source_kind:
            source.label(text=settings.source_kind, icon="CHECKMARK")
        source.label(text=settings.status)

        if settings.source_kind == "PC_ISO":
            map_box = layout.box()
            map_box.label(text="Complete PC Map", icon="WORLD_DATA")
            map_box.prop(settings, "map_name")
            map_box.prop(settings, "map_import_materials")
            map_box.prop(settings, "terrain_resolution")
            map_box.operator(
                BC2_OT_import_map.bl_idname,
                text="Import Map",
                icon="IMPORT",
            )

            archives = layout.box()
            archives.label(text="PC Frostbite Archives", icon="PACKAGE")
            row = archives.row(align=True)
            row.prop(settings, "archive_search", text="")
            row.operator(
                BC2_OT_refresh_archives.bl_idname, text="", icon="FILE_REFRESH"
            )
            archives.template_list(
                BC2_UL_archives.__name__,
                "",
                settings,
                "archives",
                settings,
                "active_archive_index",
                rows=5,
            )
            archives.operator(
                BC2_OT_index_archive.bl_idname,
                text="Index Selected Archive",
                icon="PACKAGE",
            )

        browser = layout.box()
        browser.label(text="Indexed Assets", icon="ASSET_MANAGER")
        browser.prop(settings, "category")
        row = browser.row(align=True)
        row.prop(settings, "search", text="")
        refresh = row.operator(
            BC2_OT_refresh_assets.bl_idname, text="", icon="FILE_REFRESH"
        )
        refresh.reset_page = True
        browser.template_list(
            BC2_UL_assets.__name__,
            "",
            settings,
            "assets",
            settings,
            "active_asset_index",
            rows=8,
        )
        total_pages = max(1, (settings.total_results - 1) // RESULTS_PER_PAGE + 1)
        paging = browser.row(align=True)
        previous = paging.operator(
            BC2_OT_change_page.bl_idname, text="", icon="TRIA_LEFT"
        )
        previous.delta = -1
        paging.label(
            text=f"Page {settings.page + 1}/{total_pages} "
            f"({settings.total_results:,} assets)"
        )
        following = paging.operator(
            BC2_OT_change_page.bl_idname, text="", icon="TRIA_RIGHT"
        )
        following.delta = 1

        options = layout.box()
        options.label(text="Import Options", icon="PREFERENCES")
        options.prop(settings, "axis_conversion")
        options.prop(settings, "flip_v")
        options.prop(settings, "unit_scale")
        options.prop(settings, "include_z_only")
        options.prop(settings, "terrain_resolution")
        row = layout.row(align=True)
        row.operator(BC2_OT_import_selected.bl_idname, icon="IMPORT")
        row.operator(BC2_OT_extract_selected.bl_idname, icon="EXPORT")


def _draw_import_menu(self, context):
    self.layout.menu(BC2_MT_import.bl_idname, icon="PACKAGE")


CLASSES = (
    BC2AddonPreferences,
    BC2AssetListItem,
    BC2ArchiveListItem,
    BC2Settings,
    BC2_UL_assets,
    BC2_UL_archives,
    BC2_OT_choose_source,
    BC2_OT_scan_source,
    BC2_OT_refresh_archives,
    BC2_OT_index_archive,
    BC2_OT_import_map,
    BC2_OT_refresh_assets,
    BC2_OT_change_page,
    BC2_OT_open_browser,
    BC2_OT_import_selected,
    BC2_OT_extract_selected,
    BC2_MT_import,
    BC2_PT_asset_browser,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.bc2_settings = PointerProperty(type=BC2Settings)
    bpy.types.TOPBAR_MT_file_import.append(_draw_import_menu)


def unregister():
    _close_repository()
    bpy.types.TOPBAR_MT_file_import.remove(_draw_import_menu)
    del bpy.types.Scene.bc2_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
