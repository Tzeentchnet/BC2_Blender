# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Callable, Iterable

from .dbx import DbxDocument, DbxElement, parse_dbx
from .errors import CorruptDataError, ExtractionError
from .repository import AssetRepository


GROUP_TYPES = {
    "GameSharedResources.ForestEntityData",
    "GameSharedResources.MiddlegrowthModelGroupEntityData",
    "GameSharedResources.RigidModelGroupEntityData",
    "GameSharedResources.StaticModelGroupEntityData",
}

TERRAIN_DECAL_TYPES = (
    "Terrain.TerrainFullDecal",
    "Terrain.TerrainGroupDecal",
    "Terrain.TerrainSplineDecal",
)

PC_CHARACTER_SHADER_ARCHIVE = "Dist/win32/levels/mp_001/level-00.fbrb"
PC_CHARACTER_SHADER_PATH = "Levels/mp_001/shaders/Dx11_Single.res"


def discover_pc_levels(archive_paths: Iterable[str]) -> tuple[str, ...]:
    level_files: dict[str, tuple[str, set[str]]] = {}
    for archive_path in archive_paths:
        parts = archive_path.replace("\\", "/").split("/")
        if (
            len(parts) != 5
            or [part.casefold() for part in parts[:3]]
            != ["dist", "win32", "levels"]
        ):
            continue
        filename = parts[4].casefold()
        if filename not in {"level-00.fbrb", "terrain-00.fbrb"}:
            continue
        key = parts[3].casefold()
        display_name, files = level_files.setdefault(
            key,
            (parts[3].upper(), set()),
        )
        files.add(filename)

    required = {"level-00.fbrb", "terrain-00.fbrb"}
    return tuple(
        display_name
        for _, (display_name, files) in sorted(level_files.items())
        if required <= files
    )


def pc_level_archive_paths(
    level_name: str,
    *,
    include_terrain: bool = True,
) -> tuple[str, ...]:
    level = level_name.casefold()
    paths = [f"Dist/win32/levels/{level}/level-00.fbrb"]
    if include_terrain:
        paths.append(f"Dist/win32/levels/{level}/terrain-00.fbrb")
    paths.append("Dist/win32/levels/mp_common/level-00.fbrb")
    return tuple(paths)


@dataclass(frozen=True)
class FrostbiteTransform:
    values: tuple[float, ...]

    def __post_init__(self):
        if len(self.values) != 16:
            raise CorruptDataError(
                f"Frostbite transform has {len(self.values)} values, expected 16"
            )
        if not all(math.isfinite(value) for value in self.values):
            raise CorruptDataError("Frostbite transform contains a non-finite value")

    @property
    def translation(self) -> tuple[float, float, float]:
        return self.values[12], self.values[13], self.values[14]

    def blender_translation(
        self,
        axis_conversion: bool = True,
        unit_scale: float = 1.0,
    ) -> tuple[float, float, float]:
        x, y, z = self.translation
        if axis_conversion:
            return x * unit_scale, -z * unit_scale, y * unit_scale
        return x * unit_scale, y * unit_scale, z * unit_scale

    def blender_matrix(
        self,
        axis_conversion: bool = True,
        unit_scale: float = 1.0,
    ) -> tuple[tuple[float, float, float, float], ...]:
        values = self.values
        frostbite = (
            (values[0], values[4], values[8]),
            (values[1], values[5], values[9]),
            (values[2], values[6], values[10]),
        )
        if axis_conversion:
            axis = (
                (1.0, 0.0, 0.0),
                (0.0, 0.0, -1.0),
                (0.0, 1.0, 0.0),
            )
            linear = _matrix_multiply(
                _matrix_multiply(axis, frostbite),
                _matrix_transpose(axis),
            )
        else:
            linear = frostbite
        translation = self.blender_translation(axis_conversion, unit_scale)
        return (
            (*linear[0], translation[0]),
            (*linear[1], translation[1]),
            (*linear[2], translation[2]),
            (0.0, 0.0, 0.0, 1.0),
        )


@dataclass(frozen=True)
class SurfaceShader:
    name: str
    reference: str
    database_key: str


@dataclass(frozen=True)
class MeshDefinition:
    key: str
    reference: str
    name: str
    mesh_data_path: str
    shader_set_reference: str | None
    surfaces: tuple[SurfaceShader, ...]


@dataclass(frozen=True)
class LevelPlacement:
    guid: str
    name: str
    source: str
    entity_type: str
    reference: str
    mesh_key: str
    transform: FrostbiteTransform
    enumeration: int | None


@dataclass(frozen=True)
class LevelIssue:
    code: str
    message: str
    reference: str | None = None


@dataclass(frozen=True)
class LevelScene:
    name: str
    placements: tuple[LevelPlacement, ...]
    meshes: tuple[MeshDefinition, ...]
    terrain_transform: FrostbiteTransform | None
    heightfield_paths: tuple[str, ...]
    terrain_material_map_path: str | None
    water_mesh_path: str | None
    shader_database_path: str | None
    issues: tuple[LevelIssue, ...]


@dataclass(frozen=True)
class _Blueprint:
    reference: str
    name: str
    entity_type: str
    mesh_reference: str | None
    shader_set_reference: str | None


def _matrix_multiply(left, right):
    return tuple(
        tuple(
            sum(left[row][index] * right[index][column] for index in range(3))
            for column in range(3)
        )
        for row in range(3)
    )


def _matrix_transpose(matrix):
    return tuple(
        tuple(matrix[column][row] for column in range(3))
        for row in range(3)
    )


def _field(element: DbxElement | None, name: str) -> DbxElement | None:
    return element.child("field", name) if element is not None else None


def _field_value(element: DbxElement | None, name: str) -> str | None:
    field = _field(element, name)
    return field.value if field is not None else None


def _field_reference(element: DbxElement | None, name: str) -> str | None:
    field = _field(element, name)
    if field is None:
        return None
    reference = field.attribute("ref")
    return (
        reference
        if reference and reference.casefold() != "null"
        else None
    )


def _array(element: DbxElement | None, name: str) -> DbxElement | None:
    return element.child("array", name) if element is not None else None


def _array_references(element: DbxElement | None, name: str) -> tuple[str, ...]:
    array = _array(element, name)
    if array is None:
        return ()
    return tuple(
        reference
        for item in array.children_named("item")
        if (reference := item.attribute("ref"))
        and reference.casefold() != "null"
    )


def _number_array(element: DbxElement | None, name: str) -> tuple[float, ...]:
    array = _array(element, name)
    if array is None or array.value is None or not array.value:
        return ()
    try:
        return tuple(float(value) for value in array.value.split("/"))
    except ValueError as exc:
        raise CorruptDataError(f"Invalid DBX numeric array {name}") from exc


def _transform(element: DbxElement | None) -> FrostbiteTransform | None:
    value = element.child("complex", "Transform") if element is not None else None
    if value is None or not value.value:
        return None
    try:
        return FrostbiteTransform(
            tuple(float(component) for component in value.value.split("/"))
        )
    except ValueError as exc:
        raise CorruptDataError("Invalid DBX Transform values") from exc


def split_reference(reference: str) -> tuple[str, str]:
    if "/" not in reference:
        raise CorruptDataError(f"DBX reference has no partition path: {reference}")
    partition, guid = reference.rsplit("/", 1)
    if not partition or not guid:
        raise CorruptDataError(f"Malformed DBX reference: {reference}")
    return partition, guid


def index_pc_level_archives(
    repository: AssetRepository,
    level_name: str,
    progress: Callable[[int, int, str], None] | None = None,
) -> tuple[str, ...]:
    if repository.kind != "PC_ISO":
        raise ExtractionError("Whole-map import currently supports PC retail ISOs")
    archive_paths = pc_level_archive_paths(level_name)
    for index, path in enumerate(archive_paths, 1):
        repository.index_archive_path(path)
        repository.materialize_archive_payload(path)
        if progress:
            progress(index, len(archive_paths), path)
    return archive_paths


def index_pc_character_shader_archive(
    repository: AssetRepository,
) -> str:
    if repository.kind != "PC_ISO":
        raise ExtractionError(
            "Character shader materials currently require a PC retail ISO"
        )
    repository.index_archive_path(PC_CHARACTER_SHADER_ARCHIVE)
    repository.materialize_archive_payload(PC_CHARACTER_SHADER_ARCHIVE)
    return PC_CHARACTER_SHADER_ARCHIVE


class LevelResolver:
    def __init__(
        self,
        repository: AssetRepository,
        level_name: str,
        preferred_archives: tuple[str, ...] = (),
    ):
        self.repository = repository
        self.level_name = level_name.upper()
        self.preferred_archives = preferred_archives
        self._documents: dict[str, DbxDocument] = {}
        self._blueprints: dict[str, _Blueprint] = {}
        self._meshes: dict[str, MeshDefinition] = {}
        self._issues: list[LevelIssue] = []

    def _document(self, path: str) -> DbxDocument:
        key = path.casefold()
        document = self._documents.get(key)
        if document is None:
            _, data = self.repository.read_asset_path(
                path,
                max_size=256 << 20,
                preferred_archives=self.preferred_archives,
            )
            document = parse_dbx(data)
            self._documents[key] = document
        return document

    def _external_instance(
        self, reference: str
    ) -> tuple[DbxDocument, DbxElement]:
        partition, guid = split_reference(reference)
        document = self._document(partition + ".dbx")
        instance = document.instance(guid)
        if instance is None:
            raise CorruptDataError(
                f"DBX instance {guid} is missing from {partition}.dbx"
            )
        return document, instance

    def _blueprint(self, reference: str) -> _Blueprint:
        key = reference.casefold()
        cached = self._blueprints.get(key)
        if cached is not None:
            return cached
        document, target = self._external_instance(reference)
        object_reference = _field_reference(target, "Object")
        entity = document.instance(object_reference) if object_reference else target
        if entity is None:
            raise CorruptDataError(
                f"Blueprint object is missing for {reference}"
            )
        partition, _ = split_reference(reference)
        blueprint = _Blueprint(
            reference=reference,
            name=(
                _field_value(entity, "Name")
                or _field_value(target, "Name")
                or PurePosixPath(partition).name
            ),
            entity_type=entity.attribute("type") or "<unknown>",
            mesh_reference=_field_reference(entity, "Mesh"),
            shader_set_reference=_field_reference(entity, "MeshShaderSet"),
        )
        self._blueprints[key] = blueprint
        return blueprint

    def _shader_sets(
        self,
        mesh_document: DbxDocument,
        mesh_instance: DbxElement,
        override_reference: str | None,
    ) -> tuple[str | None, tuple[DbxElement, ...]]:
        shader_sets = _array_references(mesh_instance, "ShaderSets")
        default_reference = shader_sets[0] if shader_sets else None
        resolved = []
        if default_reference:
            default_set = mesh_document.instance(default_reference)
            if default_set is not None:
                resolved.append(default_set)
        selected_reference = override_reference or default_reference
        if override_reference:
            if "/" in override_reference:
                _, override_set = self._external_instance(override_reference)
            else:
                override_set = mesh_document.instance(override_reference)
            if override_set is not None and override_set not in resolved:
                resolved.append(override_set)
        return selected_reference, tuple(resolved)

    def _mesh(
        self,
        mesh_reference: str,
        shader_set_override: str | None,
    ) -> MeshDefinition:
        key = (
            mesh_reference.casefold()
            + "|"
            + (shader_set_override or "").casefold()
        )
        cached = self._meshes.get(key)
        if cached is not None:
            return cached
        partition, guid = split_reference(mesh_reference)
        document = self._document(partition + ".dbx")
        mesh_instance = document.instance(guid)
        if mesh_instance is None:
            raise CorruptDataError(
                f"Mesh instance {guid} is missing from {partition}.dbx"
            )
        mesh_row = self.repository.catalog.find_mesh_data(
            partition,
            self.preferred_archives,
        )
        if mesh_row is None:
            raise ExtractionError(f"LOD MeshData was not found for {partition}")
        shader_set_reference, shader_sets = self._shader_sets(
            document,
            mesh_instance,
            shader_set_override,
        )
        surface_order = []
        surfaces_by_name = {}
        for shader_set in shader_sets:
            surface_array = _array(shader_set, "SurfaceShaders")
            if surface_array is None:
                continue
            for surface in surface_array.children_named("complex"):
                name = _field_value(surface, "Name") or "<unnamed>"
                reference = _field_reference(surface, "Shader")
                if reference is None:
                    self._issues.append(
                        LevelIssue(
                            code="missing_shader_reference",
                            message=f"Surface {name} has no shader reference",
                            reference=mesh_reference,
                        )
                    )
                    continue
                shader_partition, _ = split_reference(reference)
                mesh_name = _field_value(mesh_instance, "Name") or partition
                normalized_name = "".join(
                    character
                    for character in name.casefold()
                    if character.isalnum()
                )
                if normalized_name not in surfaces_by_name:
                    surface_order.append(normalized_name)
                surfaces_by_name[normalized_name] = SurfaceShader(
                    name=name,
                    reference=reference,
                    database_key=f"{mesh_name}/{shader_partition}",
                )
        surfaces = tuple(
            surfaces_by_name[name]
            for name in surface_order
        )
        definition = MeshDefinition(
            key=key,
            reference=mesh_reference,
            name=_field_value(mesh_instance, "Name") or partition,
            mesh_data_path=str(mesh_row["path"]),
            shader_set_reference=shader_set_reference,
            surfaces=surfaces,
        )
        if not surfaces:
            self._issues.append(
                LevelIssue(
                    code="missing_surface_shaders",
                    message="Mesh has no resolved surface shader set",
                    reference=mesh_reference,
                )
            )
        self._meshes[key] = definition
        return definition

    def resolve_mesh(
        self,
        mesh_reference: str,
        shader_set_override: str | None = None,
    ) -> MeshDefinition:
        return self._mesh(mesh_reference, shader_set_override)

    @property
    def issues(self) -> tuple[LevelIssue, ...]:
        return tuple(self._issues)

    def _direct_placements(
        self,
        level_document: DbxDocument,
        progress: Callable[[float, str], None] | None = None,
    ) -> list[LevelPlacement]:
        level_data = level_document.primary_instance
        if level_data is None:
            raise CorruptDataError("Level DBX has no primary instance")
        placements = []
        object_references = _array_references(level_data, "Objects")
        total = max(1, len(object_references))
        for index, guid in enumerate(object_references):
            if progress and (
                index % 100 == 0 or index + 1 == len(object_references)
            ):
                progress(
                    0.75 * index / total,
                    (
                        f"Resolving direct object {index + 1:,}/"
                        f"{len(object_references):,}"
                    ),
                )
            instance = level_document.instance(guid)
            if (
                instance is None
                or instance.attribute("type") != "Entity.ReferenceObjectData"
            ):
                continue
            transform = _transform(instance)
            reference = _field_reference(instance, "ReferencedObject")
            if transform is None or reference is None:
                self._issues.append(
                    LevelIssue(
                        code="incomplete_reference_object",
                        message="ReferenceObjectData lacks a transform or reference",
                        reference=guid,
                    )
                )
                continue
            blueprint = self._blueprint(reference)
            if blueprint.mesh_reference is None:
                self._issues.append(
                    LevelIssue(
                        code="non_visual_reference",
                        message=(
                            f"{blueprint.entity_type} has no static mesh and was "
                            "not imported"
                        ),
                        reference=reference,
                    )
                )
                continue
            shader_override = (
                _field_reference(instance, "MeshShaderSet")
                or blueprint.shader_set_reference
            )
            mesh = self._mesh(blueprint.mesh_reference, shader_override)
            enumeration_text = _field_value(instance, "Enumeration")
            placements.append(
                LevelPlacement(
                    guid=instance.attribute("guid") or guid,
                    name=blueprint.name,
                    source="reference",
                    entity_type=blueprint.entity_type,
                    reference=reference,
                    mesh_key=mesh.key,
                    transform=transform,
                    enumeration=(
                        int(enumeration_text)
                        if enumeration_text is not None
                        else None
                    ),
                )
            )
        return placements

    def _group_havok_document(
        self,
        level_document: DbxDocument,
        group: DbxElement,
    ) -> DbxDocument:
        physics = level_document.instance(
            _field_reference(group, "PhysicsData")
        )
        havok = level_document.instance(_field_reference(physics, "Asset"))
        name = _field_value(havok, "Name")
        if not name:
            raise CorruptDataError(
                f"{group.attribute('type')} has no external Havok asset name"
            )
        return self._document(name + ".dbx")

    def _group_placements(
        self,
        level_document: DbxDocument,
        group: DbxElement,
    ) -> list[LevelPlacement]:
        havok_document = self._group_havok_document(level_document, group)
        havok = havok_document.primary_instance
        if havok is None:
            raise CorruptDataError("Grouped Havok DBX has no primary instance")
        transform_values = _number_array(havok, "Transforms")
        if len(transform_values) % 16:
            raise CorruptDataError(
                "Grouped Havok transform array is not 16-float aligned"
            )
        transforms = tuple(
            FrostbiteTransform(transform_values[index:index + 16])
            for index in range(0, len(transform_values), 16)
        )
        enums = tuple(int(value) for value in _number_array(havok, "Enums"))
        assets = _array_references(havok, "Assets")
        if not (len(transforms) == len(enums) == len(assets)):
            raise CorruptDataError(
                "Grouped Havok transforms, enums, and assets have different lengths"
            )

        placements = []
        cursor = 0
        for member_guid in _array_references(group, "MemberDatas"):
            member = level_document.instance(member_guid)
            if member is None:
                raise CorruptDataError(
                    f"Grouped member is missing: {member_guid}"
                )
            mesh_entity = level_document.instance(
                _field_reference(member, "MeshEntityType")
            )
            mesh_reference = _field_reference(mesh_entity, "Mesh")
            if mesh_reference is None:
                raise CorruptDataError(
                    f"Grouped member has no visual mesh: {member_guid}"
                )
            member_shader_set = _field_reference(mesh_entity, "MeshShaderSet")
            member_name = (
                _field_value(member, "Name")
                or PurePosixPath(split_reference(mesh_reference)[0]).name
            )
            spawn_array = _array(member, "SpawnDatas")
            spawns = (
                spawn_array.children_named("complex")
                if spawn_array is not None
                else ()
            )
            for spawn in spawns:
                if cursor >= len(transforms):
                    raise CorruptDataError(
                        "Grouped spawn count exceeds Havok transform count"
                    )
                enumeration_text = _field_value(spawn, "Enumeration")
                enumeration = (
                    int(enumeration_text)
                    if enumeration_text is not None
                    else None
                )
                if enumeration != enums[cursor]:
                    raise CorruptDataError(
                        "Grouped spawn enumeration does not match Havok order"
                    )
                shader_override = (
                    _field_reference(spawn, "MeshShaderSet")
                    or member_shader_set
                )
                mesh = self._mesh(mesh_reference, shader_override)
                placements.append(
                    LevelPlacement(
                        guid=(
                            f"{group.attribute('guid') or 'group'}-"
                            f"{enumeration if enumeration is not None else cursor}"
                        ),
                        name=member_name,
                        source=group.attribute("type") or "group",
                        entity_type=(
                            mesh_entity.attribute("type")
                            if mesh_entity is not None
                            else "<unknown>"
                        ),
                        reference=assets[cursor],
                        mesh_key=mesh.key,
                        transform=transforms[cursor],
                        enumeration=enumeration,
                    )
                )
                cursor += 1
        if cursor != len(transforms):
            raise CorruptDataError(
                f"Grouped members consumed {cursor} of {len(transforms)} transforms"
            )
        return placements

    def _terrain(
        self,
        level_document: DbxDocument,
    ) -> tuple[
        FrostbiteTransform | None,
        tuple[str, ...],
        str | None,
        str | None,
    ]:
        level_data = level_document.primary_instance
        terrain_entity = next(
            (
                level_document.instance(guid)
                for guid in _array_references(level_data, "Objects")
                if (
                    level_document.instance(guid) is not None
                    and level_document.instance(guid).attribute("type")
                    == "GameSharedResources.TerrainEntityData"
                )
            ),
            None,
        )
        if terrain_entity is None:
            self._issues.append(
                LevelIssue(
                    code="missing_terrain_entity",
                    message="Level has no TerrainEntityData",
                )
            )
            return None, (), None, None
        terrain_reference = _field_reference(terrain_entity, "TerrainAsset")
        if terrain_reference is None:
            raise CorruptDataError("TerrainEntityData has no TerrainAsset reference")
        terrain_partition, _ = split_reference(terrain_reference)
        terrain_document = self._document(terrain_partition + ".dbx")
        _, terrain_guid = split_reference(terrain_reference)
        terrain_data = (
            terrain_document.instance(terrain_guid)
            or terrain_document.primary_instance
        )
        decal_counts = tuple(
            (
                decal_type,
                sum(
                    instance.attribute("type") == decal_type
                    for instance in terrain_document.instances
                ),
            )
            for decal_type in TERRAIN_DECAL_TYPES
        )
        if any(count for _, count in decal_counts):
            summary = ", ".join(
                f"{decal_type.rsplit('.', 1)[-1]}={count}"
                for decal_type, count in decal_counts
                if count
            )
            self._issues.append(
                LevelIssue(
                    code="unsupported_terrain_visual_decals",
                    message=(
                        "Terrain visual decal records are not rendered "
                        f"({summary})"
                    ),
                    reference=terrain_partition,
                )
            )
        material_map_reference = _field_reference(
            terrain_data,
            "MaterialMap",
        )
        material_map = None
        if material_map_reference:
            material_map_partition, _ = split_reference(
                material_map_reference
            )
            material_map = self.repository.catalog.find_asset(
                material_map_partition + ".res",
                self.preferred_archives,
            )
            if material_map is None:
                self._issues.append(
                    LevelIssue(
                        code="missing_terrain_material_map",
                        message="Terrain material map resource was not found",
                        reference=material_map_reference,
                    )
                )
        heightfields = tuple(
            str(row["path"])
            for row in self.repository.catalog.assets_with_prefix(
                terrain_partition + ".Heightfield-",
                "TerrainHeightfield",
            )
        )
        water = self.repository.catalog.find_asset(
            terrain_partition + ".Water.Mesh.res",
            self.preferred_archives,
        )
        if not heightfields:
            self._issues.append(
                LevelIssue(
                    code="missing_heightfields",
                    message="No indexed TerrainHeightfield resources were found",
                    reference=terrain_reference,
                )
            )
        return (
            _transform(terrain_entity),
            heightfields,
            str(material_map["path"]) if material_map is not None else None,
            str(water["path"]) if water is not None else None,
        )

    def resolve(
        self,
        progress: Callable[[float, str], None] | None = None,
    ) -> LevelScene:
        level_path = f"Levels/{self.level_name}.dbx"
        if progress:
            progress(0.0, f"Reading {level_path}")
        level_document = self._document(level_path)
        placements = self._direct_placements(level_document, progress)
        level_data = level_document.primary_instance
        groups = tuple(
            instance
            for guid in _array_references(level_data, "Objects")
            if (instance := level_document.instance(guid)) is not None
            and instance.attribute("type") in GROUP_TYPES
        )
        for index, instance in enumerate(groups):
            if progress:
                progress(
                    0.75 + 0.2 * index / max(1, len(groups)),
                    (
                        f"Resolving placement group {index + 1:,}/"
                        f"{len(groups):,}"
                    ),
                )
            placements.extend(
                self._group_placements(level_document, instance)
            )
        if progress:
            progress(0.95, "Resolving terrain and water")
        (
            terrain_transform,
            heightfields,
            terrain_material_map,
            water,
        ) = self._terrain(level_document)
        shader_path = f"Levels/{self.level_name}/shaders/Dx11_Single.res"
        shader = self.repository.catalog.find_asset(
            shader_path,
            self.preferred_archives,
        )
        if shader is None:
            self._issues.append(
                LevelIssue(
                    code="missing_shader_database",
                    message="DX11 shader database was not found",
                    reference=shader_path,
                )
            )
        if progress:
            progress(1.0, "Level graph resolved")
        return LevelScene(
            name=self.level_name,
            placements=tuple(placements),
            meshes=tuple(self._meshes.values()),
            terrain_transform=terrain_transform,
            heightfield_paths=heightfields,
            terrain_material_map_path=terrain_material_map,
            water_mesh_path=water,
            shader_database_path=(
                str(shader["path"]) if shader is not None else None
            ),
            issues=tuple(self._issues),
        )
