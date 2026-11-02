# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import queue
import tempfile
import threading
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
    CharacterAnimationLinks,
    ModularAssembly,
    animation_tree_base_set_references,
    animation_tree_clip_references,
    animation_tree_replacement_references,
    character_animation_links,
    modular_assembly,
    vehicle_entity_path_for_mesh_set,
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
    SurfaceShader,
    discover_pc_levels,
    index_pc_level_archives,
    pc_level_archive_paths,
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
_MAX_RELATED_ANIMATIONS = 64
_MAX_CHARACTER_ANIMATION_DESCRIPTORS = 256
_CORE_GAMEPLAY_TREE_STEM = "soldieranimtree3prif"
_CORE_GAMEPLAY_ANIMATION_PATHS = (
    "characters/animations/bset_rif/3p/standidle.res",
    "characters/animations/bset_rif/3p/standrelaxedidle.res",
    "characters/animations/bset_rif/3p/crouchidle.res",
    "characters/animations/bset_rif/3p/walkforward.res",
    "characters/animations/bset_rif/3p/walkbackward.res",
    "characters/animations/bset_rif/3p/runforward.res",
    "characters/animations/bset_rif/3p/runbackward.res",
    "characters/animations/bset_rif/3p/sneakforward.res",
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
    loaded_shader_assets: set[int]
    material_sources: dict[str, str]


@dataclass(frozen=True)
class _ImportedAssemblyComponent:
    definition: AssemblyComponent
    collection: object
    objects: tuple[object, ...]
    mesh_type: int
    armature: object | None = None


@dataclass(frozen=True)
class _RelatedAnimationImport:
    enabled: bool
    actions: tuple[object, ...] = ()
    issues: tuple[str, ...] = ()
    indexed_archive_count: int = 0
    total_archive_count: int = 0

    @property
    def catalog_complete(self) -> bool:
        return self.indexed_archive_count == self.total_archive_count


@dataclass(frozen=True)
class _RelatedAnimationAsset:
    asset: object
    additive_base_asset: object | None = None


@dataclass(frozen=True)
class _CharacterAnimationSource:
    links: CharacterAnimationLinks
    archive_path: str
    descriptor_path: str


@dataclass(frozen=True)
class _ImportedAssembly:
    definition: ModularAssembly
    root_collection: object
    armature: object | None
    components: tuple[_ImportedAssemblyComponent, ...]
    diagnostics_text: object | None
    related_animations: _RelatedAnimationImport | None = None


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
CONTENT_GROUPS = (
    ("ALL", "All content", "Do not filter by semantic content"),
    ("CHARACTER", "Characters", "Characters, soldiers, faces, and animals"),
    ("VEHICLE", "Vehicles", "Vehicle assets and descriptors"),
    ("WEAPON", "Weapons", "Weapon assets and descriptors"),
    ("BUILDING", "Buildings", "Buildings and architectural assets"),
    ("PROP", "Props", "Other object, scenery, and gameplay props"),
    ("VEGETATION", "Vegetation", "Vegetation and undergrowth assets"),
    ("LEVEL", "Levels & Terrain", "Levels, terrain, weather, and game modes"),
    ("EFFECT", "Effects", "Effects, FX, and decals"),
    ("UI", "UI & Movies", "Menus, movies, awards, fonts, and localization"),
    ("AUDIO", "Audio", "Sound assets"),
    ("OTHER", "Other content", "Content outside the known path groups"),
)

_ASSET_TYPE_ICONS = {
    "AimAnimation": "ANIM_DATA",
    "AnimTreeInfo": "ANIM_DATA",
    "BinkMemory": "FILE_MOVIE",
    "CompositeMeshSet": "OUTLINER_COLLECTION",
    "DeltaAnimation": "ANIM_DATA",
    "Dx9ShaderDatabase": "SHADING_TEXTURE",
    "GrannyAnimation": "ANIM_DATA",
    "GrannyModel": "ARMATURE_DATA",
    "HavokPhysicsData": "PHYSICS",
    "ImpulseResponse": "SOUND",
    "ITexture": "IMAGE_DATA",
    "IrradianceVolume": "WORLD_DATA",
    "MeshData": "MESH_DATA",
    "OccluderMesh": "MESH_DATA",
    "Ps3Texture": "IMAGE_DATA",
    "RagdollResource": "PHYSICS",
    "RigidMeshSet": "OUTLINER_OB_MESH",
    "SkinnedMeshSet": "MOD_ARMATURE",
    "SootMesh": "MESH_DATA",
    "SwfMovie": "FILE_MOVIE",
    "Terrain": "MESH_GRID",
    "TerrainHeightfield": "RNDCURVE",
    "TerrainMaterialMap": "IMAGE_DATA",
    "TreeMeshSet": "OUTLINER_OB_CURVES",
    "VisualTerrain": "MESH_GRID",
    "VisualWater": "WORLD_DATA",
    "WaterMesh": "MESH_DATA",
    "Wave": "SOUND",
    "WeatherSystem": "WORLD_DATA",
    "XenonTexture": "IMAGE_DATA",
}

MATERIAL_MODES = (
    (
        "FULL",
        "Materials and Textures",
        "Resolve source surfaces, shader bindings, and texture images",
    ),
    (
        "MATERIALS_ONLY",
        "Materials Only",
        "Create source-named materials without loading texture images",
    ),
    (
        "PLACEHOLDER",
        "Simple Placeholders",
        "Create neutral materials without source material or texture lookup",
    ),
)

COMPOSITE_VISIBILITY_MODES = (
    (
        "DEFAULT_STATE",
        "Default State",
        "Hide damaged, destroyed, blurred, and verified wreck variants",
    ),
    (
        "SHOW_ALL",
        "Show All Imported Parts",
        "Keep every imported composite part visible while retaining state metadata",
    ),
)

def _default_cache_directory() -> str:
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    return str(root / "BC2Blender" / "Cache")


def _repository_preference_updated(self, context):
    cancel_event = globals().get("_active_scan_cancel")
    if cancel_event is not None:
        cancel_event.set()
    close_repository = globals().get("_close_repository")
    if close_repository is not None:
        close_repository()
    window_manager = getattr(context, "window_manager", None)
    settings = getattr(window_manager, "bc2_settings", None)
    if settings is None:
        return
    settings.source_kind = ""
    settings.status = "Source or cache changed; scan the disc"
    settings.archives.clear()
    settings.assets.clear()
    settings.total_results = 0
    settings.indexed_asset_count = 0


class BC2AddonPreferences(AddonPreferences):
    bl_idname = ADDON_ID

    source_path: StringProperty(
        name="Last disc image",
        description="Battlefield: Bad Company 2 PC or Xbox 360 ISO",
        subtype="FILE_PATH",
        update=_repository_preference_updated,
    )
    cache_directory: StringProperty(
        name="Asset cache",
        description=(
            "Cabinets, extracted archives, the SQLite catalog, and converted DDS files "
            "are cached here"
        ),
        subtype="DIR_PATH",
        default=_default_cache_directory(),
        update=_repository_preference_updated,
    )
    default_axis_conversion: BoolProperty(
        name="Convert Y-up to Blender Z-up",
        default=True,
    )
    default_flip_v: BoolProperty(
        name="Flip texture V",
        description="Convert DirectX UV orientation to Blender orientation",
        default=True,
    )
    default_unit_scale: FloatProperty(
        name="Scale",
        default=1.0,
        min=0.0001,
        max=10000.0,
    )
    default_include_z_only: BoolProperty(
        name="Include Z-only subsets",
        description="Import shadow/depth-only geometry in addition to visible subsets",
        default=False,
    )
    default_terrain_resolution: IntProperty(
        name="Terrain mesh resolution",
        description=(
            "Maximum vertices per terrain edge; 1024 preserves the source "
            "resolution of the MP_001 playable heightfield"
        ),
        default=1024,
        min=16,
        max=1024,
    )
    default_material_mode: EnumProperty(
        name="Material mode",
        items=MATERIAL_MODES,
        default="FULL",
    )
    default_composite_visibility: EnumProperty(
        name="Composite visibility",
        items=COMPOSITE_VISIBILITY_MODES,
        default="DEFAULT_STATE",
    )
    default_import_vehicle_assemblies: BoolProperty(
        name="Import linked vehicle assemblies",
        description=(
            "When a selected vehicle CompositeMeshSet has a matching "
            "same-name entity DBX, import the complete assembly including "
            "separate track components"
        ),
        default=True,
    )
    default_import_related_animations: BoolProperty(
        name="Import related animations",
        description=(
            "With a rigged PC mesh, import exact-folder clips and clips "
            "explicitly listed by character animation trees after verifying "
            "that each clip references that exact skeleton"
        ),
        default=True,
    )
    default_import_core_gameplay_animations: BoolProperty(
        name="Import core gameplay animations",
        description=(
            "For PC human soldiers, also import eight verified rifle "
            "locomotion clips explicitly linked through the Shared tree"
        ),
        default=True,
    )
    create_diagnostics: BoolProperty(
        name="Create detailed diagnostics Text datablocks",
        description=(
            "Create JSON Text datablocks for material and map diagnostics; "
            "warnings and errors are still reported when disabled"
        ),
        default=False,
    )

    def draw(self, context):
        source = self.layout.box()
        source.label(text="Source and Cache", icon="DISK_DRIVE")
        source.prop(self, "source_path")
        source.prop(self, "cache_directory")
        source.operator("bc2.open_cache_directory", icon="FILE_FOLDER")

        defaults = self.layout.box()
        defaults.label(text="Import Defaults", icon="PREFERENCES")
        defaults.prop(self, "default_axis_conversion")
        defaults.prop(self, "default_flip_v")
        defaults.prop(self, "default_unit_scale")
        defaults.prop(self, "default_include_z_only")
        defaults.prop(self, "default_terrain_resolution")
        defaults.prop(self, "default_material_mode")
        defaults.prop(self, "default_composite_visibility")
        defaults.prop(self, "default_import_vehicle_assemblies")
        defaults.prop(self, "default_import_related_animations")
        gameplay = defaults.row()
        gameplay.active = self.default_import_related_animations
        gameplay.prop(self, "default_import_core_gameplay_animations")

        diagnostics = self.layout.box()
        diagnostics.label(text="Diagnostics", icon="INFO")
        diagnostics.prop(self, "create_diagnostics")


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


class BC2MapListItem(PropertyGroup):
    name: StringProperty()


class BC2Settings(PropertyGroup):
    source_kind: StringProperty(name="Detected source")
    status: StringProperty(name="Status", default="Select a disc image, then scan")
    defaults_initialized: BoolProperty(default=False, options={"HIDDEN"})
    diagnostic_count: IntProperty(default=0, min=0, options={"HIDDEN"})
    scan_in_progress: BoolProperty(default=False, options={"HIDDEN"})
    content_group: EnumProperty(
        name="Content group",
        items=CONTENT_GROUPS,
        default="ALL",
    )
    category: EnumProperty(
        name="Resource type",
        items=CATEGORIES,
        default="MESH",
    )
    search: StringProperty(name="Search", description="Filter paths and resource types")
    archive_search: StringProperty(
        name="Archive search", description="Filter Frostbite archives"
    )
    page: IntProperty(default=0, min=0)
    total_results: IntProperty(default=0, min=0)
    indexed_asset_count: IntProperty(default=0, min=0)
    show_asset_sizes: BoolProperty(
        name="Sizes",
        description="Show file sizes in the asset list",
        default=False,
    )
    collapse_virtual_paths: BoolProperty(
        name="Collapse identical paths",
        description=(
            "Show one result when the same case-insensitive virtual path "
            "is stored in multiple archives"
        ),
        default=True,
    )
    hide_lod_variants: BoolProperty(
        name="Hide LOD variants",
        description=(
            "Hide internal MeshData resources named like "
            "*_lod0_data.res"
        ),
        default=True,
    )
    assets: CollectionProperty(type=BC2AssetListItem)
    active_asset_index: IntProperty(default=0)
    archives: CollectionProperty(type=BC2ArchiveListItem)
    active_archive_index: IntProperty(default=0)
    available_maps: CollectionProperty(type=BC2MapListItem)
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
    material_mode: EnumProperty(
        name="Material mode",
        items=MATERIAL_MODES,
        default="FULL",
    )
    composite_visibility: EnumProperty(
        name="Composite visibility",
        items=COMPOSITE_VISIBILITY_MODES,
        default="DEFAULT_STATE",
    )
    import_vehicle_assemblies: BoolProperty(
        name="Import linked vehicle assembly",
        description=(
            "When a selected vehicle CompositeMeshSet has a matching "
            "same-name entity DBX, import the complete assembly including "
            "separate track components"
        ),
        default=True,
    )
    import_related_animations: BoolProperty(
        name="Import related animations",
        description=(
            "Import indexed exact-folder and character-tree clips when they "
            "reference that exact skeleton"
        ),
        default=True,
    )
    import_core_gameplay_animations: BoolProperty(
        name="Import core gameplay animations",
        description=(
            "Import eight verified rifle locomotion clips for PC human "
            "soldiers through their explicit Shared animation tree"
        ),
        default=True,
    )
    map_name: StringProperty(
        name="Level name",
        description=(
            "Retail PC level discovered from the archive catalog or entered manually"
        ),
        default="MP_001",
    )


_repository: AssetRepository | None = None
_repository_key: tuple[str, str] | None = None
_active_scan_cancel: threading.Event | None = None
_active_scan_thread: threading.Thread | None = None


def _preferences(context) -> BC2AddonPreferences:
    return context.preferences.addons[ADDON_ID].preferences


def _apply_preference_defaults(
    settings: BC2Settings,
    preferences: BC2AddonPreferences,
    *,
    force: bool = False,
):
    if settings.defaults_initialized and not force:
        return
    settings.axis_conversion = preferences.default_axis_conversion
    settings.flip_v = preferences.default_flip_v
    settings.unit_scale = preferences.default_unit_scale
    settings.include_z_only = preferences.default_include_z_only
    settings.terrain_resolution = preferences.default_terrain_resolution
    settings.material_mode = preferences.default_material_mode
    settings.composite_visibility = preferences.default_composite_visibility
    settings.import_vehicle_assemblies = (
        preferences.default_import_vehicle_assemblies
    )
    settings.import_related_animations = (
        preferences.default_import_related_animations
    )
    settings.import_core_gameplay_animations = (
        preferences.default_import_core_gameplay_animations
    )
    settings.defaults_initialized = True


def _settings(context) -> BC2Settings:
    settings = context.window_manager.bc2_settings
    _apply_preference_defaults(settings, _preferences(context))
    return settings


def _material_mode(settings) -> str:
    value = getattr(settings, "material_mode", None)
    if value in {"FULL", "MATERIALS_ONLY", "PLACEHOLDER"}:
        return value
    if hasattr(settings, "map_import_materials"):
        return "FULL" if settings.map_import_materials else "PLACEHOLDER"
    if hasattr(settings, "import_textures"):
        return "FULL" if settings.import_textures else "MATERIALS_ONLY"
    return "FULL"


def _uses_source_materials(settings) -> bool:
    return _material_mode(settings) != "PLACEHOLDER"


def _loads_source_textures(settings) -> bool:
    return _material_mode(settings) == "FULL"


def _shows_all_composite_parts(settings) -> bool:
    return getattr(settings, "composite_visibility", "DEFAULT_STATE") == "SHOW_ALL"


def _detailed_diagnostics_enabled(context, settings=None) -> bool:
    explicit = getattr(settings, "create_diagnostics", None)
    if explicit is not None:
        return bool(explicit)
    addon = context.preferences.addons.get(ADDON_ID)
    return bool(addon and addon.preferences.create_diagnostics)


def _close_repository():
    global _repository, _repository_key
    if _repository is not None:
        _repository.close()
    _repository = None
    _repository_key = None


def _get_repository(context, require_scan: bool = True) -> AssetRepository:
    global _repository, _repository_key
    settings = _settings(context)
    preferences = _preferences(context)
    source_path = bpy.path.abspath(preferences.source_path)
    if not source_path:
        raise BC2Error("Select a PC or Xbox 360 disc image first")
    cache_path = bpy.path.abspath(preferences.cache_directory)
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


def _asset_type_icon(asset_type: str, path: str) -> str:
    if asset_type == "<non-resource>":
        suffix = PurePosixPath(path).suffix.casefold()
        if suffix in {".dbx", ".dbmanifest", ".xml"}:
            return "FILE_TEXT"
        if suffix in {".bik", ".bk2"}:
            return "FILE_MOVIE"
        return "FILE"
    return _ASSET_TYPE_ICONS.get(asset_type, "FILE")


def _load_archives(context):
    settings = _settings(context)
    repository = _get_repository(context)
    rows = repository.catalog.archives(settings.archive_search, limit=1000)
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
    all_archives = repository.catalog.archives(limit=10000)
    settings.available_maps.clear()
    for level_name in discover_pc_levels(
        str(row["path"]) for row in all_archives
    ):
        settings.available_maps.add().name = level_name


def _load_assets(context, reset_page: bool = False):
    settings = _settings(context)
    repository = _get_repository(context)
    settings.indexed_asset_count = repository.catalog.asset_count()
    if reset_page:
        settings.page = 0
    settings.total_results = repository.catalog.asset_count(
        settings.category,
        settings.search,
        settings.content_group,
        collapse_virtual_paths=settings.collapse_virtual_paths,
        hide_lod_variants=settings.hide_lod_variants,
    )
    max_page = max(0, (settings.total_results - 1) // RESULTS_PER_PAGE)
    settings.page = min(settings.page, max_page)
    rows = repository.catalog.search_assets(
        settings.category,
        settings.search,
        RESULTS_PER_PAGE,
        settings.page * RESULTS_PER_PAGE,
        settings.content_group,
        collapse_virtual_paths=settings.collapse_virtual_paths,
        hide_lod_variants=settings.hide_lod_variants,
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


def _scan_repository(repository, progress, cancelled=None):
    if not repository.needs_scan():
        return (), False
    return tuple(repository.scan(progress, cancelled)), True


def _scan_source_worker(
    source_path: str,
    cache_path: str,
    index_all_pc: bool,
    events: queue.Queue,
    cancel_event: threading.Event,
    target_archives: tuple[str, ...] = (),
):
    repository = None
    terminal = (
        "error",
        "Background indexing ended unexpectedly; see the system console",
    )
    try:
        repository = AssetRepository(source_path, cache_path)

        def scan_progress(current, total, label):
            events.put(
                (
                    "progress",
                    "Scanning archive catalog",
                    current,
                    total,
                    label,
                )
            )

        failures, scanned = _scan_repository(
            repository,
            scan_progress,
            cancel_event.is_set,
        )
        failures = list(failures)

        should_index_pending = not target_archives and (
            (repository.kind == "PC_ISO" and index_all_pc)
            or (repository.kind == "XBOX360_ISO" and not scanned)
        )
        processed_archives = 0
        if (
            not cancel_event.is_set()
            and target_archives
            and repository.kind != "PC_ISO"
        ):
            raise BC2Error("Map object indexing requires a PC retail ISO")
        if not cancel_event.is_set() and (target_archives or should_index_pending):
            def index_progress(current, total, label):
                nonlocal processed_archives
                processed_archives = current
                events.put(
                    (
                        "progress",
                        (
                            "Indexing selected archive"
                            if target_archives
                            else "Indexing archive"
                        ),
                        current,
                        total,
                        label,
                    )
                )

            if target_archives:
                failures.extend(
                    repository.index_archive_paths(
                        target_archives,
                        index_progress,
                        cancel_event.is_set,
                    )
                )
            else:
                failures.extend(
                    repository.index_all_archives(
                        index_progress,
                        cancel_event.is_set,
                    )
                )

        result = {
            "source_path": source_path,
            "cache_path": cache_path,
            "kind": repository.kind,
            "scanned": scanned,
            "index_all_pc": index_all_pc,
            "target_archives": target_archives,
            "processed_archives": processed_archives,
            "failures": tuple(failures),
        }
        if cancel_event.is_set():
            terminal = ("cancelled", result)
        else:
            terminal = ("complete", result)
    except (BC2Error, OSError, RuntimeError, ValueError) as exc:
        terminal = ("error", str(exc))
    finally:
        if repository is not None:
            repository.close()
        events.put(terminal)


def _apply_scan_result(context, operator, result, *, cancelled: bool):
    settings = _settings(context)
    preferences = _preferences(context)
    source_path = str(Path(bpy.path.abspath(preferences.source_path)).resolve())
    cache_path = str(Path(bpy.path.abspath(preferences.cache_directory)).resolve())
    if (
        source_path != result["source_path"]
        or cache_path != result["cache_path"]
    ):
        settings.source_kind = ""
        settings.status = "Source or cache changed; scan the current source"
        operator.report({"WARNING"}, settings.status)
        return

    settings.source_kind = result["kind"]
    repository = _get_repository(context)
    _load_archives(context)
    _load_assets(context, reset_page=True)
    summary = (
        f"{result['kind']}: "
        f"{repository.catalog.archive_count():,} archives, "
        f"{settings.indexed_asset_count:,} indexed assets"
    )
    if result["kind"] == "PC_ISO":
        summary += f", {len(settings.available_maps):,} discovered maps"
    if not result["scanned"]:
        summary += "; existing catalog preserved"
    if result["target_archives"]:
        summary += (
            f"; {result['processed_archives']:,}/"
            f"{len(result['target_archives']):,} targeted archives processed"
        )
    failures = result["failures"]
    if cancelled:
        settings.status = f"{summary}; indexing cancelled"
        operator.report({"WARNING"}, settings.status)
    elif failures:
        settings.status = f"{summary}; {len(failures)} archive(s) failed"
        operator.report({"WARNING"}, settings.status)
    else:
        if (
            result["kind"] == "PC_ISO"
            and result["index_all_pc"]
            and not result["target_archives"]
        ):
            summary += "; all archives indexed"
        elif (
            result["kind"] == "PC_ISO"
            and not settings.indexed_asset_count
        ):
            summary += (
                "; select a discovered map or index a relevant archive "
                "to populate object assets"
            )
        settings.status = summary
        operator.report({"INFO"}, settings.status)


def _validated_level_name(value: str) -> str:
    level_name = value.strip().upper()
    if not level_name or any(
        not (character.isalnum() or character == "_")
        for character in level_name
    ):
        raise BC2Error(
            "Map name may contain only letters, digits, and underscores"
        )
    return level_name


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
    show_all_parts = _shows_all_composite_parts(settings)
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
        target.hide_viewport = state != "DEFAULT" and not show_all_parts
        target.hide_render = state != "DEFAULT" and not show_all_parts
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
        mesh["bc2_uv_source"] = subset.uv_source
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
            obj["bc2_geometry_state"] = "DESTROYED"
            obj["bc2_geometry_visibility_source"] = (
                "name_contains_wreck_twice"
            )
            if not show_all_parts:
                obj.hide_viewport = True
                obj.hide_render = True
                obj.hide_set(True)
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
        collection["bc2_composite_visibility"] = (
            "SHOW_ALL" if show_all_parts else "DEFAULT_STATE"
        )
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


def _mesh_set_descriptor_asset(
    repository,
    mesh_set_asset,
    preferred_archives=(),
):
    descriptor_path = (
        _resource_partition(str(mesh_set_asset["path"])) + ".dbx"
    )
    preferred = tuple(
        dict.fromkeys(
            (
                str(mesh_set_asset["archive_path"]),
                *preferred_archives,
            )
        )
    )
    return repository.catalog.find_asset(
        descriptor_path,
        preferred,
    )


def _reference_resource_path(reference: str) -> str:
    if not reference or reference.casefold() == "null" or "/" not in reference:
        raise BC2Error("DBX resource reference is missing or invalid")
    return reference.rsplit("/", 1)[0] + ".res"


def _reference_descriptor_path(reference: str) -> str:
    if not reference or reference.casefold() == "null" or "/" not in reference:
        raise BC2Error("DBX descriptor reference is missing or invalid")
    return reference.rsplit("/", 1)[0] + ".dbx"


def _skeleton_path_for_document(document, descriptor_path: str) -> str:
    instance = document.primary_instance
    if instance is None:
        raise BC2Error(
            f"DBX descriptor has no primary instance: {descriptor_path}"
        )
    field = instance.child("field", "Skeleton")
    reference = field.attribute("ref") if field is not None else None
    return _reference_resource_path(reference or "")


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
    return _skeleton_path_for_document(parse_dbx(raw), descriptor_path)


def _read_referenced_skeleton(
    repository,
    resource_asset,
    skeleton_reference: str | None = None,
):
    skeleton_path = (
        _reference_resource_path(skeleton_reference)
        if skeleton_reference is not None
        else _skeleton_path_for_resource(repository, resource_asset)
    )
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
    diagnostic_count = len(material_diagnostics)
    diagnostics_text = _create_diagnostics_text(
        context,
        settings,
        (
            f"BC2_{Path(mesh_set_asset['path']).stem}_"
            "Material_Diagnostics.json"
        ),
        {
            "summary": material_summary,
            "issues": material_diagnostics,
        },
    )
    root_collection["bc2_diagnostic_count"] = diagnostic_count
    armature_obj["bc2_diagnostic_count"] = diagnostic_count
    if diagnostics_text is not None:
        armature_obj["bc2_material_diagnostics_text"] = diagnostics_text.name
    armature_obj["bc2_resolved_material_count"] = material_summary[
        "resolved_materials"
    ]
    context.view_layer.objects.active = armature_obj
    armature_obj.select_set(True)
    related_animations = _import_related_animations(
        context,
        repository,
        mesh_set_asset,
        skeleton_asset,
        skeleton,
        armature_obj,
        settings,
    )
    return armature_obj, objects, related_animations


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
        retain_pc_vehicle_uv_candidates=(
            component.mesh_set_path.casefold().startswith(
                "objects/vehicles/"
            )
        ),
    )
    return mesh_set_asset, mesh_set_raw, mesh_asset, parsed


def _vehicle_assembly_for_mesh_resource(repository, mesh_set_asset):
    entity_path = vehicle_entity_path_for_mesh_set(
        str(mesh_set_asset["path"])
    )
    if entity_path is None:
        return None
    entity_asset = repository.catalog.find_asset(
        entity_path,
        (str(mesh_set_asset["archive_path"]),),
    )
    if (
        entity_asset is None
        or str(entity_asset["asset_type"]) != "<non-resource>"
    ):
        return None
    _, raw = repository.read_asset(
        int(entity_asset["id"]),
        max_size=256 << 20,
    )
    assembly = modular_assembly(
        parse_dbx(raw),
        str(entity_asset["path"]),
    )
    if assembly is None or assembly.kind != "VEHICLE":
        return None
    main_component = next(
        (
            component
            for component in assembly.components
            if component.role == "VEHICLE"
        ),
        None,
    )
    if (
        main_component is None
        or main_component.mesh_set_path.casefold()
        != str(mesh_set_asset["path"]).casefold()
    ):
        return None
    return entity_asset, assembly


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
    diagnostic_count = len(material_context.diagnostics)
    diagnostics_text = _create_diagnostics_text(
        context,
        settings,
        (
            f"BC2_{Path(mesh_set_asset['path']).stem}_"
            "Material_Diagnostics.json"
        ),
        {
            "summary": material_summary,
            "issues": material_context.diagnostics,
        },
    )
    root_collection["bc2_diagnostic_count"] = diagnostic_count
    if diagnostics_text is not None:
        root_collection["bc2_material_diagnostics_text"] = (
            diagnostics_text.name
        )
    root_collection["bc2_resolved_material_count"] = material_summary[
        "resolved_materials"
    ]
    root_collection["bc2_source_surface_count"] = material_summary[
        "source_surfaces"
    ]
    root_collection["bc2_source_image_count"] = material_summary[
        "source_images"
    ]
    if material_summary["mesh_reference"]:
        root_collection["bc2_mesh_reference"] = material_summary[
            "mesh_reference"
        ]
    if material_summary["shader_set_reference"]:
        root_collection["bc2_shader_set_reference"] = material_summary[
            "shader_set_reference"
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
    root_collection["bc2_track_component_count"] = sum(
        component.role == "TRACK"
        for component in assembly.components
    )
    components_collection = _new_child_collection(
        root_collection,
        "Components",
    )

    armature_obj = None
    skeleton = None
    skeleton_asset = None
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
        if component.source_type is not None:
            component_collection["bc2_component_source_type"] = (
                component.source_type
            )
        if component.source_guid is not None:
            component_collection["bc2_component_source_guid"] = (
                component.source_guid
            )
        if component.skeleton_reference is not None:
            component_collection["bc2_component_skeleton_path"] = (
                _reference_resource_path(component.skeleton_reference)
            )
        if component.transform is not None:
            component_collection["bc2_component_transform"] = list(
                component.transform.values
            )
        if component.random_group is not None:
            component_collection["bc2_random_group"] = component.random_group

        component_skeleton = skeleton
        component_skeleton_asset = skeleton_asset
        component_armature = None
        if (
            component.skeleton_reference is not None
            and str(mesh_set_asset["platform"]) == "pc"
        ):
            component_skeleton_asset, component_skeleton = (
                _read_referenced_skeleton(
                    repository,
                    mesh_set_asset,
                    component.skeleton_reference,
                )
            )
            rig_collection = _new_child_collection(
                component_collection,
                "Rig",
            )
            component_armature = _create_armature_object(
                context,
                component_skeleton,
                component_skeleton_asset,
                settings,
                collection=rig_collection,
            )
            component_armature["bc2_assembly_path"] = assembly.source_path
            component_armature["bc2_component_path"] = (
                component.mesh_set_path
            )
            component_armature["bc2_component_role"] = component.role
            if component.source_guid is not None:
                component_armature["bc2_component_source_guid"] = (
                    component.source_guid
                )
            if component.transform is not None:
                component_armature["bc2_component_transform"] = list(
                    component.transform.values
                )

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
                tuple(bone.name for bone in component_skeleton.bones)
                if component_skeleton is not None
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
            if component.source_guid is not None:
                obj["bc2_component_source_guid"] = component.source_guid
            if component.transform is not None:
                obj["bc2_component_transform"] = list(
                    component.transform.values
                )
        binding_armature = component_armature or armature_obj
        if binding_armature is not None:
            _bind_meshes_to_armature(objects, binding_armature)
        if component.transform is not None:
            component_matrix = Matrix(
                component.transform.blender_matrix(
                    axis_conversion=settings.axis_conversion,
                    unit_scale=settings.unit_scale,
                )
            )
            if component_armature is not None:
                component_armature.matrix_world = component_matrix
            elif armature_obj is None:
                for obj in objects:
                    obj.matrix_world = component_matrix @ obj.matrix_world
        resolved_material_count += summary["resolved_materials"]
        component_summaries.append(
            {
                "name": component.name,
                "role": component.role,
                "path": component.mesh_set_path,
                "skeleton": (
                    _reference_resource_path(component.skeleton_reference)
                    if component.skeleton_reference is not None
                    else None
                ),
                "translation": (
                    component.transform.translation
                    if component.transform is not None
                    else None
                ),
                **summary,
            }
        )
        imported_components.append(
            _ImportedAssemblyComponent(
                definition=component,
                collection=component_collection,
                objects=tuple(objects),
                mesh_type=parsed.mesh_type,
                armature=component_armature,
            )
        )

    diagnostic_count = len(material_context.diagnostics)
    diagnostics_text = _create_diagnostics_text(
        context,
        settings,
        f"BC2_{assembly.name}_Assembly_Diagnostics.json",
        {
            "assembly": {
                "name": assembly.name,
                "kind": assembly.kind,
                "path": assembly.source_path,
            },
            "components": component_summaries,
            "issues": material_context.diagnostics,
        },
    )
    root_collection["bc2_diagnostic_count"] = diagnostic_count
    if diagnostics_text is not None:
        root_collection["bc2_material_diagnostics_text"] = diagnostics_text.name
    root_collection["bc2_resolved_material_count"] = (
        resolved_material_count
    )
    if armature_obj is not None:
        armature_obj["bc2_diagnostic_count"] = diagnostic_count
        if diagnostics_text is not None:
            armature_obj["bc2_material_diagnostics_text"] = diagnostics_text.name
        armature_obj["bc2_resolved_material_count"] = (
            resolved_material_count
        )
        context.view_layer.objects.active = armature_obj
        armature_obj.select_set(True)
    related_animations = None
    if (
        armature_obj is not None
        and skeleton is not None
        and skeleton_asset is not None
    ):
        related_animations = _import_related_animations(
            context,
            repository,
            loaded[0][1],
            skeleton_asset,
            skeleton,
            armature_obj,
            settings,
            (
                _CharacterAnimationSource(
                    links=assembly.animation_links,
                    archive_path=str(assembly_asset["archive_path"]),
                    descriptor_path=str(assembly_asset["path"]),
                )
                if assembly.animation_links is not None
                else None
            ),
        )
    return _ImportedAssembly(
        definition=assembly,
        root_collection=root_collection,
        armature=armature_obj,
        components=tuple(imported_components),
        diagnostics_text=diagnostics_text,
        related_animations=related_animations,
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
    additive_base: GrannyAnimation | None = None,
):
    source_names = {bone.name for bone in skeleton.bones}
    armature_names = {bone.name for bone in armature_obj.data.bones}
    additive = bool(animation.additive_bone_indices)
    if additive and additive_base is None:
        raise BC2Error(
            f"Additive animation {animation.name!r} requires an authored "
            "base pose; import it through its character mesh or soldier "
            "descriptor"
        )
    if additive_base is not None and additive_base.additive_bone_indices:
        raise BC2Error(
            f"Animation base {additive_base.name!r} is itself additive"
        )

    animation_tracks = {track.name: track for track in animation.tracks}
    if len(animation_tracks) != len(animation.tracks):
        raise BC2Error(
            f"Animation {animation.name!r} contains duplicate track names"
        )
    base_tracks = (
        {track.name: track for track in additive_base.tracks}
        if additive_base is not None
        else {}
    )
    if (
        additive_base is not None
        and len(base_tracks) != len(additive_base.tracks)
    ):
        raise BC2Error(
            f"Animation base {additive_base.name!r} contains duplicate "
            "track names"
        )
    action_tracks = (
        additive_base.tracks
        if additive_base is not None
        else animation.tracks
    )
    missing_source = [
        track.name for track in action_tracks
        if track.name not in source_names
    ]
    missing_armature = [
        track.name for track in action_tracks
        if track.name not in armature_names
    ]
    if missing_source or missing_armature:
        missing = sorted(set(missing_source + missing_armature))
        raise BC2Error(
            "Animation tracks do not match the selected skeleton: "
            + ", ".join(missing[:8])
        )

    if additive:
        invalid_indices = tuple(
            index
            for index in animation.additive_bone_indices
            if index >= len(skeleton.bones)
        )
        if invalid_indices:
            raise BC2Error(
                "Additive animation bone indices exceed the selected "
                f"skeleton: {invalid_indices[:8]}"
            )
        indexed_names = {
            skeleton.bones[index].name
            for index in animation.additive_bone_indices
        }
        track_names = {track.name for track in animation.tracks}
        if indexed_names != track_names:
            missing = sorted(indexed_names - track_names)
            unexpected = sorted(track_names - indexed_names)
            details = []
            if missing:
                details.append("missing " + ", ".join(missing[:8]))
            if unexpected:
                details.append("unexpected " + ", ".join(unexpected[:8]))
            raise BC2Error(
                "Additive animation bone-index metadata does not match its "
                "transform tracks: " + "; ".join(details)
            )
        missing_base_tracks = sorted(track_names - set(base_tracks))
        if missing_base_tracks:
            raise BC2Error(
                f"Animation base {additive_base.name!r} does not define "
                "the additive tracks: "
                + ", ".join(missing_base_tracks[:8])
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
    for track in action_tracks:
        pose_bone = armature_obj.pose.bones[track.name]
        pose_bone.rotation_mode = "QUATERNION"
        locations = []
        rotations = []
        scales = []
        previous_rotation = None
        for time in times:
            if additive:
                base_track = base_tracks[track.name]
                base_local = _source_transform_matrix(
                    base_track.translation.evaluate(0.0),
                    base_track.rotation_at(0.0),
                    base_track.scale_shear.evaluate(0.0),
                )
                delta_track = animation_tracks.get(track.name)
                source_local = (
                    base_local
                    if delta_track is None
                    else base_local
                    @ _source_transform_matrix(
                        delta_track.translation.evaluate(time),
                        delta_track.rotation_at(time),
                        delta_track.scale_shear.evaluate(time),
                    )
                )
            else:
                source_local = _source_transform_matrix(
                    track.translation.evaluate(time),
                    track.rotation_at(time),
                    track.scale_shear.evaluate(time),
                )
            animated_local = _convert_skeleton_matrix(
                source_local,
                settings,
            )
            basis = (
                rest_by_name[track.name].inverted_safe()
                @ animated_local
            )
            location, rotation, scale = basis.decompose()
            reconstructed = Matrix.LocRotScale(location, rotation, scale)
            residual = max(
                abs(basis[row][column] - reconstructed[row][column])
                for row in range(4)
                for column in range(4)
            )
            if residual >= 1e-5:
                raise BC2Error(
                    f"Animation track {track.name!r} contains scale/shear "
                    "that Blender pose channels cannot represent"
                )
            if (
                previous_rotation is not None
                and rotation.dot(previous_rotation) < 0.0
            ):
                rotation.negate()
            locations.append(tuple(location))
            rotations.append(tuple(rotation))
            scales.append(tuple(scale))
            previous_rotation = rotation.copy()
        location_path = pose_bone.path_from_id("location")
        rotation_path = pose_bone.path_from_id("rotation_quaternion")
        scale_path = pose_bone.path_from_id("scale")
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
        for axis in range(3):
            _add_action_curve(
                channelbag,
                scale_path,
                axis,
                track.name,
                frames,
                tuple(value[axis] for value in scales),
            )

    action["bc2_duration_seconds"] = animation.duration
    action["bc2_time_step_seconds"] = animation.time_step
    action["bc2_source_fps"] = source_fps
    action["bc2_track_count"] = len(animation.tracks)
    action["bc2_baked_track_count"] = len(action_tracks)
    action["bc2_additive"] = additive
    action["bc2_additive_baked"] = additive
    action["bc2_additive_bone_count"] = len(
        animation.additive_bone_indices
    )
    if additive_base is not None:
        action["bc2_additive_base_name"] = additive_base.name
    armature_obj.animation_data_create()
    armature_obj.animation_data.action = action
    context.scene.render.fps = source_fps
    context.scene.render.fps_base = 1.0
    context.scene.frame_start = 1
    context.scene.frame_end = sample_count
    context.scene.frame_set(1)
    return action


def _character_animation_sources(
    repository,
    resource_asset,
    preferred_archives: tuple[str, ...],
    known_source: _CharacterAnimationSource | None,
) -> tuple[tuple[_CharacterAnimationSource, ...], tuple[str, ...]]:
    if known_source is not None:
        return (known_source,), ()
    if str(resource_asset["platform"]).casefold() != "pc":
        return (), ()

    preferred = {path.casefold() for path in preferred_archives}
    candidates = tuple(
        row
        for row in repository.catalog.assets_with_prefix(
            "Objects/Soldiers/"
        )
        if str(row["path"]).casefold().endswith(".dbx")
        and str(row["archive_path"]).casefold() in preferred
    )
    if len(candidates) > _MAX_CHARACTER_ANIMATION_DESCRIPTORS:
        return (
            (),
            (
                f"The preferred archives contain {len(candidates):,} soldier "
                "descriptors; the automatic character-link limit is "
                f"{_MAX_CHARACTER_ANIMATION_DESCRIPTORS:,}",
            ),
        )

    issues = []
    by_archive = {}
    for candidate in candidates:
        by_archive.setdefault(str(candidate["archive_path"]), []).append(
            candidate
        )
    for archive_path, rows in by_archive.items():
        if len(rows) < 2:
            continue
        try:
            repository.materialize_archive_payload(archive_path)
        except (BC2Error, OSError, ValueError) as exc:
            issues.append(
                f"Could not cache {archive_path} for character animation "
                f"discovery: {exc}"
            )

    resource_partition = _resource_partition(
        str(resource_asset["path"])
    ).casefold().encode("utf-8")
    sources = []
    for candidate in candidates:
        try:
            _, raw = repository.read_asset(
                int(candidate["id"]),
                max_size=256 << 20,
            )
            if resource_partition not in raw.lower():
                continue
            links = character_animation_links(
                parse_dbx(raw),
                str(resource_asset["path"]),
            )
        except (BC2Error, OSError, ValueError) as exc:
            issues.append(f"{candidate['path']}: {exc}")
            continue
        if links is not None:
            sources.append(
                _CharacterAnimationSource(
                    links=links,
                    archive_path=str(candidate["archive_path"]),
                    descriptor_path=str(candidate["path"]),
                )
            )
    return tuple(sources), tuple(issues)


def _character_linked_animation_paths(
    repository,
    sources: tuple[_CharacterAnimationSource, ...],
    skeleton_path: str,
    preferred_archives: tuple[str, ...],
    include_core_gameplay: bool,
) -> tuple[tuple[str, ...], dict[str, str], tuple[str, ...]]:
    paths = {}
    tree_sources = {}
    shared_tree_sources = {}
    additive_base_paths = {}
    ambiguous_base_paths = set()
    issues = []
    for source in sources:
        for reference in source.links.animation_references:
            try:
                path = _reference_resource_path(reference)
            except BC2Error as exc:
                issues.append(f"{source.descriptor_path}: {exc}")
                continue
            paths.setdefault(path.casefold(), path)
        for reference in source.links.animation_tree_references:
            try:
                path = _reference_descriptor_path(reference)
            except BC2Error as exc:
                issues.append(f"{source.descriptor_path}: {exc}")
                continue
            tree_sources.setdefault(
                path.casefold(),
                (path, source.archive_path),
            )
        for reference in source.links.shared_animation_tree_references:
            try:
                path = _reference_descriptor_path(reference)
            except BC2Error as exc:
                issues.append(f"{source.descriptor_path}: {exc}")
                continue
            shared_tree_sources.setdefault(
                path.casefold(),
                (path, source.archive_path),
            )

    for path, source_archive in sorted(
        tree_sources.values(),
        key=lambda item: item[0].casefold(),
    ):
        preferred = tuple(
            dict.fromkeys((source_archive, *preferred_archives))
        )
        tree_asset = repository.catalog.find_asset(path, preferred)
        if tree_asset is None:
            issues.append(f"Character animation tree is not indexed: {path}")
            continue
        try:
            _, raw = repository.read_asset(
                int(tree_asset["id"]),
                max_size=256 << 20,
            )
            document = parse_dbx(raw)
            tree_skeleton = _skeleton_path_for_document(document, path)
            if tree_skeleton.casefold() != skeleton_path.casefold():
                issues.append(
                    f"{path}: tree skeleton {tree_skeleton} does not match "
                    f"{skeleton_path}"
                )
                continue
            references = animation_tree_clip_references(document)
            replacement_references = (
                animation_tree_replacement_references(document)
            )
        except (BC2Error, OSError, ValueError) as exc:
            issues.append(f"{path}: {exc}")
            continue
        clip_paths = []
        for reference in references:
            try:
                clip_path = _reference_resource_path(reference)
            except BC2Error as exc:
                issues.append(f"{path}: {exc}")
                continue
            paths.setdefault(clip_path.casefold(), clip_path)
            clip_paths.append(clip_path)
        replacement_paths = []
        for reference in replacement_references:
            try:
                replacement_paths.append(
                    _reference_resource_path(reference)
                )
            except BC2Error as exc:
                issues.append(f"{path}: {exc}")
        if len(replacement_paths) == 1:
            base_path = replacement_paths[0]
            base_key = base_path.casefold()
            for clip_path in clip_paths:
                clip_key = clip_path.casefold()
                if clip_key == base_key or clip_key in ambiguous_base_paths:
                    continue
                existing = additive_base_paths.get(clip_key)
                if (
                    existing is not None
                    and existing.casefold() != base_key
                ):
                    additive_base_paths.pop(clip_key, None)
                    ambiguous_base_paths.add(clip_key)
                    issues.append(
                        f"{clip_path}: character trees specify conflicting "
                        "replacement poses"
                    )
                else:
                    additive_base_paths[clip_key] = base_path
        elif len(replacement_paths) > 1:
            issues.append(
                f"{path}: multiple replacement poses make automatic "
                "additive composition ambiguous"
            )

    if include_core_gameplay:
        for path, source_archive in sorted(
            shared_tree_sources.values(),
            key=lambda item: item[0].casefold(),
        ):
            preferred = tuple(
                dict.fromkeys((source_archive, *preferred_archives))
            )
            shared_asset = repository.catalog.find_asset(path, preferred)
            if shared_asset is None:
                issues.append(
                    f"Shared character animation tree is not indexed: {path}"
                )
                continue
            try:
                _, raw = repository.read_asset(
                    int(shared_asset["id"]),
                    max_size=256 << 20,
                )
                document = parse_dbx(raw)
                tree_skeleton = _skeleton_path_for_document(document, path)
                if tree_skeleton.casefold() != skeleton_path.casefold():
                    issues.append(
                        f"{path}: tree skeleton {tree_skeleton} does not "
                        f"match {skeleton_path}"
                    )
                    continue
                base_set_references = animation_tree_base_set_references(
                    document
                )
            except (BC2Error, OSError, ValueError) as exc:
                issues.append(f"{path}: {exc}")
                continue

            rifle_trees = []
            for reference in base_set_references:
                try:
                    candidate_path = _reference_descriptor_path(reference)
                except BC2Error as exc:
                    issues.append(f"{path}: {exc}")
                    continue
                if (
                    PurePosixPath(candidate_path).stem.casefold()
                    == _CORE_GAMEPLAY_TREE_STEM
                ):
                    rifle_trees.append(candidate_path)
            if not rifle_trees:
                issues.append(
                    f"{path}: Shared BaseSetAnimTrees does not link the "
                    "verified rifle tree"
                )
                continue

            for rifle_path in rifle_trees:
                rifle_preferred = tuple(
                    dict.fromkeys(
                        (
                            str(shared_asset["archive_path"]),
                            *preferred,
                        )
                    )
                )
                rifle_asset = repository.catalog.find_asset(
                    rifle_path,
                    rifle_preferred,
                )
                if rifle_asset is None:
                    issues.append(
                        "Core rifle animation tree is not indexed: "
                        f"{rifle_path}"
                    )
                    continue
                try:
                    _, raw = repository.read_asset(
                        int(rifle_asset["id"]),
                        max_size=256 << 20,
                    )
                    rifle_document = parse_dbx(raw)
                    rifle_skeleton = _skeleton_path_for_document(
                        rifle_document,
                        rifle_path,
                    )
                    if rifle_skeleton.casefold() != skeleton_path.casefold():
                        issues.append(
                            f"{rifle_path}: tree skeleton {rifle_skeleton} "
                            f"does not match {skeleton_path}"
                        )
                        continue
                    rifle_references = animation_tree_clip_references(
                        rifle_document
                    )
                except (BC2Error, OSError, ValueError) as exc:
                    issues.append(f"{rifle_path}: {exc}")
                    continue
                rifle_paths = {}
                for reference in rifle_references:
                    try:
                        clip_path = _reference_resource_path(reference)
                    except BC2Error as exc:
                        issues.append(f"{rifle_path}: {exc}")
                        continue
                    rifle_paths.setdefault(clip_path.casefold(), clip_path)
                for expected_path in _CORE_GAMEPLAY_ANIMATION_PATHS:
                    clip_path = rifle_paths.get(expected_path)
                    if clip_path is None:
                        issues.append(
                            f"{rifle_path}: core gameplay clip is not "
                            f"listed: {expected_path}"
                        )
                        continue
                    paths.setdefault(clip_path.casefold(), clip_path)
    return (
        tuple(sorted(paths.values(), key=str.casefold)),
        additive_base_paths,
        tuple(issues),
    )


def _related_animation_assets(
    repository,
    resource_asset,
    skeleton_asset,
    preferred_archives: tuple[str, ...],
    include_core_gameplay: bool,
    known_source: _CharacterAnimationSource | None = None,
) -> tuple[tuple[_RelatedAnimationAsset, ...], tuple[str, ...], int, int]:
    skeleton_path = str(skeleton_asset["path"])
    folders = tuple(
        sorted(
            {
                PurePosixPath(str(resource_asset["path"])).parent.as_posix(),
                PurePosixPath(skeleton_path).parent.as_posix(),
            },
            key=str.casefold,
        )
    )
    rows = tuple(
        row
        for folder in folders
        for row in repository.catalog.assets_with_prefix(
            folder.rstrip("/") + "/",
            "GrannyAnimation",
        )
    )
    folded_folders = {folder.casefold() for folder in folders}
    folder_paths = {
        str(row["path"]).casefold(): str(row["path"])
        for row in rows
        if PurePosixPath(str(row["path"])).parent.as_posix().casefold()
        in folded_folders
    }
    sources, source_issues = _character_animation_sources(
        repository,
        resource_asset,
        preferred_archives,
        known_source,
    )
    graph_paths, additive_base_paths, graph_issues = (
        _character_linked_animation_paths(
            repository,
            sources,
            skeleton_path,
            preferred_archives,
            include_core_gameplay,
        )
    )
    paths = dict(folder_paths)
    for path in graph_paths:
        paths.setdefault(path.casefold(), path)
    ordered_paths = tuple(sorted(paths.values(), key=str.casefold))
    indexed_count, total_count = repository.catalog.archive_index_counts()
    issues = [*source_issues, *graph_issues]
    if len(ordered_paths) > _MAX_RELATED_ANIMATIONS:
        return (
            (),
            (
                *issues,
                f"Related discovery found {len(ordered_paths):,} indexed "
                "animation paths; "
                f"the automatic limit is {_MAX_RELATED_ANIMATIONS:,}",
            ),
            indexed_count,
            total_count,
        )

    assets = []
    validated_base_assets = {}
    graph_path_keys = {path.casefold() for path in graph_paths}
    for path in ordered_paths:
        asset = repository.catalog.find_asset(path, preferred_archives)
        if asset is None:
            issues.append(f"Indexed related animation disappeared: {path}")
            continue
        try:
            referenced_path = _skeleton_path_for_resource(repository, asset)
        except (BC2Error, OSError, ValueError) as exc:
            issues.append(f"{path}: {exc}")
            continue
        if referenced_path.casefold() != skeleton_path.casefold():
            if path.casefold() in graph_path_keys:
                issues.append(
                    f"{path}: clip skeleton {referenced_path} does not match "
                    f"{skeleton_path}"
                )
            continue

        additive_base_asset = None
        base_path = additive_base_paths.get(path.casefold())
        if base_path is not None:
            base_key = base_path.casefold()
            if base_key not in validated_base_assets:
                candidate = repository.catalog.find_asset(
                    base_path,
                    preferred_archives,
                )
                if candidate is None:
                    issues.append(
                        f"{path}: additive base pose is not indexed: "
                        f"{base_path}"
                    )
                    validated_base_assets[base_key] = None
                else:
                    try:
                        base_skeleton_path = _skeleton_path_for_resource(
                            repository,
                            candidate,
                        )
                    except (BC2Error, OSError, ValueError) as exc:
                        issues.append(f"{base_path}: {exc}")
                        validated_base_assets[base_key] = None
                    else:
                        if (
                            base_skeleton_path.casefold()
                            != skeleton_path.casefold()
                        ):
                            issues.append(
                                f"{base_path}: base-pose skeleton "
                                f"{base_skeleton_path} does not match "
                                f"{skeleton_path}"
                            )
                            validated_base_assets[base_key] = None
                        else:
                            validated_base_assets[base_key] = candidate
            additive_base_asset = validated_base_assets[base_key]
            if additive_base_asset is None:
                continue
        assets.append(
            _RelatedAnimationAsset(
                asset=asset,
                additive_base_asset=additive_base_asset,
            )
        )
    return tuple(assets), tuple(issues), indexed_count, total_count


def _create_animation_action_for_resource(
    context,
    resource_asset,
    animation: GrannyAnimation,
    armature_obj,
    skeleton: GrannySkeleton,
    settings: BC2Settings,
    additive_base: GrannyAnimation | None = None,
    additive_base_asset=None,
):
    action = _create_animation_action(
        context,
        armature_obj,
        skeleton,
        animation,
        settings,
        additive_base,
    )
    action["bc2_asset_path"] = resource_asset["path"]
    action["bc2_archive_path"] = resource_asset["archive_path"]
    action["bc2_source_additive"] = bool(
        animation.additive_bone_indices
    )
    if additive_base_asset is not None:
        action["bc2_baked_base_asset_path"] = additive_base_asset["path"]
        action["bc2_baked_base_archive_path"] = additive_base_asset[
            "archive_path"
        ]
    return action


def _create_animation_action_from_resource(
    context,
    resource_asset,
    raw: bytes,
    armature_obj,
    skeleton: GrannySkeleton,
    settings: BC2Settings,
):
    animation = parse_granny_animation(
        raw,
        resource_asset["platform"],
        Path(resource_asset["path"]).stem,
    )
    return _create_animation_action_for_resource(
        context,
        resource_asset,
        animation,
        armature_obj,
        skeleton,
        settings,
    )


def _import_related_animations(
    context,
    repository,
    resource_asset,
    skeleton_asset,
    skeleton: GrannySkeleton,
    armature_obj,
    settings: BC2Settings,
    known_source: _CharacterAnimationSource | None = None,
) -> _RelatedAnimationImport:
    if not settings.import_related_animations:
        armature_obj["bc2_related_animation_count"] = 0
        return _RelatedAnimationImport(enabled=False)

    preferred_archives = tuple(
        dict.fromkeys(
            (
                str(resource_asset["archive_path"]),
                str(skeleton_asset["archive_path"]),
                *(
                    (known_source.archive_path,)
                    if known_source is not None
                    else ()
                ),
            )
        )
    )
    assets, discovery_issues, indexed_count, total_count = (
        _related_animation_assets(
            repository,
            resource_asset,
            skeleton_asset,
            preferred_archives,
            settings.import_core_gameplay_animations,
            known_source,
        )
    )
    actions = []
    issues = list(discovery_issues)
    parsed_animations = {}
    candidate_assets = {
        str(asset["path"]).casefold(): asset
        for related_asset in assets
        for asset in (
            related_asset.asset,
            related_asset.additive_base_asset,
        )
        if asset is not None
    }
    for key, asset in candidate_assets.items():
        try:
            resource, raw = repository.read_asset(
                int(asset["id"]),
                max_size=256 << 20,
            )
            parsed_animations[key] = (
                resource,
                parse_granny_animation(
                    raw,
                    resource["platform"],
                    Path(resource["path"]).stem,
                ),
            )
        except (BC2Error, OSError, ValueError) as exc:
            issues.append(f"{asset['path']}: {exc}")

    for related_asset in assets:
        asset = related_asset.asset
        parsed = parsed_animations.get(str(asset["path"]).casefold())
        if parsed is None:
            continue
        resource, animation = parsed
        additive_base = None
        additive_base_asset = None
        if (
            animation.additive_bone_indices
            and related_asset.additive_base_asset is not None
        ):
            base_key = str(
                related_asset.additive_base_asset["path"]
            ).casefold()
            parsed_base = parsed_animations.get(base_key)
            if parsed_base is None:
                continue
            additive_base_asset, additive_base = parsed_base
        try:
            actions.append(
                _create_animation_action_for_resource(
                    context,
                    resource,
                    animation,
                    armature_obj,
                    skeleton,
                    settings,
                    additive_base,
                    additive_base_asset,
                )
            )
        except (BC2Error, OSError, ValueError) as exc:
            issues.append(f"{asset['path']}: {exc}")

    armature_obj["bc2_related_animation_count"] = len(actions)
    armature_obj["bc2_related_animation_catalog_complete"] = (
        indexed_count == total_count
    )
    armature_obj["bc2_related_animation_issue_count"] = len(issues)
    return _RelatedAnimationImport(
        enabled=True,
        actions=tuple(actions),
        issues=tuple(issues),
        indexed_archive_count=indexed_count,
        total_archive_count=total_count,
    )


def _import_granny_animation(
    context,
    repository,
    resource_asset,
    raw: bytes,
    settings: BC2Settings,
):
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
    action = _create_animation_action_from_resource(
        context,
        resource_asset,
        raw,
        armature_obj,
        skeleton,
        settings,
    )
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
    mesh["bc2_uv_sources"] = json.dumps(
        [subset.uv_source for subset in parsed.subsets],
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
    dimension: int | None = None,
) -> str:
    stem = PurePosixPath(path).name.casefold()
    if stem.endswith(".res"):
        stem = stem[:-4]
    if dimension in {9, 10}:
        return "ENVIRONMENT"
    if stem.startswith("envmap_") or "environmentmap" in stem:
        return "ENVIRONMENT"
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
    if "decal" in stem or stem.startswith("symbols_"):
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
    ) or stem.endswith("_oc"):
        return "OCCLUSION"
    if stem.startswith("dt_") or stem.endswith("_dt") or "detail" in stem:
        return "DETAIL"
    if "noise" in stem:
        return "NOISE"
    if stem.endswith(("_rough", "_roughness")):
        return "ROUGHNESS"
    if stem.endswith(("_s", "_sp", "_spec", "_specular")):
        return "SPECULAR"
    if stem.startswith("rgb_") or stem.endswith(
        (
            "_d",
            "_diff",
            "_diffuse",
            "_albedo",
            "_color",
            "_c",
            "_cm",
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
        _texture_role(
            binding.path,
            binding.parameters,
            binding.dimension,
        )
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
        "objects/vehicles/air/mi28/shaders/main": "MI28_MAIN",
        "objects/vehicles/air/mi28/shaders/mainmetal": (
            "MI28_MAIN_METAL"
        ),
        "objects/vehicles/air/mi28/shaders/glass": "MI28_GLASS",
        "objects/weapons/stationary/minigun/shaders/main": "MINIGUN_MAIN",
    }.get(partition)


def _is_vehicle_glass_surface(surface: SurfaceShader) -> bool:
    identity = (
        f"{surface.name}/{surface.reference}/{surface.database_key}"
        .replace("\\", "/")
        .casefold()
    )
    surface_name = _normalized_surface_name(surface.name)
    return "/vehicles/" in identity and (
        "glass" in identity
        or "window" in surface_name
        or "windshield" in surface_name
        or "windscreen" in surface_name
    )


def _surface_binding_roles(surface: SurfaceShader, bindings):
    roles = list(_binding_roles(bindings))
    family = _verified_surface_family(surface)
    if _is_vehicle_glass_surface(surface):
        for index, binding in enumerate(bindings):
            normalized_path = binding.path.replace("\\", "/").casefold()
            if binding.index >= 256 or "/textures/sky/" in normalized_path:
                roles[index] = "ENVIRONMENT"
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
    elif family in {"MI28_MAIN", "MI28_MAIN_METAL"}:
        first_index = 1 if family == "MI28_MAIN" else 2
        verified_roles = {
            first_index: "NORMAL",
            first_index + 1: "COLOR",
            first_index + 2: "COLOR",
            first_index + 3: "MASK",
            first_index + 4: "DETAIL",
            first_index + 5: "MASK",
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
    if family in {"MI28_MAIN", "MI28_MAIN_METAL"}:
        first_index = 1 if family == "MI28_MAIN" else 2
        verified_uv_maps = {
            first_index: "UVMap",
            first_index + 1: "UV1",
            first_index + 2: "UV2",
            first_index + 3: "UV3",
            first_index + 4: "UVMap",
            first_index + 5: "UV1",
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


def _shader_variant_for_subset(
    shader_material,
    vertex_stride: int | None,
    uv_offsets: tuple[int, ...],
):
    if vertex_stride is None or not uv_offsets:
        return None
    candidates = shader_material.compatible_variants(vertex_stride)
    if not candidates:
        return None
    available = set(uv_offsets)
    return max(
        candidates,
        key=lambda variant: (
            sum(
                route.uv_offset in available
                for route in variant.texture_routes
            ),
            sum(
                offset in available
                for offset in (
                    variant.layout.uv_offsets
                    if variant.layout is not None
                    else ()
                )
            ),
            len(variant.texture_routes),
        ),
    )


def _shader_uv_offsets_by_mesh(
    definitions,
    shader_database: ShaderDatabase | None,
):
    if shader_database is None:
        return {}
    offsets_by_mesh = {}
    for definition in definitions:
        by_stride = offsets_by_mesh.setdefault(
            definition.mesh_data_path.casefold(),
            {},
        )
        for surface in definition.surfaces:
            shader_material, _, _ = _find_shader_material(
                shader_database,
                surface.database_key,
                surface.shader_partition,
            )
            if shader_material is None:
                continue
            for variant in shader_material.variants:
                if variant.layout is None:
                    continue
                by_stride.setdefault(
                    variant.layout.stride,
                    set(),
                ).update(variant.layout.uv_offsets)
    return {
        mesh_path: {
            stride: tuple(sorted(offsets))
            for stride, offsets in by_stride.items()
        }
        for mesh_path, by_stride in offsets_by_mesh.items()
    }


def _shader_binding_route(variant, binding: ShaderTextureBinding):
    if variant is None:
        return None
    routes = tuple(
        route
        for route in variant.texture_routes
        if route.texture_index == binding.slot
    )
    if not routes:
        return None
    identities = {
        (route.uv_offset, route.scale, route.offset)
        for route in routes
    }
    return routes[0] if len(identities) == 1 else None


def _uv_map_for_offset(
    uv_offsets: tuple[int, ...],
    offset: int,
) -> str | None:
    try:
        index = uv_offsets.index(offset)
    except ValueError:
        return None
    return "UVMap" if index == 0 else f"UV{index}"


def _surface_binding_uv_scale(
    surface: SurfaceShader,
    binding: ShaderTextureBinding,
) -> tuple[float, float]:
    family = _verified_surface_family(surface)
    if family == "AH60_MAIN" and binding.index == 5:
        return (15.0, 15.0)
    if (
        family == "MI28_MAIN"
        and binding.index == 6
        or family == "MI28_MAIN_METAL"
        and binding.index == 7
    ):
        return (10.0, 10.0)
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
    decals = _unique_role_nodes(texture_nodes, "DECAL")
    occlusion = _unique_role_nodes(texture_nodes, "OCCLUSION")
    if colors:
        color_output = colors[0][1].outputs["Color"]
        color_node = colors[0][1]
        model = "reflected primary color"
    elif details:
        color_output = details[0][1].outputs["Color"]
        color_node = details[0][1]
        model = "reflected detail color"
    elif len(decals) == 1 and "atlas" not in PurePosixPath(
        decals[0][0].path
    ).name.casefold():
        color_output = decals[0][1].outputs["Color"]
        color_node = decals[0][1]
        model = "sole reflected non-atlas decal color"
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


def _configure_verified_vehicle_main(
    material,
    nodes,
    links,
    shader,
    texture_nodes,
    *,
    node_prefix: str,
    family_label: str,
    primary_index: int,
    symbols_index: int,
    dirt_index: int,
    detail_index: int,
    dirt_channel: str,
    dirt_scale: float,
    dirt_bias: float,
    detail_factor: float,
    detail_label: str,
    lighting_repeat: float,
    color_model: str,
    evidence: str,
) -> bool:
    primary = _binding_texture_node(texture_nodes, primary_index)
    if primary is None:
        return False
    color_output = primary.outputs["Color"]

    symbols = _binding_texture_node(texture_nodes, symbols_index)
    if symbols is not None:
        symbols_mix = nodes.new("ShaderNodeMixRGB")
        symbols_mix.name = f"BC2 {node_prefix} symbols"
        symbols_mix.label = "Main color to symbols by alpha"
        symbols_mix.blend_type = "MIX"
        symbols_mix.location = (-20, 180)
        links.new(symbols.outputs["Alpha"], symbols_mix.inputs["Fac"])
        links.new(color_output, symbols_mix.inputs["Color1"])
        links.new(symbols.outputs["Color"], symbols_mix.inputs["Color2"])
        color_output = symbols_mix.outputs["Color"]

    dirt = _binding_texture_node(texture_nodes, dirt_index)
    if dirt is not None:
        channels = nodes.new("ShaderNodeSeparateColor")
        channels.name = f"BC2 {node_prefix} dirt channels"
        channels.label = f"DirtMaskRGB {dirt_channel.casefold()}"
        channels.location = (-220, -80)
        links.new(dirt.outputs["Color"], channels.inputs["Color"])
        dirt_scale_node = nodes.new("ShaderNodeMath")
        dirt_scale_node.name = f"BC2 {node_prefix} dirt scale"
        dirt_scale_node.label = (
            f"Dirt {dirt_channel.casefold()} x {dirt_scale:g}"
        )
        dirt_scale_node.operation = "MULTIPLY"
        dirt_scale_node.inputs[1].default_value = dirt_scale
        links.new(
            channels.outputs[dirt_channel],
            dirt_scale_node.inputs[0],
        )
        dirt_bias_node = nodes.new("ShaderNodeMath")
        dirt_bias_node.name = f"BC2 {node_prefix} dirt bias"
        dirt_bias_node.label = f"Dirt term + {dirt_bias:g}"
        dirt_bias_node.operation = "ADD"
        dirt_bias_node.inputs[1].default_value = dirt_bias
        links.new(dirt_scale_node.outputs[0], dirt_bias_node.inputs[0])
        dirt_add = nodes.new("ShaderNodeMixRGB")
        dirt_add.name = f"BC2 {node_prefix} dirt"
        dirt_add.label = "Add source dirt term"
        dirt_add.blend_type = "ADD"
        dirt_add.inputs["Fac"].default_value = 1.0
        links.new(color_output, dirt_add.inputs["Color1"])
        links.new(dirt_bias_node.outputs[0], dirt_add.inputs["Color2"])
        color_output = dirt_add.outputs["Color"]

    detail = _binding_texture_node(texture_nodes, detail_index)
    if detail is not None:
        detail_double = nodes.new("ShaderNodeMixRGB")
        detail_double.name = f"BC2 {node_prefix} detail double"
        detail_double.label = detail_label
        detail_double.blend_type = "MULTIPLY"
        detail_double.inputs["Fac"].default_value = 1.0
        detail_double.inputs["Color2"].default_value = (
            2.0,
            2.0,
            2.0,
            1.0,
        )
        links.new(detail.outputs["Color"], detail_double.inputs["Color1"])
        detail_mix = nodes.new("ShaderNodeMixRGB")
        detail_mix.name = f"BC2 {node_prefix} detail modulation"
        detail_mix.label = (
            f"Main x lerp(1, detail x 2, {detail_factor:g})"
        )
        detail_mix.blend_type = "MULTIPLY"
        detail_mix.inputs["Fac"].default_value = detail_factor
        links.new(color_output, detail_mix.inputs["Color1"])
        links.new(
            detail_double.outputs["Color"],
            detail_mix.inputs["Color2"],
        )
        color_output = detail_mix.outputs["Color"]

    links.new(color_output, shader.inputs["Base Color"])
    material["bc2_material_family"] = family_label
    material["bc2_color_model"] = color_model
    material["bc2_lighting_mask_model"] = (
        f"Msk_Generic retained at source UV x{lighting_repeat:g} for the "
        "outdoor-light term, which remains Blender-native"
    )
    material["bc2_material_evidence"] = evidence
    material["bc2_material_status"] = (
        f"resolved verified {family_label} graph"
    )
    return True


def _configure_vehicle_glass_material(
    material,
    nodes,
    links,
    shader,
    texture_nodes,
    *,
    family_label: str,
    color_model: str,
    evidence: str,
    status: str,
    opacity_scale: float = 1.0,
) -> bool:
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
    if dirt_node is None:
        dirt_node = next(
            (
                node
                for _binding, role, node in texture_nodes
                if role == "MASK"
            ),
            None,
        )
    if dirt_node is not None:
        channels = nodes.new("ShaderNodeSeparateColor")
        channels.name = "BC2 glass dirt channels"
        channels.label = "Source glass mask red"
        channels.location = (-180, -300)
        links.new(dirt_node.outputs["Color"], channels.inputs["Color"])
        add = nodes.new("ShaderNodeMath")
        add.name = "BC2 glass opacity"
        add.label = "Fresnel + source mask red"
        add.operation = "ADD"
        add.use_clamp = True
        add.location = (320, -240)
        links.new(fresnel.outputs["Fac"], add.inputs[0])
        links.new(channels.outputs["Red"], add.inputs[1])
        opacity = add.outputs[0]
    if opacity_scale != 1.0:
        scale = nodes.new("ShaderNodeMath")
        scale.name = "BC2 glass opacity scale"
        scale.label = f"Interior glass opacity x {opacity_scale:g}"
        scale.operation = "MULTIPLY"
        scale.inputs[1].default_value = opacity_scale
        scale.location = (500, -240)
        links.new(opacity, scale.inputs[0])
        opacity = scale.outputs[0]
    links.new(opacity, shader.inputs["Alpha"])
    material.surface_render_method = "DITHERED"
    material["bc2_material_family"] = family_label
    material["bc2_color_model"] = color_model
    material["bc2_material_evidence"] = evidence
    material["bc2_material_status"] = status
    return True


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
        return _configure_verified_vehicle_main(
            material,
            nodes,
            links,
            shader,
            texture_nodes,
            node_prefix="AH60 main",
            family_label="AH60 main",
            primary_index=1,
            symbols_index=2,
            dirt_index=3,
            detail_index=4,
            dirt_channel="Blue",
            dirt_scale=0.1,
            dirt_bias=0.045,
            detail_factor=0.482,
            detail_label="Ah60Blackhawk_D x 2",
            lighting_repeat=15.0,
            color_model=(
                "Ah60Blackhawk_C blended to Symbols_Us by alpha, "
                "DirtMaskRGB blue x0.1 + 0.045, then Ah60Blackhawk_D "
                "detail modulation x0.482"
            ),
            evidence=(
                "MP_004 vertex shaders 428/554/748/1310 and "
                "pixel shaders 1229/1792"
            ),
        )

    if family in {"MI28_MAIN", "MI28_MAIN_METAL"}:
        main_metal = family == "MI28_MAIN_METAL"
        first_index = 2 if main_metal else 1
        family_label = "MI28 main metal" if main_metal else "MI28 main"
        evidence = (
            "MP_005 vertex shaders 17/91/406/1417 and "
            "pixel shaders 513/762"
            if main_metal
            else "MP_005 vertex shaders 751/1049 and pixel shaders 3/1454"
        )
        return _configure_verified_vehicle_main(
            material,
            nodes,
            links,
            shader,
            texture_nodes,
            node_prefix=family_label,
            family_label=family_label,
            primary_index=first_index + 1,
            symbols_index=first_index + 2,
            dirt_index=first_index + 3,
            detail_index=first_index + 4,
            dirt_channel="Red",
            dirt_scale=0.3,
            dirt_bias=0.045,
            detail_factor=0.725,
            detail_label="Mi28Havoc_D x 2",
            lighting_repeat=10.0,
            color_model=(
                "Mi28Havoc_C blended to Symbols_Rus by alpha, "
                "DirtMaskRGB red x0.3 + 0.045, then Mi28Havoc_D "
                "detail modulation x0.725"
            ),
            evidence=evidence,
        )

    if family in {"AH60_GLASS", "MI28_GLASS"}:
        if family == "MI28_GLASS":
            return _configure_vehicle_glass_material(
                material,
                nodes,
                links,
                shader,
                texture_nodes,
                family_label="MI28 glass",
                color_model=(
                    "transparent reflective glass with view-angle opacity"
                ),
                evidence="MP_005 pixel shaders 1221, 1346, and 1655",
                status="resolved verified MI28 glass graph",
            )
        return _configure_vehicle_glass_material(
            material,
            nodes,
            links,
            shader,
            texture_nodes,
            family_label="AH60 glass",
            color_model=(
                "transparent reflective glass with Fresnel plus "
                "DirtMaskRGB red opacity"
            ),
            evidence="MP_004 pixel shaders 978, 1135, and 2110",
            status="resolved verified AH60 glass graph",
        )

    if _is_vehicle_glass_surface(surface):
        identity = (
            f"{surface.name}/{surface.reference}/{surface.database_key}"
            .replace("\\", "/")
            .casefold()
        )
        inside = "inside" in identity or "interior" in identity
        return _configure_vehicle_glass_material(
            material,
            nodes,
            links,
            shader,
            texture_nodes,
            family_label=(
                "vehicle interior glass" if inside else "vehicle glass"
            ),
            color_model=(
                "semantic transmissive vehicle glass with view-angle and "
                "source-mask opacity"
            ),
            evidence=(
                "shared PC retail vehicle glass shader behavior; exact "
                "reflection and distance response remain heuristic"
            ),
            status="resolved semantic vehicle glass graph",
            opacity_scale=0.1 if inside else 1.0,
        )

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
    shader_partition: str = "",
):
    lookup_keys = [key]
    prefix, separator, name = key.rpartition("/")
    if separator and name.casefold().startswith("cheap_"):
        lookup_keys.append(prefix + "/" + name[len("cheap_") :])
    for index, lookup_key in enumerate(lookup_keys):
        material = shader_database.material(lookup_key)
        if material is not None:
            return (
                material,
                material.key,
                "exact" if index == 0 else "cheap_alias",
            )
    partitions = [shader_partition]
    partition_prefix, partition_separator, partition_name = (
        shader_partition.rpartition("/")
    )
    if (
        partition_separator
        and partition_name.casefold().startswith("cheap_")
    ):
        partitions.append(
            partition_prefix
            + "/"
            + partition_name[len("cheap_") :]
        )
    for partition in partitions:
        matches = shader_database.materials_for_shader_partition(partition)
        if len(matches) == 1:
            return matches[0], matches[0].key, "shader_reference_suffix"
    return None, key, "missing"


def _shader_lookup_terms(
    surface: SurfaceShader,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    keys = [surface.database_key]
    prefix, separator, name = surface.database_key.rpartition("/")
    if separator and name.casefold().startswith("cheap_"):
        keys.append(prefix + "/" + name[len("cheap_") :])
    partitions = [surface.shader_partition]
    partition_prefix, partition_separator, partition_name = (
        surface.shader_partition.rpartition("/")
    )
    if (
        partition_separator
        and partition_name.casefold().startswith("cheap_")
    ):
        partitions.append(
            partition_prefix
            + "/"
            + partition_name[len("cheap_") :]
        )
    return tuple(keys), tuple(
        partition for partition in partitions if partition
    )


def _surface_material(
    repository,
    surface: SurfaceShader,
    shader_database: ShaderDatabase | None,
    preferred_archives,
    material_cache: dict[str, object],
    image_cache: dict[str, tuple[object, object]],
    diagnostics: list[dict[str, str]],
    import_textures: bool,
    *,
    vertex_stride: int | None = None,
    uv_offsets: tuple[int, ...] = (),
):
    declaration_key = (
        f"|{vertex_stride}|{','.join(str(value) for value in uv_offsets)}"
    )
    cache_key = (
        "surface|" + surface.database_key.casefold() + declaration_key
    )
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
        material["bc2_material_status"] = (
            "resolved source material without textures"
        )
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
        surface.shader_partition,
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
    shader_variant = (
        None
        if character_surface
        else _shader_variant_for_subset(
            shader_material,
            vertex_stride,
            uv_offsets,
        )
    )
    binding_routes = tuple(
        (
            None
            if character_surface
            else _shader_binding_route(shader_variant, binding)
        )
        for binding in shader_material.textures
    )
    if shader_variant is not None:
        material["bc2_shader_variant"] = json.dumps(
            {
                "table_index": shader_variant.table_index,
                "vertex_shader_index": shader_variant.vertex_shader_index,
                "pixel_shader_index": shader_variant.pixel_shader_index,
                "layout_stride": (
                    shader_variant.layout.stride
                    if shader_variant.layout is not None
                    else None
                ),
                "layout_uv_offsets": (
                    list(shader_variant.layout.uv_offsets)
                    if shader_variant.layout is not None
                    else []
                ),
                "route_error": shader_variant.route_error,
            },
            separators=(",", ":"),
        )
        if shader_variant.route_error:
            diagnostics.append(
                {
                    "code": "shader_uv_route_unsupported",
                    "reference": surface.database_key,
                    "message": shader_variant.route_error,
                }
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
                "dimension": binding.dimension,
                "route": (
                    {
                        "uv_offset": route.uv_offset,
                        "scale": list(route.scale),
                        "offset": list(route.offset),
                    }
                    if route is not None
                    else None
                ),
            }
            for binding, role, route in zip(
                shader_material.textures,
                roles,
                binding_routes,
            )
        ],
        separators=(",", ":"),
    )
    texture_nodes = []
    normal_node = None
    normal_format = None
    normal_path = None
    uv_nodes = {}
    for position, (binding, role, route) in enumerate(
        zip(shader_material.textures, roles, binding_routes)
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
        routed_uv_map = (
            _uv_map_for_offset(uv_offsets, route.uv_offset)
            if route is not None
            else None
        )
        if route is not None and routed_uv_map is None:
            diagnostics.append(
                {
                    "code": "shader_uv_route_unavailable",
                    "reference": binding.path,
                    "message": (
                        f"Shader routes t{binding.slot} to vertex byte "
                        f"{route.uv_offset}, which is not decoded on this "
                        "mesh subset"
                    ),
                }
            )
        uv_map = routed_uv_map or (
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
        route_scale = route.scale if route is not None else (None, None)
        if route is not None and any(
            value is None
            for value in (*route.scale, *route.offset)
        ):
            diagnostics.append(
                {
                    "code": "shader_uv_transform_unsupported",
                    "reference": binding.path,
                    "message": (
                        f"Shader route for t{binding.slot} uses a dynamic "
                        "UV transform; the physical source channel is "
                        "preserved and the existing transform fallback is "
                        "used"
                    ),
                }
            )
        if route is not None and all(
            value is not None for value in route_scale
        ):
            uv_scale = tuple(float(value) for value in route_scale)
        else:
            uv_scale = (
                _character_binding_uv_scale(surface, role)
                if character_surface
                else _surface_binding_uv_scale(surface, binding)
            )
        route_offset = route.offset if route is not None else (None, None)
        uv_offset = (
            tuple(float(value) for value in route_offset)
            if route is not None
            and all(value is not None for value in route_offset)
            else (0.0, 0.0)
        )
        vector_output = uv_node.outputs["UV"]
        if uv_scale != (1.0, 1.0):
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
            vector_output = scale_node.outputs["Vector"]
        if uv_offset != (0.0, 0.0):
            offset_node = nodes.new("ShaderNodeVectorMath")
            offset_node.name = (
                f"BC2 {role} UV +{uv_offset[0]:g},+{uv_offset[1]:g}"
            )
            offset_node.label = (
                f"{role} UV +{uv_offset[0]:g}, +{uv_offset[1]:g}"
            )
            offset_node.operation = "ADD"
            offset_node.inputs[1].default_value = (
                uv_offset[0],
                uv_offset[1],
                0.0,
            )
            links.new(vector_output, offset_node.inputs[0])
            vector_output = offset_node.outputs["Vector"]
        links.new(vector_output, node.inputs["Vector"])
        node["bc2_uv_map"] = uv_map
        node["bc2_uv_scale"] = uv_scale
        node["bc2_uv_offset"] = uv_offset
        node["bc2_uv_route_source"] = (
            "shader" if routed_uv_map is not None else "fallback"
        )
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
    import_textures = _loads_source_textures(settings)
    return _SourceMaterialContext(
        import_textures=import_textures,
        material_archives=tuple(preferred_archives),
        shader_database=None,
        shader_database_path=None,
        material_cache={},
        image_cache={},
        diagnostics=[],
        loaded_shader_assets=set(),
        material_sources={},
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
        tuple(dict.fromkeys((*first.layouts, *second.layouts))),
    )


def _extend_source_shader_database(
    repository,
    material_context: _SourceMaterialContext,
    shader_asset,
    surfaces: tuple[SurfaceShader, ...],
) -> bool:
    asset_id = int(shader_asset["id"])
    if asset_id in material_context.loaded_shader_assets:
        return False
    material_context.loaded_shader_assets.add(asset_id)
    lookup_keys = []
    shader_partitions = []
    for surface in surfaces:
        surface_keys, surface_partitions = _shader_lookup_terms(surface)
        lookup_keys.extend(surface_keys)
        shader_partitions.extend(surface_partitions)
    asset, database = repository.read_shader_database(
        asset_id,
        material_keys=tuple(dict.fromkeys(lookup_keys)),
        shader_partitions=tuple(dict.fromkeys(shader_partitions)),
    )
    archive_path = str(asset["archive_path"])
    shader_path = str(asset["path"])
    source = f"{archive_path}|{shader_path}"
    selected = []
    selected_keys = set()
    for surface in surfaces:
        material, resolved_key, _ = _find_shader_material(
            database,
            surface.database_key,
            surface.shader_partition,
        )
        if material is None:
            continue
        normalized_key = material.key.casefold()
        existing = (
            material_context.shader_database.material(material.key)
            if material_context.shader_database is not None
            else None
        )
        if existing is None and normalized_key not in selected_keys:
            selected.append(material)
            selected_keys.add(normalized_key)
            material_context.material_sources[normalized_key] = source
        elif existing is not None and existing != material:
            existing_source = material_context.material_sources.get(
                normalized_key,
                "another shader database",
            )
            material_context.diagnostics.append(
                {
                    "code": "conflicting_shader_material",
                    "reference": resolved_key,
                    "message": (
                        "Material key is defined differently by "
                        f"{existing_source} and {source}; the higher-ranked "
                        "source was retained"
                    ),
                }
            )
    if selected:
        material_context.shader_database = _merge_shader_databases(
            material_context.shader_database,
            ShaderDatabase(
                tuple(selected),
                database.trailing_bytes,
                database.layouts,
            ),
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
            source,
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
    return bool(selected)


def _catalog_shader_database_matches_surfaces(
    catalog,
    asset_id: int,
    surfaces: tuple[SurfaceShader, ...],
) -> bool:
    material_keys = tuple(
        key.replace("\\", "/").strip("/").casefold()
        for key in catalog.shader_database_material_keys(asset_id)
    )
    material_key_set = set(material_keys)
    for surface in surfaces:
        lookup_keys, partitions = _shader_lookup_terms(surface)
        if any(
            key.replace("\\", "/").strip("/").casefold()
            in material_key_set
            for key in lookup_keys
        ):
            return True
        for partition in partitions:
            normalized = (
                partition.replace("\\", "/").strip("/").casefold()
            )
            suffix = "/" + normalized
            if (
                sum(
                    key == normalized or key.endswith(suffix)
                    for key in material_keys
                )
                == 1
            ):
                return True
    return False


def _ensure_source_shader_materials(
    repository,
    material_context: _SourceMaterialContext,
    surfaces: tuple[SurfaceShader, ...],
    mesh_path: str | None = None,
    preferred_archives: tuple[str, ...] = (),
):
    shader_keys = []
    shader_partitions = []
    for surface in surfaces:
        lookup_keys, lookup_partitions = _shader_lookup_terms(surface)
        shader_keys.extend(lookup_keys)
        shader_partitions.extend(lookup_partitions)
    shader_keys = tuple(dict.fromkeys(shader_keys))
    shader_partitions = tuple(dict.fromkeys(shader_partitions))
    if (
        repository.kind != "PC_ISO"
        or not material_context.import_textures
        or not shader_keys
    ):
        return

    def unresolved():
        database = material_context.shader_database
        if database is None:
            return surfaces
        return tuple(
            surface
            for surface in surfaces
            if (
                _find_shader_material(
                    database,
                    surface.database_key,
                    surface.shader_partition,
                )[0]
                is None
            )
        )

    if not unresolved():
        return
    candidates = repository.shader_database_candidates(
        shader_keys,
        preferred_archives or material_context.material_archives,
        mesh_path,
        shader_partitions,
    )
    for shader_asset in candidates:
        remaining = unresolved()
        if not remaining:
            return
        if (
            bool(shader_asset["shader_scanned"])
            and not _catalog_shader_database_matches_surfaces(
                repository.catalog,
                int(shader_asset["id"]),
                remaining,
            )
        ):
            continue
        _extend_source_shader_database(
            repository,
            material_context,
            shader_asset,
            remaining,
        )


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
    if not _uses_source_materials(settings):
        materials = []
        for subset in parsed.subsets:
            material = bpy.data.materials.new(
                f"BC2 Placeholder {PurePosixPath(subset.name).name}"
            )
            material.diffuse_color = (0.8, 0.8, 0.8, 1.0)
            material["bc2_material_status"] = "placeholder material"
            materials.append(material)
        return (
            tuple(materials),
            (),
            {
                "mesh_reference": "",
                "shader_set_reference": "",
                "shader_database": "",
                "subsets": len(parsed.subsets),
                "source_surfaces": 0,
                "resolved_materials": 0,
                "source_images": 0,
                "diagnostics": 0,
            },
        )
    created_context = material_context is None
    material_context = material_context or _source_material_context(
        repository,
        preferred_archives,
        settings,
    )
    diagnostic_start = 0 if created_context else len(
        material_context.diagnostics
    )
    descriptor_path = (
        _resource_partition(str(mesh_set_asset["path"])) + ".dbx"
    )
    descriptor_asset = _mesh_set_descriptor_asset(
        repository,
        mesh_set_asset,
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
        definition.surfaces,
        str(mesh_set_asset["path"]),
        tuple(
            dict.fromkeys(
                (
                    str(mesh_set_asset["archive_path"]),
                    *preferred_archives,
                )
            )
        ),
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
                vertex_stride=subset.vertex_stride,
                uv_offsets=subset.uv_offsets,
            )
            material["bc2_subset_match"] = match
            source_key = str(
                material.get(
                    "bc2_resolved_shader_key",
                    surface.database_key,
                )
            ).casefold()
            source = material_context.material_sources.get(source_key)
            if source:
                material["bc2_shader_database_source"] = source
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
    "defaultmaterial": (
        "jungleground",
        "grasscloseup",
        "burnedground",
        "treeground",
        "drygrass",
        "grassgreen",
        "grass",
        "snowgrass",
        "snowrough",
        "snow",
        "dirt",
        "mountain",
        "cliff",
    ),
    "grass": ("grasscloseup", "grass"),
    "dirt": ("soil", "ground_mudstones", "flatdirt", "dirt"),
    "tarmac": (
        "asphalt",
        "tarmac",
        "pavement",
        "concrete",
        "cobble",
        "mudroad",
    ),
    "gravel": ("gravel", "cobble", "rubble"),
    "stone": ("cliffface", "cliff", "stone", "rock", "rubble"),
    "seabed": (
        "rubblesand",
        "seabed",
        "riverbed",
        "waterbed",
        "gravelsand",
    ),
    "forest": (
        "forest",
        "leaf",
        "treeground",
        "jungleground",
        "grass",
    ),
    "sand": ("sand", "dune"),
    "rock": ("cliffface", "cliff", "rock", "rubble", "mountain"),
    "snow": ("snowrough", "snowdunes", "snowgrass", "snow"),
    "water": ("water", "riverbed", "seabed"),
    "concrete": ("concrete", "cobble", "pavement", "asphalt"),
    "mud": ("mud", "soil", "dirt", "ground"),
    "jungle": ("jungleground", "jungle", "forest", "grass"),
    "field": ("field", "grass", "jungleground", "burnedground"),
}

_TERRAIN_LAYER_COLORS = {
    "defaultmaterial": (0.18, 0.28, 0.12, 1.0),
    "grass": (0.16, 0.32, 0.12, 1.0),
    "dirt": (0.24, 0.16, 0.09, 1.0),
    "tarmac": (0.10, 0.11, 0.11, 1.0),
    "gravel": (0.34, 0.31, 0.26, 1.0),
    "stone": (0.29, 0.30, 0.27, 1.0),
    "seabed": (0.25, 0.23, 0.16, 1.0),
    "forest": (0.10, 0.22, 0.08, 1.0),
    "sand": (0.42, 0.34, 0.21, 1.0),
    "rock": (0.28, 0.27, 0.24, 1.0),
    "snow": (0.72, 0.76, 0.78, 1.0),
    "water": (0.08, 0.18, 0.24, 1.0),
    "concrete": (0.31, 0.31, 0.29, 1.0),
    "mud": (0.18, 0.12, 0.07, 1.0),
    "jungle": (0.11, 0.27, 0.08, 1.0),
    "field": (0.28, 0.30, 0.10, 1.0),
}


def _terrain_shader_texture_paths(
    shader_database: ShaderDatabase | None,
    material_map_path: str,
) -> dict[str, int]:
    if shader_database is None:
        return {}
    terrain_name = PurePosixPath(material_map_path).parent.name.casefold()
    if not terrain_name:
        return {}
    prefix = f"terrains/{terrain_name}/{terrain_name}/"
    counts = {}
    for material in shader_database.materials:
        if not material.key.casefold().startswith(prefix):
            continue
        for binding in material.textures:
            role = _texture_role(
                binding.path,
                binding.parameters,
                binding.dimension,
            )
            if role in {
                "NORMAL",
                "MASK",
                "NOISE",
                "OCCLUSION",
                "ROUGHNESS",
                "SPECULAR",
                "DECAL",
                "ENVIRONMENT",
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
        (),
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
    *,
    import_textures: bool = True,
):
    material_map = parse_material_map(
        material_map_raw,
        platform=material_map_asset["platform"],
    )
    if not import_textures:
        material = _simple_material(
            f"BC2 {level_scene.name} Terrain",
            (0.22, 0.32, 0.16, 1.0),
            roughness=0.9,
        )
        material["bc2_material_status"] = (
            "resolved terrain material names without textures"
        )
        material["bc2_terrain_material_map"] = material_map_asset["path"]
        material["bc2_terrain_material_side"] = material_map.side
        material["bc2_terrain_material_endian"] = material_map.endian
        material["bc2_terrain_layers"] = json.dumps(
            [
                {"index": index, "name": name, "texture": ""}
                for index, name in enumerate(material_map.names)
            ],
            separators=(",", ":"),
        )
        return material, material_map

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
        str(material_map_asset["path"]),
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


def _create_diagnostics_text(context, settings, name: str, payload):
    if not _detailed_diagnostics_enabled(context, settings):
        return None
    text = bpy.data.texts.new(name)
    text.write(json.dumps(payload, indent=2, sort_keys=True))
    return text


def _create_level_scene(
    context,
    repository,
    level_scene,
    preferred_archives,
    settings: BC2Settings,
    progress=None,
):
    material_mode = _material_mode(settings)
    use_source_materials = material_mode != "PLACEHOLDER"
    load_source_textures = material_mode == "FULL"
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
    }
    if _detailed_diagnostics_enabled(context, settings):
            collections["diagnostics"] = _child_collection(
                root,
                "Diagnostics",
            )
    diagnostics = [
        {
            "code": issue.code,
            "message": issue.message,
            "reference": issue.reference or "",
        }
        for issue in level_scene.issues
    ]

    shader_database = None
    if load_source_textures and level_scene.shader_database_path:
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
    shader_uv_offsets = _shader_uv_offsets_by_mesh(
        level_scene.meshes,
        shader_database,
    )
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
                retain_pc_vehicle_uv_candidates=(
                    definition.mesh_data_path.casefold().startswith(
                        "objects/vehicles/"
                    )
                ),
                pc_declared_uv_offsets_by_stride=shader_uv_offsets.get(
                    geometry_key,
                ),
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

        assignments = (
            _surface_assignments(parsed, definition)
            if use_source_materials
            else tuple((None, "placeholder") for _ in parsed.subsets)
        )
        assignment_records = []
        for subset_index, (subset, assignment) in enumerate(
            zip(parsed.subsets, assignments)
        ):
            surface, match_method = assignment
            if not use_source_materials:
                placeholder_key = (
                    f"placeholder|{definition.key}|{subset_index}"
                )
                material = _simple_material(
                    f"BC2 Placeholder {subset.name}",
                    (0.8, 0.8, 0.8, 1.0),
                )
                material["bc2_material_status"] = "placeholder material"
                material_cache[placeholder_key] = material
                shader_key = ""
            elif surface is None:
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
                    load_source_textures,
                    vertex_stride=subset.vertex_stride,
                    uv_offsets=subset.uv_offsets,
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
        use_source_materials
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
            import_textures=load_source_textures,
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
            if not use_source_materials
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
    text = _create_diagnostics_text(
        context,
        settings,
        f"BC2_{level_scene.name}_Diagnostics.json",
        {
            "summary": summary,
            "issues": diagnostics,
        },
    )
    if text is not None:
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
    root["bc2_diagnostic_count"] = len(diagnostics)
    if text is not None:
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


def _report_import_result(
    operator,
    settings: BC2Settings,
    message: str,
    diagnostic_count: int = 0,
    warning: str = "",
):
    settings.diagnostic_count = diagnostic_count
    details = []
    if diagnostic_count:
        details.append(f"{diagnostic_count:,} diagnostic(s)")
    if warning:
        details.append(warning)
    if details:
        settings.status = f"{message}; " + "; ".join(details)
        operator.report({"WARNING"}, settings.status)
    else:
        settings.status = message
        operator.report({"INFO"}, message)


def _related_animation_warning(
    result: _RelatedAnimationImport,
) -> str:
    if not result.enabled:
        return ""
    warnings = []
    if result.issues:
        first = result.issues[0]
        suffix = (
            f" (+{len(result.issues) - 1:,} more)"
            if len(result.issues) > 1
            else ""
        )
        warnings.append(f"Related animation issue: {first}{suffix}")
    if not result.catalog_complete:
        unindexed = (
            result.total_archive_count - result.indexed_archive_count
        )
        warnings.append(
            f"{unindexed:,} archive(s) remain unindexed; related animation "
            "discovery may be incomplete (use Index All (Slow) for full coverage)"
        )
    return "; ".join(warnings)


def _import_modular_assembly_asset(
    operator,
    context,
    repository,
    assembly_asset,
    assembly: ModularAssembly,
    settings: BC2Settings,
):
    imported = _create_modular_assembly(
        context,
        repository,
        assembly_asset,
        assembly,
        settings,
    )
    object_count = sum(
        len(component.objects)
        for component in imported.components
    )
    related_animations = imported.related_animations
    related_suffix = ""
    related_warning = ""
    if related_animations is not None:
        related_suffix = (
            f" and {len(related_animations.actions)} "
            "related animation(s)"
            if related_animations.enabled
            else ""
        )
        related_warning = _related_animation_warning(
            related_animations
        )
    _report_import_result(
        operator,
        settings,
        f"Imported {assembly.kind.lower()} assembly with "
        f"{len(imported.components)} component(s) and "
        f"{object_count} mesh object(s){related_suffix}",
        int(
            imported.root_collection.get(
                "bc2_diagnostic_count",
                0,
            )
        ),
        related_warning,
    )
    return imported


class BC2_UL_assets(UIList):
    def draw_item(
        self, context, layout, data, item, icon, active_data, active_property, index
    ):
        row = layout.row(align=True)
        row.label(text=_basename(item.path))
        row.label(
            text="",
            icon=_asset_type_icon(item.asset_type, item.path),
        )
        if data.show_asset_sizes:
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
    bl_description = (
        "Select and automatically scan a PC or Xbox 360 Battlefield: "
        "Bad Company 2 ISO"
    )

    filename_ext = ".iso"
    filter_glob: StringProperty(default="*.iso", options={"HIDDEN"})

    def execute(self, context):
        settings = _settings(context)
        _preferences(context).source_path = self.filepath
        settings.source_kind = ""
        settings.archives.clear()
        settings.assets.clear()
        settings.total_results = 0
        settings.indexed_asset_count = 0
        settings.status = "Disc selected; scanning"
        _close_repository()
        result = bpy.ops.bc2.scan_source("EXEC_DEFAULT")
        if "RUNNING_MODAL" in result:
            return {"FINISHED"}
        return {"CANCELLED"}


class BC2_OT_open_cache_directory(Operator):
    bl_idname = "bc2.open_cache_directory"
    bl_label = "Open Cache Folder"
    bl_description = "Open the configured BC2 asset cache in the system file browser"

    def execute(self, context):
        try:
            raw_path = bpy.path.abspath(_preferences(context).cache_directory)
            if not raw_path:
                raise BC2Error("Configure an asset cache directory first")
            path = Path(raw_path).resolve()
            path.mkdir(parents=True, exist_ok=True)
            result = bpy.ops.wm.path_open(filepath=str(path))
            if "FINISHED" not in result:
                raise BC2Error(f"Could not open the cache folder: {path}")
            return {"FINISHED"}
        except (BC2Error, OSError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class BC2_OT_reset_import_options(Operator):
    bl_idname = "bc2.reset_import_options"
    bl_label = "Reset to Preference Defaults"
    bl_description = "Reset this Blender session's import options to add-on defaults"

    def execute(self, context):
        settings = _settings(context)
        _apply_preference_defaults(
            settings,
            _preferences(context),
            force=True,
        )
        settings.status = "Import options reset to add-on defaults"
        self.report({"INFO"}, settings.status)
        return {"FINISHED"}


class BC2_OT_scan_source(Operator):
    bl_idname = "bc2.scan_source"
    bl_label = "Scan Disc"
    bl_description = (
        "Catalog Frostbite archives, or run an explicitly requested indexing job"
    )

    target_level: StringProperty(default="", options={"HIDDEN"})
    target_archive: StringProperty(default="", options={"HIDDEN"})
    index_all: BoolProperty(default=False, options={"HIDDEN"})

    def _start(self, context):
        global _active_scan_cancel, _active_scan_thread
        settings = _settings(context)
        if (
            _active_scan_thread is not None
            and _active_scan_thread.is_alive()
        ):
            settings.scan_in_progress = True
            self.report({"WARNING"}, "A disc scan is already running")
            return {"CANCELLED"}

        preferences = _preferences(context)
        source_path = bpy.path.abspath(preferences.source_path)
        cache_path = bpy.path.abspath(preferences.cache_directory)
        if not source_path:
            self.report({"ERROR"}, "Select a PC or Xbox 360 disc image first")
            return {"CANCELLED"}
        if not cache_path:
            self.report({"ERROR"}, "Configure an asset cache directory first")
            return {"CANCELLED"}

        source_path = str(Path(source_path).resolve())
        cache_path = str(Path(cache_path).resolve())
        index_all_pc = self.index_all
        target_archives = ()
        if self.target_level:
            try:
                level_name = _validated_level_name(self.target_level)
            except BC2Error as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            target_archives = pc_level_archive_paths(
                level_name,
                include_terrain=False,
            )
            settings.map_name = level_name
        elif self.target_archive:
            target_archives = (self.target_archive,)
        _close_repository()

        self._events = queue.Queue()
        self._cancel_event = threading.Event()
        self._worker = threading.Thread(
            target=_scan_source_worker,
            args=(
                source_path,
                cache_path,
                index_all_pc,
                self._events,
                self._cancel_event,
                target_archives,
            ),
            name="BC2 asset indexer",
            daemon=True,
        )
        _active_scan_cancel = self._cancel_event
        _active_scan_thread = self._worker
        settings.scan_in_progress = True
        settings.status = (
            f"Starting background object indexing for {settings.map_name}"
            if self.target_level
            else f"Starting background indexing for {self.target_archive}"
            if self.target_archive
            else "Starting background disc scan"
        )
        context.window_manager.progress_begin(0, 1000)
        self._timer = context.window_manager.event_timer_add(
            0.1,
            window=context.window,
        )
        context.window_manager.modal_handler_add(self)
        self._worker.start()
        return {"RUNNING_MODAL"}

    def _finish(self, context):
        global _active_scan_cancel, _active_scan_thread
        if getattr(self, "_timer", None) is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        context.window_manager.progress_end()
        _settings(context).scan_in_progress = False
        if _active_scan_thread is getattr(self, "_worker", None):
            _active_scan_thread = None
            _active_scan_cancel = None

    def invoke(self, context, event):
        return self._start(context)

    def execute(self, context):
        return self._start(context)

    def modal(self, context, event):
        settings = _settings(context)
        if event.type == "ESC":
            self._cancel_event.set()
            settings.status = "Cancelling after the current archive"
            return {"RUNNING_MODAL"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        terminal = None
        while True:
            try:
                message = self._events.get_nowait()
            except queue.Empty:
                break
            if message[0] == "progress":
                _, phase, current, total, label = message
                fraction = current / max(1, total)
                context.window_manager.progress_update(
                    int(fraction * 1000)
                )
                settings.status = (
                    f"{phase} {current:,}/{total:,}: {label}"
                )
            else:
                terminal = message

        if terminal is None:
            return {"RUNNING_MODAL"}

        self._finish(context)
        kind, payload = terminal
        if kind == "error":
            settings.status = payload
            self.report({"ERROR"}, payload)
            return {"CANCELLED"}
        try:
            _apply_scan_result(
                context,
                self,
                payload,
                cancelled=kind == "cancelled",
            )
        except (BC2Error, OSError, RuntimeError, ValueError) as exc:
            settings.status = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"CANCELLED"} if kind == "cancelled" else {"FINISHED"}


class BC2_OT_cancel_scan(Operator):
    bl_idname = "bc2.cancel_scan"
    bl_label = "Cancel Scan"
    bl_description = "Stop after the archive currently being indexed"

    def execute(self, context):
        if _active_scan_cancel is None:
            self.report({"WARNING"}, "No disc scan is running")
            return {"CANCELLED"}
        _active_scan_cancel.set()
        settings = _settings(context)
        settings.status = "Cancelling after the current archive"
        self.report({"INFO"}, settings.status)
        return {"FINISHED"}


class BC2_OT_refresh_archives(Operator):
    bl_idname = "bc2.refresh_archives"
    bl_label = "Refresh Archives"

    def execute(self, context):
        try:
            _load_archives(context)
            return {"FINISHED"}
        except (BC2Error, OSError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class BC2_OT_index_archive(Operator):
    bl_idname = "bc2.index_archive"
    bl_label = "Index Selected Archive"
    bl_description = (
        "Extract the selected PC Frostbite archive from its cabinet and index its assets"
    )

    def execute(self, context):
        try:
            archive = _selected_archive(_settings(context))
            result = bpy.ops.bc2.scan_source(
                "EXEC_DEFAULT",
                target_archive=archive.path,
            )
            if "RUNNING_MODAL" in result:
                return {"FINISHED"}
            return {"CANCELLED"}
        except (BC2Error, OSError) as exc:
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
        try:
            level_name = _validated_level_name(settings.map_name)
        except BC2Error as exc:
            self.report({"ERROR"}, str(exc))
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
            _report_import_result(
                self,
                settings,
                (
                    f"Imported {level_name}: {summary['placements']:,} objects, "
                    f"{summary['unique_mesh_resources']:,} unique meshes, "
                    f"{summary['heightfields']:,} terrain tiles"
                ),
                summary["diagnostics"],
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


def _draw_asset_browser(layout, settings: BC2Settings, *, rows: int, list_id: str):
    layout.prop(settings, "content_group")
    layout.prop(settings, "category")
    options = layout.row(align=True)
    options.prop(settings, "collapse_virtual_paths")
    options.prop(settings, "hide_lod_variants")
    row = layout.row(align=True)
    row.prop(settings, "search", text="")
    row.prop(settings, "show_asset_sizes")
    refresh = row.operator(
        BC2_OT_refresh_assets.bl_idname,
        text="",
        icon="FILE_REFRESH",
    )
    refresh.reset_page = True
    layout.template_list(
        BC2_UL_assets.__name__,
        list_id,
        settings,
        "assets",
        settings,
        "active_asset_index",
        rows=rows,
    )
    total_pages = max(
        1,
        (settings.total_results - 1) // RESULTS_PER_PAGE + 1,
    )
    paging = layout.row(align=True)
    previous = paging.operator(
        BC2_OT_change_page.bl_idname,
        text="",
        icon="TRIA_LEFT",
    )
    previous.delta = -1
    paging.label(
        text=f"Page {settings.page + 1}/{total_pages} "
        f"({settings.total_results:,} assets)"
    )
    following = paging.operator(
        BC2_OT_change_page.bl_idname,
        text="",
        icon="TRIA_RIGHT",
    )
    following.delta = 1


def _draw_import_settings(layout, settings: BC2Settings):
    layout.prop(settings, "material_mode")
    layout.prop(settings, "composite_visibility")
    layout.prop(settings, "import_vehicle_assemblies")
    layout.prop(settings, "import_related_animations")
    gameplay = layout.row()
    gameplay.active = settings.import_related_animations
    gameplay.prop(settings, "import_core_gameplay_animations")
    layout.prop(settings, "axis_conversion")
    layout.prop(settings, "flip_v")
    layout.prop(settings, "unit_scale")
    layout.prop(settings, "include_z_only")
    layout.prop(settings, "terrain_resolution")
    layout.operator(
        BC2_OT_reset_import_options.bl_idname,
        icon="LOOP_BACK",
    )


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
        _draw_asset_browser(
            layout,
            settings,
            rows=14,
            list_id="menu_browser",
        )
        options = layout.box()
        options.label(text="Import Settings", icon="PREFERENCES")
        _draw_import_settings(options, settings)
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
                source_descriptor = (
                    _mesh_set_descriptor_asset(
                        repository,
                        source_asset,
                    )
                    if (
                        asset_type
                        in {"RigidMeshSet", "TreeMeshSet"}
                        and source_asset["platform"] == "pc"
                    )
                    else None
                )
                linked_vehicle = (
                    _vehicle_assembly_for_mesh_resource(
                        repository,
                        source_asset,
                    )
                    if (
                        asset_type == "CompositeMeshSet"
                        and settings.import_vehicle_assemblies
                    )
                    else None
                )
                if linked_vehicle is not None:
                    assembly_asset, assembly = linked_vehicle
                    _import_modular_assembly_asset(
                        self,
                        context,
                        repository,
                        assembly_asset,
                        assembly,
                        settings,
                    )
                else:
                    asset, raw = repository.read_asset(
                        selected.asset_id,
                        max_size=1 << 30,
                        resolve_mesh=True,
                    )
                    parsed = parse_meshdata(
                        raw,
                        asset["platform"],
                        include_z_only=settings.include_z_only,
                        retain_pc_vehicle_uv_candidates=(
                            str(source_asset["path"])
                            .casefold()
                            .startswith("objects/vehicles/")
                        ),
                    )
                    if (
                        asset_type == "SkinnedMeshSet"
                        and source_asset["platform"] == "pc"
                    ):
                        (
                            armature,
                            objects,
                            related_animations,
                        ) = _create_character(
                            context,
                            repository,
                            source_asset,
                            parsed,
                            asset,
                            settings,
                        )
                        related_suffix = (
                            f" and {len(related_animations.actions)} "
                            "related animation(s)"
                            if related_animations.enabled
                            else ""
                        )
                        _report_import_result(
                            self,
                            settings,
                            f"Imported {len(objects)} skinned mesh object(s) "
                            f"on {len(armature.data.bones)} bones"
                            f"{related_suffix}",
                            int(
                                armature.get(
                                    "bc2_diagnostic_count",
                                    0,
                                )
                            ),
                            _related_animation_warning(
                                related_animations
                            ),
                        )
                    elif (
                        asset_type == "CompositeMeshSet"
                        or source_descriptor is not None
                    ):
                        root_collection, objects = _create_mesh_set_import(
                            context,
                            repository,
                            source_asset,
                            parsed,
                            asset,
                            settings,
                        )
                        _report_import_result(
                            self,
                            settings,
                            (
                                f"Imported {len(objects)} modular mesh "
                                "object(s)"
                                if asset_type == "CompositeMeshSet"
                                else (
                                    f"Imported {len(objects)} mesh object(s) "
                                    "with source material references"
                                )
                            ),
                            int(
                                root_collection.get(
                                    "bc2_diagnostic_count",
                                    0,
                                )
                            ),
                        )
                    else:
                        objects = _create_mesh_objects(
                            context,
                            parsed,
                            asset,
                            settings,
                        )
                        _report_import_result(
                            self,
                            settings,
                            f"Imported {len(objects)} mesh object(s)",
                            warning=(
                                (
                                    "source material resolution is currently "
                                    "implemented for PC mesh sets only"
                                )
                                if (
                                    asset_type
                                    in {"RigidMeshSet", "TreeMeshSet"}
                                    and _uses_source_materials(settings)
                                    and source_asset["platform"] != "pc"
                                )
                                else (
                                    "no same-name source descriptor is "
                                    "indexed; imported neutral subset "
                                    "materials"
                                    if (
                                        asset_type
                                        in {
                                            "RigidMeshSet",
                                            "TreeMeshSet",
                                        }
                                        and _uses_source_materials(settings)
                                    )
                                    else ""
                                )
                            ),
                        )
            elif asset_type == "GrannyModel":
                asset, raw = repository.read_asset(
                    selected.asset_id, max_size=256 << 20
                )
                skeleton = parse_granny_model(raw, asset["platform"])
                armature = _create_armature_object(
                    context, skeleton, asset, settings
                )
                _report_import_result(
                    self,
                    settings,
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
                _report_import_result(
                    self,
                    settings,
                    f"Imported {action.name} on {armature.name} "
                    f"({int(action['bc2_track_count'])} tracks)",
                )
            elif asset_type == "WaterMesh":
                asset, raw = repository.read_asset(
                    selected.asset_id, max_size=1 << 30
                )
                parsed = parse_watermesh(raw, asset["platform"])
                objects = _create_mesh_objects(context, parsed, asset, settings)
                _report_import_result(
                    self,
                    settings,
                    f"Imported {len(objects)} water mesh object(s)",
                )
            elif asset_type in {"ITexture", "XenonTexture", "Ps3Texture"}:
                asset, raw = repository.read_asset(
                    selected.asset_id, max_size=1 << 30
                )
                _, _, converted = _import_texture(context, repository, asset, raw)
                _report_import_result(
                    self,
                    settings,
                    f"Imported {converted.width}x{converted.height} {converted.format_name}",
                )
            elif asset_type == "TerrainHeightfield":
                asset, raw = repository.read_asset(
                    selected.asset_id, max_size=1 << 30
                )
                parsed = parse_heightfield(raw, settings.terrain_resolution)
                _create_terrain_object(context, parsed, asset, settings)
                _report_import_result(
                    self,
                    settings,
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
                    _import_modular_assembly_asset(
                        self,
                        context,
                        repository,
                        asset,
                        assembly,
                        settings,
                    )
                else:
                    xml = dbx_to_xml(raw)
                    text = bpy.data.texts.new(
                        Path(asset["path"]).stem + ".xml"
                    )
                    text.write(xml)
                    text["bc2_asset_path"] = asset["path"]
                    text["bc2_archive_path"] = asset["archive_path"]
                    _report_import_result(
                        self,
                        settings,
                        f"Imported DBX as text: {text.name}",
                    )
            else:
                _, path = repository.extract_asset(selected.asset_id)
                _report_import_result(
                    self,
                    settings,
                    f"Extracted unsupported resource to {path}",
                )
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


class BC2_PT_tools(Panel):
    bl_idname = "BC2_PT_tools"
    bl_label = "Bad Company 2 Assets"
    bl_category = "Frostbite"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        if settings.source_kind:
            layout.label(text=settings.source_kind, icon="CHECKMARK")
            layout.label(
                text=f"{settings.indexed_asset_count:,} indexed assets",
                icon="ASSET_MANAGER",
            )
        else:
            layout.label(text="Select and scan a disc image", icon="INFO")
        if settings.diagnostic_count:
            layout.label(
                text=f"Last import: {settings.diagnostic_count:,} diagnostic(s)",
                icon="ERROR",
            )


class BC2_PT_source(Panel):
    bl_idname = "BC2_PT_source"
    bl_label = "Source"
    bl_parent_id = BC2_PT_tools.bl_idname
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        preferences = _preferences(context)
        layout.prop(preferences, "source_path", text="")
        row = layout.row(align=True)
        row.enabled = not settings.scan_in_progress
        row.operator(
            BC2_OT_choose_source.bl_idname,
            text="Choose",
            icon="FILE_FOLDER",
        )
        row.operator(
            BC2_OT_scan_source.bl_idname,
            text="Scan",
            icon="VIEWZOOM",
        )
        if settings.scan_in_progress:
            layout.operator(
                BC2_OT_cancel_scan.bl_idname,
                text="Cancel After Current Archive",
                icon="CANCEL",
            )
        if settings.source_kind:
            layout.label(text=settings.source_kind, icon="CHECKMARK")
        layout.label(text=settings.status)


class BC2_PT_map(Panel):
    bl_idname = "BC2_PT_map"
    bl_label = "Complete PC Map"
    bl_parent_id = BC2_PT_tools.bl_idname
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    @classmethod
    def poll(cls, context):
        return _settings(context).source_kind == "PC_ISO"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        if settings.available_maps:
            layout.label(
                text=f"{len(settings.available_maps):,} maps found in the disc catalog.",
                icon="WORLD_DATA",
            )
            layout.prop_search(
                settings,
                "map_name",
                settings,
                "available_maps",
                text="Level",
            )
        else:
            layout.label(
                text="No level/terrain archive pairs were discovered.",
                icon="INFO",
            )
            layout.prop(settings, "map_name", text="Level")
        row = layout.row(align=True)
        row.enabled = not settings.scan_in_progress
        prepare = row.operator(
            BC2_OT_scan_source.bl_idname,
            text="Index Map Objects",
            icon="PACKAGE",
        )
        prepare.target_level = settings.map_name
        row.operator(
            BC2_OT_import_map.bl_idname,
            text="Import Map",
            icon="IMPORT",
        )


class BC2_PT_archives(Panel):
    bl_idname = "BC2_PT_archives"
    bl_label = "PC Frostbite Archives"
    bl_parent_id = BC2_PT_tools.bl_idname
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    @classmethod
    def poll(cls, context):
        return _settings(context).source_kind == "PC_ISO"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        row = layout.row(align=True)
        row.prop(settings, "archive_search", text="")
        row.operator(
            BC2_OT_refresh_archives.bl_idname,
            text="",
            icon="FILE_REFRESH",
        )
        layout.template_list(
            BC2_UL_archives.__name__,
            "",
            settings,
            "archives",
            settings,
            "active_archive_index",
            rows=5,
        )
        row = layout.row()
        row.enabled = not settings.scan_in_progress
        row.operator(
            BC2_OT_index_archive.bl_idname,
            text="Index Selected Archive",
            icon="PACKAGE",
        )
        index_all = row.operator(
            BC2_OT_scan_source.bl_idname,
            text="Index All (Slow)",
            icon="TIME",
        )
        index_all.index_all = True


class BC2_PT_assets(Panel):
    bl_idname = "BC2_PT_assets"
    bl_label = "Indexed Assets"
    bl_parent_id = BC2_PT_tools.bl_idname
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        if (
            settings.source_kind == "PC_ISO"
            and not settings.indexed_asset_count
        ):
            info = layout.box()
            info.label(
                text="PC scan catalogs maps and archives first.",
                icon="INFO",
            )
            info.label(
                text="Index a relevant archive or import a discovered map."
            )
        elif settings.source_kind and not settings.total_results:
            layout.label(
                text="No indexed assets match the current filter.",
                icon="INFO",
            )
        content = layout.column()
        content.enabled = bool(settings.source_kind)
        _draw_asset_browser(
            content,
            settings,
            rows=8,
            list_id="sidebar_browser",
        )
        row = content.row(align=True)
        row.operator(BC2_OT_import_selected.bl_idname, icon="IMPORT")
        row.operator(BC2_OT_extract_selected.bl_idname, icon="EXPORT")


class BC2_PT_import_settings(Panel):
    bl_idname = "BC2_PT_import_settings"
    bl_label = "Import Settings"
    bl_parent_id = BC2_PT_tools.bl_idname
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        _draw_import_settings(self.layout, _settings(context))


def _draw_import_menu(self, context):
    self.layout.menu(BC2_MT_import.bl_idname, icon="PACKAGE")


CLASSES = (
    BC2AddonPreferences,
    BC2AssetListItem,
    BC2ArchiveListItem,
    BC2MapListItem,
    BC2Settings,
    BC2_UL_assets,
    BC2_UL_archives,
    BC2_OT_choose_source,
    BC2_OT_open_cache_directory,
    BC2_OT_reset_import_options,
    BC2_OT_scan_source,
    BC2_OT_cancel_scan,
    BC2_OT_refresh_archives,
    BC2_OT_index_archive,
    BC2_OT_import_map,
    BC2_OT_refresh_assets,
    BC2_OT_change_page,
    BC2_OT_open_browser,
    BC2_OT_import_selected,
    BC2_OT_extract_selected,
    BC2_MT_import,
    BC2_PT_tools,
    BC2_PT_source,
    BC2_PT_map,
    BC2_PT_archives,
    BC2_PT_assets,
    BC2_PT_import_settings,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.bc2_settings = PointerProperty(type=BC2Settings)
    bpy.types.TOPBAR_MT_file_import.append(_draw_import_menu)


def unregister():
    if _active_scan_cancel is not None:
        _active_scan_cancel.set()
    _close_repository()
    bpy.types.TOPBAR_MT_file_import.remove(_draw_import_menu)
    del bpy.types.WindowManager.bc2_settings
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
