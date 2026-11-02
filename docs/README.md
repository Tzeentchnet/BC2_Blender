# Bad Company 2 format notes

This directory records the format knowledge used by the Blender importer. It is
intended for people implementing another importer, extending this one, or
planning a conservative exporter.

The notes describe observed Battlefield: Bad Company 2 retail data and the
behavior implemented in this repository. Frostbite 1 games and builds can
differ. Do not assume that an observed field is universal without testing more
samples.

## Confidence labels

The documents use these terms deliberately:

- **Verified**: checked against supplied retail media, decoded output, or both.
- **Implemented**: behavior covered by the current parser and usually by a
  synthetic regression test.
- **Heuristic**: works on tested assets but is not a complete format
  specification.
- **Unknown**: not sufficiently understood for safe writing.

## Reading order

| Topic | Document |
|---|---|
| PC ISO, Windows Installer, and cabinets | [PC platform](platforms/pc.md) |
| Xbox 360 XGD2/XDVDFS layout | [Xbox 360 platform](platforms/xbox-360.md) |
| Cross-platform Frostbite archives | [FbRB archives](formats/fbrb.md) |
| Resource type names and current support | [Resource type catalog](formats/resource-types.md) |
| MeshData, mesh sets, and WaterMesh | [Meshes](formats/meshes.md) |
| Granny skeletons, skinning, and transform animation | [Granny characters and animation](formats/granny-animation.md) |
| ITexture, XenonTexture, tiling, and DDS | [Textures](formats/textures.md) |
| TerrainHeightfield, TerrainMaterialMap, binary DBX grammar, and semantic support boundaries | [Terrain and DBX](formats/terrain-and-dbx.md) |
| Level graphs, transforms, instancing, and building destruction research | [Levels and complete maps](formats/levels-and-maps.md) |
| Shader database findings and normal decoding | [Shaders and materials](shaders-and-materials.md) |
| BF3/BF4 MaxScript evidence and applicability limits | [External MaxScript reference audit](maxscript-reference-audit.md) |
| BC2-oriented external writers and export constraints | [External writer reference audit](external-writer-reference-audit.md) |
| NumPy acceleration and native Blender image loading | [Import performance](import-performance.md) |
| Safe importer/exporter architecture | [Import and export design](import-export-design.md) |
| Evidence, test coverage, and open questions | [Validation and unknowns](validation-and-unknowns.md) |

## Data path

The supported read path is:

```text
PC ISO9660 -> AUTORUN.DAT (MSI) -> CAB -> FbRB -> resource
Xbox XGD2 -> XDVDFS ---------------------> FbRB -> resource
```

Both paths converge at the FbRB parser. The importer then catalogs active
entries in SQLite and dispatches selected resources to mesh, texture, terrain,
water, Granny skeleton/animation, modular entity assembly, or DBX readers.

The source media is always read-only. Cached cabinets, extracted archives,
resources, and converted DDS files are disposable derivatives.

## Implementation map

| Area | Implementation |
|---|---|
| ISO9660 | [`core/iso9660.py`](../bc2_frostbite_importer/core/iso9660.py) |
| XDVDFS | [`core/xdvdfs.py`](../bc2_frostbite_importer/core/xdvdfs.py) |
| MSI mapping | [`core/msi.py`](../bc2_frostbite_importer/core/msi.py) |
| Platform source abstraction | [`core/source.py`](../bc2_frostbite_importer/core/source.py) |
| FbRB | [`core/fbrb.py`](../bc2_frostbite_importer/core/fbrb.py) |
| SQLite catalog | [`core/catalog.py`](../bc2_frostbite_importer/core/catalog.py) |
| Mesh and water | [`core/mesh.py`](../bc2_frostbite_importer/core/mesh.py) |
| Granny skeleton and animation | [`core/granny.py`](../bc2_frostbite_importer/core/granny.py) |
| Texture conversion | [`core/texture.py`](../bc2_frostbite_importer/core/texture.py) |
| Terrain | [`core/terrain.py`](../bc2_frostbite_importer/core/terrain.py) |
| DBX | [`core/dbx.py`](../bc2_frostbite_importer/core/dbx.py) |
| Character/vehicle assembly links | [`core/assembly.py`](../bc2_frostbite_importer/core/assembly.py) |
| Level resolution | [`core/level.py`](../bc2_frostbite_importer/core/level.py) |
| DX11 shader database | [`core/shaderdb.py`](../bc2_frostbite_importer/core/shaderdb.py) |
| Blender conversion | [`blender_addon.py`](../bc2_frostbite_importer/blender_addon.py) |
| Regression fixtures | [`tests/test_core.py`](../tests/test_core.py) |

## General rules for format work

1. Select byte order from the platform when it is known. Use content
   heuristics only as a recovery path.
2. Bound every count, offset, and allocation before reading it.
3. Keep container offsets distinct from offsets into decompressed payloads.
4. Treat texture storage and texture meaning as separate problems. A DXT5
   resource is not necessarily an RGBA color texture.
5. Preserve unknown bytes and original metadata before attempting export.
6. Record whether a conclusion came from code, synthetic fixtures, or a real
   retail sample.
7. Never commit disc images, extracted game assets, shader bytecode, or other
   copyrighted payloads to this repository.
8. Treat later-Frostbite tools as corroborating references only. Record which
   principles transfer, which findings are heuristic, and which versioned
   offsets or DCC choices are unsafe for BC2. Treat BC2-oriented external
   writers the same way until no-edit and in-game validation succeeds. See the
   [MaxScript audit](maxscript-reference-audit.md) and
   [writer audit](external-writer-reference-audit.md).
