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
object-type-2 mesh detachment is only a mesh-local vehicle-part hypothesis and
does not establish BC2 placement or instancing semantics. See the
[external MaxScript audit](../maxscript-reference-audit.md).

## Required PC archives

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
  detail textures. Exact terrain shader constants, decals, mask regions,
  blend arithmetic, and atlas masks are not decoded.
- Water geometry imports, but its material is an explicit approximation.
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
