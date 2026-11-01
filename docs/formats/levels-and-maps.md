# Levels and complete-map assembly

## Scope

The implemented whole-map path targets the **PC retail** build and has been
validated against `MP_001`. It imports the verified static visual foundation:

- direct blueprint placements;
- Forest, Middlegrowth, Rigid, and Static compact model groups;
- all indexed terrain heightfields;
- the level WaterMesh;
- mesh surface sets and texture bindings from the level DX11 shader database.

Dynamic game logic, effects, weather, collision, AI, destruction state, audio,
and runtime culling are not reconstructed as Blender scene behavior.

The external BF3/BF4 MaxScripts do not parse a level DBX graph, placement
matrix, compact group, blueprint, or instance record. Their optional
object-type-2 mesh detachment does not establish BC2 placement or instancing
semantics. Composite membership is now independently verified from BC2 retail
data, but it remains mesh-local. See the
[external MaxScript audit](../maxscript-reference-audit.md).

## Modular entity assembly versus map placement

High-level soldier and vehicle DBX imports are separate from complete-map
placement resolution. They follow deterministic asset links but do not search
a level graph for transforms.

Character assembly uses:

```text
GameSharedResources.SoldierEntityData
  Meshes3p[]
    Mesh -> Render.SkinnedMeshAsset
    MeshShaderSet -> optional component override
    RandomGroup -> required/optional grouping metadata
```

Each linked component resolves its own LOD0 MeshData and materials. The
importer requires every module to reference the same skeleton, creates that
armature once, and binds body, head, and headgear to it. All modules remain
visible; `RandomGroup` is preserved as collection metadata for user-controlled
mix-and-match rather than interpreted as a runtime randomizer.

Vehicle assembly uses:

```text
GameSharedResources.VehicleEntityData
  Mesh -> Render.CompositeMeshAsset
  Components[] -> nested vehicle components
    HealthStates[] -> Health + MeshName
    LowRpmModel/HighRpmModel -> MeshName
```

The composite LOD0 is split using verified type-2 global part IDs and ordered
mesh-set part records. Each record supplies local bounds and a 3x3 plus
translation bind transform, which places authored local geometry into the
retail vehicle bind pose. Nested DBX health states mark the highest-health
named mesh as the default and lower-health named meshes as damaged;
low-/high-RPM records keep the static rotor visible and the blurred alternative
hidden. Explicit destroyed/damaged names provide a conservative fallback for
components whose state graph names only the destruction mesh. `Wreck` must
occur at least twice in one name to trigger that fallback; a single occurrence
often identifies a destructible vehicle section rather than destroyed
geometry.

Non-default collections are preserved with their source transforms and
metadata but hidden in both viewport and render. This creates an editable
retail-like default assembly, not a vehicle rig: runtime pivots, parent links,
automatic damage switching, rotor animation, and destruction animation remain
unknown and are not synthesized. Geometry objects are also checked
independently after part splitting: a source, object, or mesh name containing
at least two case-insensitive `Wreck` occurrences is hidden in the viewport
and render even if its part ID places the fragment in an otherwise healthy
collection. Occurrences are counted within each individual name rather than
summed across duplicate metadata names. A single occurrence does not trigger
object- or collection-level hiding by name. A collection can still be hidden
by an independently decoded damage or alternate state.

Selecting a high-level entity `.dbx` invokes these assembly paths. Selecting a
component mesh set remains a focused component import. Other DBX documents
still open as XML Text datablocks. This avoids ambiguous reverse-reference
selection when one body or vehicle component is shared by multiple entities.

## Research: PC building destruction assemblies

**Verified scope:** PC retail `MP_001` level and `mp_common` archives. A survey
of `Objects/Buildings` found 37 DBX documents with part components, including
standalone subcomponents. This is not a survey of the whole game or evidence
for Xbox 360 behavior.

The PC `IND_Warehouse` blueprint, used by 21 direct `MP_001` placements,
contains an authored destruction graph:

```text
Entity.ObjectBlueprint.Object
  -> GameSharedResources.StaticModelEntityData
       Mesh -> Render.CompositeMeshAsset -> CompositeMeshSet and LOD0 MeshData
       Components[] -> GameSharedResources.PartComponentData
         HealthStates[] -> GameSharedResources.HealthStateData
           Health, MeshName, Effects[]
       PartLinks[] -> GameSharedResources.PartLinkData
```

In this sample, the component array has 57 parts and the part-link array has
112 links. Both endpoint indices on every link match the corresponding
component names in `Components[]`, for 224 of 224 endpoints. These component
indices are not the global mesh part IDs. State `MeshName` values instead
match ordered mesh-set part names case-insensitively.

| PC building sample | Components | Distinct named mesh swaps | Empty lowest-health mesh names | Single-state parts |
|---|---:|---:|---:|---:|
| `IND_Warehouse` | 57 | 20 | 36 | 1 |
| `House_09_15x12` | 21 | 20 | 0 | 1 |
| `IND_SmallFactory` | 107 | 0 | 106 | 1 |

For the warehouse, all 77 named health-state meshes match the 77 LOD0
mesh-set records and have triangles in the decoded type-2 MeshData. All seven
visible subsets split into valid part IDs, with no out-of-range IDs or
unmatched named states. All 77 recorded bind transforms are identity in
this particular sample; other buildings must not be assumed to share that
property. The separate `House_09_15x12` and `IND_SmallFactory` samples also
matched all their named states to parts with triangles.

For example, warehouse `RoofPart_4` names `RoofPart_4` at health `100` and
`RoofPart_4_dest` at health `0`. The latter state references smoke and metal
debris effects. Other components instead have an empty `MeshName` at health
`0`; a preview can represent those states as no mesh, rather than inventing a
replacement. The single `ruin` component has only one named state, at health
`0`. The other highest thresholds are `1` for 18 parts and `100` for 38 parts;
do not assume that `100` is the universal healthy threshold or that `0` alone
means a mesh is hidden.

**Prototype validation:** a local Blender 5.2.2 scene was assembled from one
actual `MP_001` warehouse placement. It was saved and reopened with separate
highest-health and lowest-health views, and an individual-state view containing
all 113 health-state collections. Switching one named swap and one empty
state was checked after reopening. This is an analytical preview, not a
shipped importer feature or proof that all lowest-health states can occur
simultaneously in game. It uses neutral materials and does not recreate
particle timing, physics, or collapse behavior.

The prototype applies each mesh-set bind transform before the level placement:
`world = level_placement @ part_bind @ vertex`, with the existing
Frostbite-to-Blender basis conversion. The warehouse's sampled bind transforms
are identity; its level placement includes rotation and translation. Mesh-set
part records contain 18 little-endian `float32` values immediately after the
NUL-terminated LOD part name, as detailed in [Meshes](meshes.md). The DBX
fields above are semantic fields in a variable-length element stream, not
fixed-offset structures. DBX numeric arrays use big-endian elements of their
encoded width; the current reader does not preserve the exact raw type and
width of each decoded health value.

**Unknown:** runtime threshold comparison, the initial visibility of
single-state parts, the meaning and timing of whole-building collapse,
part-link propagation, spawned physics, effect timing, and Xbox 360
equivalence. The warehouse stores `DestroyRuinPartPercentage = 75` and
`ManyPartsDestroyedPercentage = 80`, but their runtime semantics are not
established. Effects referenced by a health state corroborate a relationship
between damage and effects; they do not prove that particles conceal a
transition. The current whole-map importer does not select building health
states, and standalone composite imports do not interpret this building DBX
graph. These previews are not an exporter-safe destruction specification.

## Required PC archives

A PC level is offered in the add-on's map picker when the MSI archive catalog
contains both `Dist/win32/levels/<name>/level-00.fbrb` and
`terrain-00.fbrb`. This implemented discovery rule found 33 candidates on the
validated retail disc without extracting their FbRB contents. It is only a
selection heuristic; it does not establish that every candidate's DBX graph is
supported.

A multiplayer level is not self-contained. `MP_001` requires these indexed
archives:

```text
Dist/win32/levels/mp_001/level-00.fbrb
Dist/win32/levels/mp_001/terrain-00.fbrb
Dist/win32/levels/mp_common/level-00.fbrb
```

The shared `mp_common` archive is required for common blueprints, meshes, and
textures. For the tested disc it is about 300 MB and contains 15,861 active
assets. Level-specific archives alone resolve only 831 direct placement
occurrences; including `mp_common` resolves all verified static visuals.

All three tested map archives have gzip-compressed FbRB payloads. Seeking an
entry inside such a stream otherwise requires inflating from byte zero for
every read. Whole-map preparation therefore materializes each decompressed
payload once in the source-fingerprint cache and performs later resource reads
as direct bounded seeks.

For `MP_001`, the three decompressed payloads total about 730 MB:

| Archive | Decompressed payload |
|---|---:|
| level | 136,168,592 bytes |
| terrain | 5,691,061 bytes |
| `mp_common` | 587,886,688 bytes |

The repository also caches parsed FbRB metadata per archive while it remains
open. Both caches are disposable derivatives of the source ISO.

## Main level document

The entry point is:

```text
Levels/<LEVEL_NAME>.dbx
```

For `MP_001`, the primary `LevelData` instance has 3,344 local object
references. Every local reference resolves. Of these, 2,485 are
`Entity.ReferenceObjectData` instances.

### Direct placement chain

```text
LevelData.Objects
  -> Entity.ReferenceObjectData
  -> ReferencedObject external path/GUID
  -> Entity.ObjectBlueprint.Object
  -> Static/Dynamic/Rigid/Ladder model entity
  -> Mesh external path/GUID
  -> Render mesh partition
  -> LOD0 MeshData
```

The direct path produces:

| Outcome | Count |
|---|---:|
| Visual placements | 2,461 |
| Nonvisual effect/weather references | 24 |
| Unique referenced blueprint partitions | 144 |
| Unique direct mesh references | 131 |

The 24 nonvisual references are reported in import diagnostics rather than
represented by success-shaped placeholder meshes.

An entity or placement can provide a `MeshShaderSet` override. That override is
part of the resolved mesh-variant key because identical geometry can use
different surface sets. A tested override can be sparse rather than a complete
replacement: the resolver starts with the mesh's default shader set and
replaces surfaces whose normalized names occur in the selected override,
preserving unmatched defaults. Override-only names are appended.

`IND_SmallFactory` demonstrates why this matters. Its placement override
contains only three `cheap_metal` LOD variants, while the default set supplies
Glass, Template, Wall, Metal Light, grate, Wood, Metal Diamond, Concrete,
Ground, Corrugated Metal, and their LOD variants. Replacing the default set
left eight of eleven visible LOD0 subsets magenta. Merging the sparse override
resolves all eleven while retaining the intended metal substitutions.

## Compact model groups

Four level entity types store many placements compactly:

| Entity type | Member types | Placements |
|---|---:|---:|
| `GameSharedResources.ForestEntityData` | 6 | 878 |
| `GameSharedResources.MiddlegrowthModelGroupEntityData` | 3 | 390 |
| `GameSharedResources.RigidModelGroupEntityData` | 2 | 97 |
| `GameSharedResources.StaticModelGroupEntityData` | 39 | 1,554 |
| **Total** | **50** | **2,919** |

The level DBX stores member definitions and `SpawnDatas`. A related external
Composite Havok DBX stores parallel arrays:

```text
Transforms
Assets
Enums
```

The verified ordering rule is:

1. Iterate `MemberDatas` in serialized DBX order.
2. For each member, iterate its `SpawnDatas` in serialized order.
3. Pair that flattened spawn with the same-index Havok transform, asset, and
   enumeration.

Across all four tested groups, the flattened DBX enumeration sequence exactly
matches the Havok `Enums` array. Transform, asset, and enumeration counts also
match the spawn count. The resolver rejects a mismatch instead of guessing.
Enumerations are unique and cover 1 through 2,919, but each individual group's
serialized order is not necessarily numerically sorted.

## Placement transforms

A placement transform is a 16-value column-major numeric DBX field. Its source
matrix is reconstructed as:

```text
| value[0]  value[4]  value[8]   value[12] |
| value[1]  value[5]  value[9]   value[13] |
| value[2]  value[6]  value[10]  value[14] |
| value[3]  value[7]  value[11]  value[15] |
```

The source translation is therefore
`(value[12], value[13], value[14])`. Earlier importer versions exchanged X and
Z in both the basis and translation. Serialized identity then became a
negative-determinant reflection, mirroring readable texture content and
placing objects at the wrong terrain coordinate.

The default Y-up to Blender Z-up conversion applies:

```text
(x, y, z) -> (x, -z, y)
```

to both the basis and translation. It is a proper rotation with determinant
`+1`; it does not reflect the source. The complete-map regression verifies
positive world-matrix determinants for the reported GasSilo, Silo, and
SmallFactory placements.

TerrainHeightfield bounds and WaterMesh vertices are already serialized in the
same level coordinate frame as visual placements. `MP_001` also serializes a
terrain entity translation of approximately `(0, -89.2776, 0)`, but applying
that value moved terrain and water down a second time. The importer therefore
retains the converted source translation as custom metadata and deliberately
does not apply it to either geometry type.

This is validated geometrically rather than by an arbitrary corrective offset:
after import, all 878 forest placement origins have terrain hits and their
median vertical difference is approximately `-1.09` Blender units. Applying the
serialized translation produced a median difference of approximately `91.47`.
Water remains at its source elevation, approximately `130.5..133.25`.

Three targeted placement origins now agree with the full-resolution central
heightfield within one meter:

| Enumeration | Asset | Vertical offset |
|---:|---|---:|
| 4,419 | GasSilo | approximately `-0.44` |
| 4,808 | IND_Silo_Medium | approximately `+0.07` |
| 5,777 | IND_SmallFactory | approximately `+0.15` |

## Mesh and instance strategy

The verified `MP_001` static visual foundation contains:

| Item | Count |
|---|---:|
| Direct visual placements | 2,461 |
| Compact-group placements | 2,919 |
| **Total placed objects** | **5,380** |
| Unique MeshData resources | 178 |
| Resolved mesh/material variants | 211 |
| Unique decoded vertices | 378,856 |
| Unique decoded triangles | 247,147 |

All 178 LOD0 resources parse. Observed vertex strides are 32, 48, 64, 68, 72,
and 76 bytes. Position encoding is selected once for all related subsets with
the same stride in a MeshData resource. This prevents one ambiguous subset from
being decoded as floats while sibling subsets use half-floats. The validated
bridge and building-block regressions otherwise expanded roughly 71 m and
10 m assets to tens of kilometers.

The Blender adapter parses each MeshData path once. It combines visible subsets
into one mesh datablock, retains a material slot per subset, and creates one
datablock per resolved geometry/shader-set variant. Every placement using that
variant links to the same datablock. This preserves instancing instead of
duplicating geometry 5,380 times.

Subset-to-surface matching uses:

1. normalized exact subset/surface names;
2. the longest normalized surface name that is a suffix of the subset name;
3. the longest normalized surface name contained in the subset name;
4. a single-subset/single-surface fallback;
5. serialized index matching only when subset and surface counts agree;
6. an explicit diagnostic material when no match is defensible.

The chosen match method and shader key are stored on each mesh datablock.

In the current `MP_001` validation, all 485 visible subset slots resolve by
source naming:

| Match method | Subsets |
|---|---:|
| Longest suffix | 485 |
| Explicitly unresolved | 0 |

No index fallback was needed for this map. Sparse override merging supplies
the default surfaces that were previously missing from building variants.

The real-media validation also asserts that no placed object's world extent
exceeds 4,000 units. The largest remaining objects are explicitly named
backdrop/skyline assets; the corrected scene contains no accidental
kilometer-scale ordinary prop caused by mixed position decoding.

## Blender collection layout

A whole-map import creates:

```text
BC2_<LEVEL>
  Direct
  Forest
  Middlegrowth
  Rigid Group
  Static Group
  Terrain
  Water
  Diagnostics
```

The Diagnostics collection contains an empty pointing to a JSON Text datablock.
That text records counts and all unresolved nonvisual, shader, texture, and
surface issues. Source references, entity types, enumerations, and the original
16 transform values are retained as custom properties on placed objects.

The saved validation scene reopens with these collection counts:

| Collection | Objects |
|---|---:|
| Direct | 2,461 |
| Forest | 878 |
| Middlegrowth | 390 |
| Rigid Group | 97 |
| Static Group | 1,554 |
| Terrain | 13 |
| Water | 1 |
| Diagnostics | 1 |

## Remaining map work

- Terrain physical-material indices are decoded and approximated with source
  detail textures. Full/group/spline decal records and their authoring mask
  bounds are now identified, but compiled `VisualTerrain` records, atlas
  regions, exact shader constants, and blend arithmetic are not decoded.
- Water geometry imports with its complete 32-bit topology, including the
  `MP_001` harbor. Its material is an explicit approximation.
- Shader binding recovery does not yet recover every shader constant,
  arithmetic operation, UV source, swizzle, or permutation.
- Static cull trees are visibility structures and are not required for the
  verified placement set; importing them as visibility metadata is optional
  future work.
- Destructible states, collision, lights, particles, weather, sound, animation,
  and game logic remain outside the current static scene foundation.

See also [Terrain and DBX](terrain-and-dbx.md),
[Shaders and materials](../shaders-and-materials.md), and
[`level.py`](../../bc2_frostbite_importer/core/level.py).
