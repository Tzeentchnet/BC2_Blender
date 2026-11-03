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
| `RigidMeshSet` | Mesh | Resolves a naming-matched LOD MeshData, preferring LOD0. PC and BF1943 STFS direct imports with a same-name DBX descriptor resolve source surfaces through the shared shader-key catalog; PC uses DX11 materials and BF1943 uses version-117 Xbox materials. Descriptorless resources remain geometry with neutral materials and an explicit warning. Xbox BC2 and PS3 imports are geometry-only |
| `SkinnedMeshSet` | Mesh | PC: resolves MeshData, preserved character shader-coordinate channels, the DBX skeleton/default surface references, source DX11 materials, and a bound armature. BF1943: resolves the verified Xbox Granny skeleton, topology-selected coordinates, and version-117 source materials. Xbox BC2 and PS3 remain geometry-only |
| `CompositeMeshSet` | Mesh | Resolves LOD0 MeshData and materials; rebases verified shared index streams and separates type-2 geometry into ordered source-named part collections. A vehicle `_Mesh.res` additionally imports its complete same-name entity assembly by default only after exact primary-mesh validation |
| `TreeMeshSet` | Mesh | Resolves MeshData. PC and BF1943 STFS direct imports can resolve same-name descriptor surfaces through their platform shader catalogs; no tree-specific wind or runtime vegetation semantics are implemented. Xbox BC2 and PS3 imports are geometry-only |
| `WaterMesh` | Mesh | Native positions and triangle geometry |
| `ITexture` | Texture | PC resource-to-DDS conversion |
| `XenonTexture` | Texture | Xbox untile, byte correction, and DDS conversion |
| `TerrainHeightfield` | Terrain | Downsampled native preview mesh |
| soldier entity `.dbx` with `Meshes3p` | Level / DBX | PC: imports every linked LOD0 body/head/headgear module on one shared armature with source materials and modular collections |
| vehicle entity `.dbx` with a composite `Mesh` | Level / DBX | Imports the linked LOD0 composite into named source-part collections with source materials; PC `TrackComponentData` records additionally instantiate their shared skinned mesh and explicit skeleton at each exact source transform |
| other non-resource path ending `.dbx` | Level / DBX | Implemented binary DBX subset converted to an XML Text datablock for inspection; unrecognized fields are not native Blender behavior and XML-to-DBX writing is unsupported |
| PC/BF1943 level DBX graph | Level / DBX | Complete static map assembly with direct/grouped placements, terrain, and water; PC uses DX11 materials while BF1943 uses verified version-117 Xbox material keys and texture bindings with explicit Xenos-bytecode/UV-routing limitations |
| `Dx11ShaderDatabase` | Other | Material keys and indexed texture bindings parsed during PC complete-map and skinned-character import |
| BF1943 `Dx9ShaderDatabase` | Other | Big-endian version-117 section graph, material keys, source binding values, and texture paths parsed for complete-map and direct mesh-set materials; compiled Xbox bytecode and exact UV routing remain unsupported |
| `GrannyModel` | Animation | PC little-endian or verified BF1943 big-endian skeleton converted to a native armature |
| `GrannyAnimation` | Animation | PC or verified BF1943 transform tracks converted to a Blender 5.2 Action on the matching armature |

Several parsers contain PS3-oriented recognition paths: big-endian MeshData,
composite-part, WaterMesh, TerrainHeightfield, TerrainMaterialMap, and texture
header handling, plus the `Ps3Texture` type. PS3 disc access, storage variants,
and end-to-end Blender imports have not been validated because the maintainer
does not own the PS3 release. Recognition must not be advertised as verified
PS3 support.

BC2 retail investigation independently verifies the high-level separation
between type-1 skinning and type-2 vehicle-part membership. BC2 part IDs,
shared-index rebasing, ordered LOD names, and the remaining declaration
boundaries are documented in [Meshes](meshes.md). Type-4 tree runtime
semantics and most other catalog types remain unverified.

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
| Physics/other | `HavokPhysicsData` |
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
