# Resource type catalog

FbRB entries carry a type string independently of their path. The importer uses
that string for category selection and for a synthetic extension when a raw
resource is extracted.

This table distinguishes discovery from native conversion. A type appearing in
the browser is not necessarily decoded.

## Native or partially native imports

| FbRB type/path | Category | Current behavior |
|---|---|---|
| `MeshData` | Mesh | Native geometry, UV preview, and available weight groups |
| `RigidMeshSet` | Mesh | Resolves a naming-matched LOD MeshData, preferring LOD0. PC direct imports with a same-name DBX descriptor also resolve source surfaces and DX11 materials through the shared shader-key catalog; descriptorless undergrowth remains geometry with neutral materials and an explicit warning. Console imports are geometry-only |
| `SkinnedMeshSet` | Mesh | PC: resolves MeshData, four preserved character shader-coordinate channels with nondegenerate material routing, the DBX skeleton/default surface references, source DX11 materials, and a bound armature; console: geometry-only |
| `CompositeMeshSet` | Mesh | Resolves LOD0 MeshData and materials; rebases verified shared index streams and separates type-2 geometry into ordered source-named part collections. A vehicle `_Mesh.res` additionally imports its complete same-name entity assembly by default only after exact primary-mesh validation |
| `TreeMeshSet` | Mesh | Resolves MeshData. PC direct imports also resolve same-name descriptor surfaces and DX11 materials; no tree-specific wind or runtime vegetation semantics are implemented. Console imports are geometry-only |
| `WaterMesh` | Mesh | Native positions and triangle geometry |
| `ITexture` | Texture | PC resource-to-DDS conversion |
| `XenonTexture` | Texture | Xbox untile, byte correction, and DDS conversion |
| `TerrainHeightfield` | Terrain | Downsampled native preview mesh |
| soldier entity `.dbx` with `Meshes3p` | Level / DBX | PC: imports every linked LOD0 body/head/headgear module on one shared armature with source materials and modular collections |
| vehicle entity `.dbx` with a composite `Mesh` | Level / DBX | Imports the linked LOD0 composite into named source-part collections with source materials; PC `TrackComponentData` records additionally instantiate their shared skinned mesh and explicit skeleton at each exact source transform |
| other non-resource path ending `.dbx` | Level / DBX | Implemented binary DBX subset converted to an XML Text datablock for inspection; unrecognized fields are not native Blender behavior and XML-to-DBX writing is unsupported |
| PC level DBX graph | Level / DBX | Complete static map assembly with direct/grouped placements, terrain, water, and DX11-bound materials |
| `Dx11ShaderDatabase` | Other | Material keys and indexed texture bindings parsed during PC complete-map and skinned-character import |
| `GrannyModel` | Animation | PC little-endian skeleton converted to a native armature |
| `GrannyAnimation` | Animation | PC transform tracks converted to a Blender 5.2 Action on the matching armature |

The texture dispatcher also recognizes `Ps3Texture`, but PS3 disc media and
texture variants were not part of this validation effort. It must not be
advertised as verified support.

Later BF3/BF4 MaxScripts use object type 1 for skinned meshes and add a
separate object-type-2 vehicle-part path. BC2 retail investigation now
independently verifies this high-level separation, but not the later scripts'
numeric offsets or layouts. BC2 part IDs, shared index rebasing, and ordered
LOD names are documented in [Meshes](meshes.md). The scripts provide no
type-4 tree semantics or evidence for other catalog types. See the
[external MaxScript audit](../maxscript-reference-audit.md).

High-level DBX assembly dispatch is intentionally deterministic. Selecting the
soldier or vehicle entity imports its full linked assembly. A direct
`SkinnedMeshSet` remains focused. A direct vehicle `CompositeMeshSet` ending
in `_Mesh.res` checks only the same-folder, same-name `.dbx`; it imports the
complete assembly by default only if that document resolves as a vehicle and
its primary component references the selected mesh exactly. The user can
disable **Import linked vehicle assembly** to keep every direct composite
selection focused. The importer does not perform an arbitrary reverse search
because one body mesh can participate in multiple entities. Selecting a track
`SkinnedMeshSet` also remains focused, while the validated main vehicle graph
supplies repeated track instances and placement.

Direct PC `RigidMeshSet` and `TreeMeshSet` selections use only their
same-partition `.dbx` descriptor for source material references. A catalog
survey found this deterministic descriptor for 1,175 of 1,295 unique rigid
mesh sets and all 57 tree mesh sets; all 1,232 parsed successfully. The 120
descriptorless rigid resources were undergrowth in the tested retail source
and remain geometry-only with an explicit warning. Direct `MeshData` does not
guess an owner through reverse lookup. See
[Shaders and materials](../shaders-and-materials.md) for source-surface and
shader-key coverage.

The generic DBX reader and the semantic importers have different support
boundaries. See [Terrain and DBX](terrain-and-dbx.md) for the binary grammar,
the exact fields interpreted by Blender, and the major instance families that
remain XML-only or ignored.

## Cataloged but extracted raw

| Group | Known type strings |
|---|---|
| Mesh-like | `OccluderMesh`, `SootMesh` |
| Terrain/environment | `Terrain`, `VisualTerrain`, `VisualWater`, `IrradianceVolume`, `TerrainMaterialMap`, `WeatherSystem` |
| Animation/model | `AimAnimation`, `AnimTreeInfo`, `DeltaAnimation`, `RagdollResource` |
| Audio | `Wave`, `ImpulseResponse` |
| UI/video | `SwfMovie`, `BinkMemory` |
| Physics/other | `HavokPhysicsData`, `Dx9ShaderDatabase` |
| Non-resource files | `.dbmanifest`, `.xml`, and other original suffixes |

Unknown type strings are retained and categorized as Other. Raw extraction
uses the original path suffix when possible, otherwise a known synthetic
extension or `.bin`.

## Known synthetic extensions

The current mapping includes:

```text
AimAnimation       .aimanimation
AnimTreeInfo       .animtreeinfo
BinkMemory         .binkmemory
CompositeMeshSet   .compositemeshset
DeltaAnimation     .deltaanimation
Dx9ShaderDatabase  .dx9shaderdatabase
GrannyAnimation    .grannyanimation
GrannyModel        .grannymodel
HavokPhysicsData   .havokphysicsdata
ImpulseResponse    .impulseresponse
ITexture           .itexture
IrradianceVolume   .irradiancevolume
MeshData           .meshdata
OccluderMesh       .occludermesh
RagdollResource    .ragdollresource
RigidMeshSet       .rigidmeshset
SkinnedMeshSet     .skinnedmeshset
SootMesh           .sootmesh
SwfMovie           .swfmovie
Terrain            .terrain
TerrainHeightfield .terrainheightfield
TerrainMaterialMap .terrainmaterialmap
TreeMeshSet        .treemeshset
VisualTerrain      .visualterrain
VisualWater        .visualwater
WaterMesh          .watermesh
Wave               .wave
WeatherSystem      .weathersystem
XenonTexture       .xenontexture
```

These extensions are cache conveniences, not proof that the game uses them on
disc or that another tool will recognize them.

See [`fbrb.py`](../../bc2_frostbite_importer/core/fbrb.py) for the authoritative
category and extension maps, and
[`blender_addon.py`](../../bc2_frostbite_importer/blender_addon.py) for native
dispatch behavior.
