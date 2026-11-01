# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
import tempfile
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
from mathutils import Matrix

from .core.dbx import dbx_to_xml
from .core.errors import BC2Error
from .core.io import safe_cache_name
from .core.level import (
    LevelResolver,
    MeshDefinition,
    SurfaceShader,
    index_pc_level_archives,
)
from .core.mesh import MeshAsset, parse_meshdata, parse_watermesh
from .core.repository import AssetRepository
from .core.shaderdb import ShaderDatabase, parse_shader_database
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


def _create_vertex_groups(obj, influences):
    groups = {}
    for vertex_index, vertex_influences in enumerate(influences):
        for bone, weight in vertex_influences:
            groups.setdefault(bone, {}).setdefault(weight, []).append(
                vertex_index
            )
    for bone, weights in groups.items():
        group = obj.vertex_groups.new(name=f"Bone_{bone:03d}")
        for weight, vertex_indices in weights.items():
            group.add(vertex_indices, weight, "REPLACE")


def _create_mesh_objects(
    context,
    parsed: MeshAsset,
    asset,
    settings: BC2Settings,
):
    collection = _new_collection(context, Path(asset["path"]).stem)
    created = []
    for subset_index, subset in enumerate(parsed.subsets):
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
        mesh["bc2_uv_offsets"] = json.dumps(
            subset.uv_offsets,
            separators=(",", ":"),
        )
        material = bpy.data.materials.new(subset.name)
        material.diffuse_color = (0.8, 0.8, 0.8, 1.0)
        mesh.materials.append(material)
        obj = bpy.data.objects.new(
            f"{subset_index:02d}_{subset.name}", mesh
        )
        collection.objects.link(obj)
        _create_vertex_groups(obj, subset.influences)
        obj["bc2_asset_path"] = asset["path"]
        obj["bc2_archive_path"] = asset["archive_path"]
        obj["bc2_platform"] = asset["platform"]
        created.append(obj)
    if created:
        bpy.context.view_layer.objects.active = created[0]
        created[0].select_set(True)
    return created


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
    stem = PurePosixPath(path).name.casefold()
    if role == "OCCLUSION" and any(
        marker in stem
        for marker in ("ocrgb", "oc_rgb", "oc-rgb")
    ):
        return "UV1"
    return "UVMap"


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


def _dxt5nm_output(
    nodes,
    links,
    image_node,
    *,
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
    x_output = _signed_channel(
        nodes,
        links,
        image_node.outputs["Alpha"],
        "DXT5nm X",
        (-280, 120),
    )
    y_output = _signed_channel(
        nodes,
        links,
        separate.outputs["Green"],
        "DXT5nm Y",
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


def _standard_normal_output(nodes, links, image_node):
    normal_map = nodes.new("ShaderNodeNormalMap")
    normal_map.location = (300, -260)
    normal_map.space = "TANGENT"
    normal_map.inputs["Strength"].default_value = 1.0
    links.new(image_node.outputs["Color"], normal_map.inputs["Color"])
    return normal_map.outputs["Normal"]


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

    roles = _binding_roles(shader_material.textures)
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
                    non_color=role in {
                        "NORMAL",
                        "MASK",
                        "NOISE",
                        "OCCLUSION",
                        "ROUGHNESS",
                        "SPECULAR",
                    },
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
        if role in {
            "NORMAL",
            "MASK",
            "NOISE",
            "OCCLUSION",
            "ROUGHNESS",
            "SPECULAR",
        }:
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
        uv_map = _binding_uv_map(binding.path, role)
        uv_node = uv_nodes.get(uv_map)
        if uv_node is None:
            uv_node = nodes.new("ShaderNodeUVMap")
            uv_node.uv_map = uv_map
            uv_node.name = f"BC2 {uv_map}"
            uv_node.label = uv_map
            uv_node.location = (-1050, 400 - len(uv_nodes) * 180)
            uv_nodes[uv_map] = uv_node
        links.new(uv_node.outputs["UV"], node.inputs["Vector"])
        node["bc2_uv_map"] = uv_map
        texture_nodes.append((binding, role, node))
        if role == "NORMAL" and normal_node is None:
            normal_node = node
            normal_format = converted.format_name
            normal_path = binding.path

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
                asset, raw = repository.read_asset(
                    selected.asset_id, max_size=1 << 30, resolve_mesh=True
                )
                parsed = parse_meshdata(
                    raw,
                    asset["platform"],
                    include_z_only=settings.include_z_only,
                )
                objects = _create_mesh_objects(context, parsed, asset, settings)
                self.report({"INFO"}, f"Imported {len(objects)} mesh object(s)")
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
                xml = dbx_to_xml(raw)
                text = bpy.data.texts.new(Path(asset["path"]).stem + ".xml")
                text.write(xml)
                text["bc2_asset_path"] = asset["path"]
                text["bc2_archive_path"] = asset["archive_path"]
                self.report({"INFO"}, f"Imported DBX as text: {text.name}")
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
