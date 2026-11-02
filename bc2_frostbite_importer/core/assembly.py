# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from .dbx import DbxDocument, DbxElement
from .errors import CorruptDataError
from .level import FrostbiteTransform


@dataclass(frozen=True)
class AssemblyComponent:
    name: str
    role: str
    mesh_reference: str
    mesh_set_path: str
    shader_set_reference: str | None
    random_group: int | None
    skeleton_reference: str | None = None
    transform: FrostbiteTransform | None = None
    source_type: str | None = None
    source_guid: str | None = None


@dataclass(frozen=True)
class AssemblyPart:
    name: str
    state: str
    source: str
    component_type: str
    component_name: str | None


@dataclass(frozen=True)
class CharacterAnimationLinks:
    animation_references: tuple[str, ...] = ()
    animation_tree_references: tuple[str, ...] = ()
    shared_animation_tree_references: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModularAssembly:
    name: str
    kind: str
    source_path: str
    components: tuple[AssemblyComponent, ...]
    parts: tuple[AssemblyPart, ...] = ()
    animation_links: CharacterAnimationLinks | None = None


def _field(element: DbxElement, name: str) -> DbxElement | None:
    return element.child("field", name)


def _reference(element: DbxElement, name: str) -> str | None:
    field = _field(element, name)
    reference = field.attribute("ref") if field is not None else None
    return (
        reference
        if reference and reference.casefold() != "null"
        else None
    )


def _value(element: DbxElement, name: str) -> str | None:
    field = _field(element, name)
    return field.value if field is not None else None


def _array_references(
    element: DbxElement,
    name: str,
) -> tuple[str, ...]:
    array = element.child("array", name)
    if array is None:
        return ()
    return tuple(
        reference
        for child in array.children
        if (reference := child.attribute("ref"))
        and reference.casefold() != "null"
    )


def _unique_references(references) -> tuple[str, ...]:
    unique = {}
    for reference in references:
        if reference and reference.casefold() != "null":
            unique.setdefault(reference.casefold(), reference)
    return tuple(unique.values())


def _character_animation_links(
    document: DbxDocument,
    instance: DbxElement,
) -> CharacterAnimationLinks:
    animation_references = []
    animation_tree_references = []
    shared_animation_tree_references = []
    tree_reference = _reference(instance, "AnimTree3p")
    tree = document.instance(tree_reference)
    if tree is None:
        return CharacterAnimationLinks()

    static_pose = _reference(tree, "StaticPose")
    if static_pose and "/" in static_pose:
        animation_references.append(static_pose)

    for name in ("KitJointsAnimTree", "ReviveAnimTree"):
        reference = _reference(tree, name)
        if reference and "/" in reference:
            animation_tree_references.append(reference)

    facial_reference = _reference(tree, "FacialAnimations")
    if facial_reference:
        if "/" in facial_reference:
            animation_tree_references.append(facial_reference)
        else:
            facial_data = document.instance(facial_reference)
            if facial_data is not None:
                reference = _reference(facial_data, "AnimTree")
                if reference and "/" in reference:
                    animation_tree_references.append(reference)

    shared_reference = _reference(tree, "Shared")
    if shared_reference and "/" in shared_reference:
        shared_animation_tree_references.append(shared_reference)

    return CharacterAnimationLinks(
        animation_references=_unique_references(animation_references),
        animation_tree_references=_unique_references(
            animation_tree_references
        ),
        shared_animation_tree_references=_unique_references(
            shared_animation_tree_references
        ),
    )


def character_animation_links(
    document: DbxDocument,
    mesh_set_path: str | None = None,
) -> CharacterAnimationLinks | None:
    instance = document.primary_instance
    if instance is None:
        return None
    components = _character_components(instance)
    if not components:
        return None
    if (
        mesh_set_path is not None
        and all(
            component.mesh_set_path.casefold()
            != mesh_set_path.casefold()
            for component in components
        )
    ):
        return None
    return _character_animation_links(document, instance)


def _animation_tree_array_references(
    document: DbxDocument,
    names: set[str],
) -> tuple[str, ...]:
    references = []

    def visit(element: DbxElement, in_animation_list: bool = False):
        is_animation_list = (
            element.tag == "array"
            and (element.attribute("name") or "").casefold() in names
        )
        active = in_animation_list or is_animation_list
        if active:
            reference = element.attribute("ref")
            if reference and "/" in reference:
                references.append(reference)
        for child in element.children:
            visit(child, active)

    visit(document.root)
    return _unique_references(references)


def animation_tree_clip_references(
    document: DbxDocument,
) -> tuple[str, ...]:
    return _animation_tree_array_references(
        document,
        {
            "animationlist",
            "aimanimationlist",
            "replacmentanimations",
            "replacementanimations",
        },
    )


def animation_tree_replacement_references(
    document: DbxDocument,
) -> tuple[str, ...]:
    return _animation_tree_array_references(
        document,
        {
            "replacmentanimations",
            "replacementanimations",
        },
    )


def animation_tree_base_set_references(
    document: DbxDocument,
) -> tuple[str, ...]:
    return _animation_tree_array_references(
        document,
        {"basesetanimtrees"},
    )


def vehicle_part_state(name: str) -> str:
    tokens = tuple(
        token
        for token in re.split(r"[^a-z0-9]+", name.casefold())
        if token
    )
    normalized = "".join(tokens)
    if "undamaged" in normalized:
        return "DEFAULT"
    if (
        normalized.count("wreck") >= 2
        or any(
            marker in normalized
            for marker in ("destroy", "debris", "crash")
        )
    ):
        return "DESTROYED"
    if any(
        marker in normalized
        for marker in ("damage", "broken", "dmg")
    ):
        return "DAMAGED"
    if "blur" in normalized or "alternate" in normalized:
        return "ALTERNATE"
    return "DEFAULT"


def _vehicle_parts(
    document: DbxDocument,
    entity: DbxElement,
) -> tuple[AssemblyPart, ...]:
    parts: dict[str, AssemblyPart] = {}
    visited = set()
    priority = {
        "DEFAULT": 0,
        "ALTERNATE": 1,
        "DAMAGED": 2,
        "DESTROYED": 3,
    }

    def register(
        mesh_name: str,
        state: str,
        source: str,
        component: DbxElement,
    ):
        classified = vehicle_part_state(mesh_name)
        if priority[classified] > priority[state]:
            state = classified
            source = "name"
        key = mesh_name.casefold()
        existing = parts.get(key)
        if (
            existing is not None
            and priority[existing.state] >= priority[state]
        ):
            return
        parts[key] = AssemblyPart(
            name=mesh_name,
            state=state,
            source=source,
            component_type=component.attribute("type") or "<unknown>",
            component_name=_value(component, "Name"),
        )

    def visit(component: DbxElement):
        guid = component.attribute("guid")
        key = guid.casefold() if guid else str(id(component))
        if key in visited:
            return
        visited.add(key)

        health_states = []
        for reference in _array_references(component, "HealthStates"):
            state = document.instance(reference)
            if state is None:
                continue
            health_text = _value(state, "Health")
            try:
                health = float(health_text) if health_text else 0.0
            except ValueError as exc:
                raise CorruptDataError(
                    "Vehicle health-state threshold is not numeric"
                ) from exc
            health_states.append(
                (health, _value(state, "MeshName"))
            )
        if health_states:
            healthy_threshold = max(
                health for health, _ in health_states
            )
            for health, mesh_name in health_states:
                if mesh_name:
                    register(
                        mesh_name,
                        (
                            "DEFAULT"
                            if health == healthy_threshold
                            else "DAMAGED"
                        ),
                        "health_state",
                        component,
                    )

        for model in component.children_named("complex"):
            mesh_name = _value(model, "MeshName")
            if mesh_name is None:
                continue
            model_name = (model.attribute("name") or "").casefold()
            register(
                mesh_name,
                (
                    "ALTERNATE"
                    if any(
                        token in model_name
                        for token in ("high", "damage", "destroy")
                    )
                    else "DEFAULT"
                ),
                model.attribute("name") or "model",
                component,
            )

        for reference in _array_references(component, "Components"):
            child = document.instance(reference)
            if child is not None:
                visit(child)

    for reference in _array_references(entity, "Components"):
        component = document.instance(reference)
        if component is not None:
            visit(component)
    return tuple(parts.values())


def _mesh_set_path(reference: str) -> str:
    if "/" not in reference:
        raise CorruptDataError(
            f"Modular mesh reference has no instance GUID: {reference}"
        )
    partition, guid = reference.rsplit("/", 1)
    if not partition or not guid:
        raise CorruptDataError(f"Malformed modular mesh reference: {reference}")
    return partition + ".res"


def vehicle_entity_path_for_mesh_set(mesh_set_path: str) -> str | None:
    suffix = "_mesh.res"
    folded = mesh_set_path.casefold()
    if (
        not folded.startswith("objects/vehicles/")
        or not folded.endswith(suffix)
    ):
        return None
    return mesh_set_path[:-len(suffix)] + ".dbx"


def _component_role(path: str, assembly_kind: str) -> str:
    if assembly_kind == "VEHICLE":
        return "VEHICLE"
    stem = PurePosixPath(path).stem.casefold()
    if "headgear" in stem:
        return "HEADGEAR"
    if "head" in stem:
        return "HEAD"
    if "body" in stem:
        return "BODY"
    return "CHARACTER"


def _component(
    mesh_reference: str,
    assembly_kind: str,
    shader_set_reference: str | None = None,
    random_group: int | None = None,
    *,
    name: str | None = None,
    role: str | None = None,
    skeleton_reference: str | None = None,
    transform: FrostbiteTransform | None = None,
    source_type: str | None = None,
    source_guid: str | None = None,
) -> AssemblyComponent:
    path = _mesh_set_path(mesh_reference)
    return AssemblyComponent(
        name=name or PurePosixPath(path).stem,
        role=role or _component_role(path, assembly_kind),
        mesh_reference=mesh_reference,
        mesh_set_path=path,
        shader_set_reference=shader_set_reference,
        random_group=random_group,
        skeleton_reference=skeleton_reference,
        transform=transform,
        source_type=source_type,
        source_guid=source_guid,
    )


def _component_transform(component: DbxElement) -> FrostbiteTransform | None:
    value = component.child("complex", "Transform")
    if value is None or value.value is None:
        return None
    try:
        values = tuple(float(item) for item in value.value.split("/"))
    except ValueError as exc:
        raise CorruptDataError(
            "Vehicle component Transform contains a non-numeric value"
        ) from exc
    return FrostbiteTransform(values)


def _vehicle_track_components(
    document: DbxDocument,
    entity: DbxElement,
) -> tuple[AssemblyComponent, ...]:
    components = []
    visited = set()

    def visit(component: DbxElement):
        guid = component.attribute("guid")
        key = guid.casefold() if guid else str(id(component))
        if key in visited:
            return
        visited.add(key)

        component_type = component.attribute("type") or ""
        if component_type.endswith("TrackComponentData"):
            mesh_reference = _reference(component, "Mesh")
            if mesh_reference is not None:
                source_name = _value(component, "Name")
                components.append(
                    _component(
                        mesh_reference,
                        "VEHICLE",
                        name=(
                            source_name
                            if source_name
                            else f"Track {len(components) + 1:02d}"
                        ),
                        role="TRACK",
                        skeleton_reference=_reference(
                            component,
                            "Skeleton",
                        ),
                        transform=_component_transform(component),
                        source_type=component_type,
                        source_guid=guid,
                    )
                )

        for reference in _array_references(component, "Components"):
            child = document.instance(reference)
            if child is not None:
                visit(child)

    for reference in _array_references(entity, "Components"):
        component = document.instance(reference)
        if component is not None:
            visit(component)
    return tuple(components)


def _character_components(
    instance: DbxElement,
) -> tuple[AssemblyComponent, ...]:
    meshes = instance.child("array", "Meshes3p")
    if meshes is None:
        return ()
    components = []
    for entry in meshes.children_named("complex"):
        mesh_reference = _reference(entry, "Mesh")
        if mesh_reference is None:
            continue
        random_group_field = _field(entry, "RandomGroup")
        try:
            random_group = (
                int(random_group_field.value)
                if random_group_field is not None
                and random_group_field.value is not None
                else None
            )
        except ValueError as exc:
            raise CorruptDataError(
                "Character Meshes3p RandomGroup is not an integer"
            ) from exc
        components.append(
            _component(
                mesh_reference,
                "CHARACTER",
                _reference(entry, "MeshShaderSet"),
                random_group,
            )
        )
    return tuple(components)


def modular_assembly(
    document: DbxDocument,
    source_path: str,
) -> ModularAssembly | None:
    instance = document.primary_instance
    if instance is None:
        return None
    instance_type = instance.attribute("type") or ""
    name = (
        instance.attribute("id")
        or PurePosixPath(source_path).stem
    )

    character_components = _character_components(instance)
    if character_components:
        return ModularAssembly(
            name=name,
            kind="CHARACTER",
            source_path=source_path,
            components=character_components,
            animation_links=_character_animation_links(document, instance),
        )

    if instance_type.endswith("VehicleEntityData"):
        mesh_reference = _reference(instance, "Mesh")
        if mesh_reference is not None:
            components = (
                _component(
                    mesh_reference,
                    "VEHICLE",
                    _reference(instance, "MeshShaderSet"),
                    None,
                    source_type=instance_type,
                    source_guid=instance.attribute("guid"),
                ),
                *_vehicle_track_components(document, instance),
            )
            return ModularAssembly(
                name=name,
                kind="VEHICLE",
                source_path=source_path,
                components=components,
                parts=_vehicle_parts(document, instance),
            )
    return None
