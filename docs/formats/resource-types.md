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
| `RigidMeshSet` | Mesh | Resolves a naming-matched LOD MeshData, preferring LOD0 |
| `SkinnedMeshSet` | Mesh | PC: resolves MeshData, four preserved character shader-coordinate channels with nondegenerate material routing, the DBX skeleton/default surface references, source DX11 materials, and a bound armature; console: geometry-only |
| `CompositeMeshSet` | Mesh | Resolves MeshData; no part hierarchy decoding |
| `TreeMeshSet` | Mesh | Resolves MeshData; no tree-specific semantics |
| `WaterMesh` | Mesh | Native positions and triangle geometry |
| `ITexture` | Texture | PC resource-to-DDS conversion |
| `XenonTexture` | Texture | Xbox untile, byte correction, and DDS conversion |
| `TerrainHeightfield` | Terrain | Downsampled native preview mesh |
| non-resource path ending `.dbx` | Level / DBX | Binary DBX converted to an XML Text datablock |
| PC level DBX graph | Level / DBX | Complete static map assembly with direct/grouped placements, terrain, water, and DX11-bound materials |
| `Dx11ShaderDatabase` | Other | Material keys and indexed texture bindings parsed during PC complete-map and skinned-character import |
| `GrannyModel` | Animation | PC little-endian skeleton converted to a native armature |
| `GrannyAnimation` | Animation | PC transform tracks converted to a Blender 5.2 Action on the matching armature |

The texture dispatcher also recognizes `Ps3Texture`, but PS3 disc media and
texture variants were not part of this validation effort. It must not be
advertised as verified support.

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
