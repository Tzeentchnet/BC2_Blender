# Validation and unknowns

## Real-media observations

The implementation has been exercised against supplied PC and Xbox 360 retail
images.

### Xbox catalog

| Category | Active assets |
|---|---:|
| Animation | 11,749 |
| Audio | 117,739 |
| Data | 35,019 |
| Level / DBX | 69,688 |
| Mesh | 9,635 |
| Other | 2,134 |
| Terrain | 473 |
| Texture | 4,704 |
| UI / Video | 110 |
| **Total** | **251,251** |

The disc contained 630 FbRB archives and 384,241 total metadata records when
deleted entries were included.

### PC catalog

The tested MSI exposed 625 unique extractable FbRB archives. Archive indexing
was validated on demand, including the MedKit mesh and shader-related assets.

### PC `MP_001` complete-map foundation

The level resolver was run against the retail PC media with the level,
terrain, and `mp_common` archives indexed:

| Measurement | Result |
|---|---:|
| Direct visual placements | 2,461 |
| Compact-group placements | 2,919 |
| Total static visual placements | 5,380 |
| Unique MeshData resources | 178 |
| Mesh/shader-set variants | 211 |
| Terrain heightfields | 13 |
| Water meshes | 1 |
| Expected nonvisual references | 24 |
| Unexpected unresolved geometry | 0 |

Every one of the 178 LOD0 MeshData resources parsed. The unique geometry totals
378,856 decoded vertices and 247,147 triangles. All group spawn counts,
transform counts, asset counts, and serialized enumeration order agree.

The level DX11 database parser recovered 8,222 unique material keys from a
26,905,158-byte resource with no trailing bytes.

The final Blender 5.2 integration scene was saved and reopened with:

| Measurement | Result |
|---|---:|
| Linked placement objects | 5,380 |
| Linked mesh datablocks | 211 |
| Resolved shader database keys | 418 exact matches |
| Connected Base Color graphs | 417 |
| Native file-backed images available after reopen | 185 |
| Matched visible subset slots | 485 of 485 |
| Explicit unresolved subset slots | 0 |
| Missing shader database keys | 0 |
| Terrain world footprint | `-1024..1024` on Blender X and Y |
| Terrain world elevation | approximately `0..482.43` on Blender Z |
| Central heightfield vertices | 1,048,576 |
| Median forest-origin terrain offset | approximately `-1.09` Blender units |
| Water world elevation | approximately `130.5..133.25` on Blender Z |

The diagnostics Text has ten deduplicated nonvisual-reference descriptions
covering the 24 raw nonvisual placements. There were no missing-shader,
missing-texture, texture-conversion, or unresolved-subset diagnostics.

## Verified resource samples

- PC and Xbox MedKit MeshData: 3,378 vertices and 3,130 triangles each, with
  matching decoded geometry and UVs.
- Xbox tiled L8 texture: untiled and decoded through DDS.
- Xbox DXT5 texture: untiled, 16-bit block-swapped, and decoded.
- PC L8 ITexture: converted and decoded through DDS.
- Xbox grouped/padded mip resources: base surface recovered without reading
  nonexistent separate mip payloads.
- Xbox 2048 x 2048 TerrainHeightfield: bounds, samples, and preview mesh parsed.
- PC little-endian and Xbox big-endian TerrainHeightfield headers.
- Binary DBX: typed immutable tree plus well-formed XML inspection view.
- WaterMesh: imported as native geometry.
- MedKit material: texture bindings and DXT5nm decode checked against compiled
  DX11 shaders.
- `MP_001`: direct and grouped placement graph, shared meshes, terrain, water,
  and shader database resolved from retail archives.
- `MP_001` map materials: pixel-shader `RDEF` names retained for each binding;
  representative background-tree, bark, foliage, and layered-prop
  permutations checked with FXC disassembly.
- Multi-subset map meshes: one position representation selected across related
  subsets, including half-float building blocks and the float-position bridge.
- Tested stride-48 static meshes: primary UV at byte 24 and secondary UV at
  byte 28, including GasSilo and IND_Silo_Medium.
- Layered silo materials: OcRGB uses UV1, repeating details use UV0, and the
  undecoded decal atlas remains classified separately instead of becoming a
  striped primary-color texture.
- Terrain alignment: forest origins ray-tested against imported heightfields;
  the source terrain entity translation is retained but not double-applied.
- `MP_001.Heightfield-000`: full 1024 x 1024 source mesh, plus targeted
  GasSilo, IND_Silo_Medium, and IND_SmallFactory contacts within one meter.
- `MP_001` TerrainMaterialMap: 4096 x 4096 indexed map, 128 tiles, seven named
  material classes, and file-backed L8 Blender sampling.
- Placement transforms: source column-major reconstruction, positive target
  determinants, and no former X/Z reflection.
- `IND_SmallFactory`: sparse metal override merged over the complete default
  shader set with no unresolved visible subsets.

The earlier individual-asset examples were reopened without extraction caches
in Blender 5.2.2 LTS, confirming that their required images were packed and
those scenes were self-contained. The complete-map workflow intentionally
keeps its much larger DDS set file-backed for performance.

## Automated regression coverage

Run:

```powershell
python -m compileall -q bc2_frostbite_importer
python -m unittest discover -s tests -v
```

Current tests cover:

- little- and big-endian MeshData, including mesh-wide half/float selection;
- PC L8-to-DDS conversion;
- generated L8 DDS output for decoded material-index images;
- Xbox tiled L8 conversion;
- NumPy and pure-Python untile parity for 1-, 4-, 8-, and 16-byte pitches;
- chunked NumPy untile and 16-bit block-swap parity;
- grouped Xbox mip allocation;
- PC/Xbox TerrainHeightfield header byte order, sample byte order, and
  downsampling;
- TerrainMaterialMap tile RLE, nibble order, names, and bounds validation;
- binary DBX typed-tree and XML parity;
- Frostbite placement translation and basis conversion;
- compact-group spawn/Havok enumeration validation;
- synthetic DX11 shader material, texture binding, and `RDEF` resource-name
  parsing;
- exact case-insensitive catalog lookup and path-based reads;
- one-time materialization and direct reuse of compressed FbRB payloads;
- synthetic XDVDFS -> FbRB -> MeshData end-to-end import.

See [`tests/test_core.py`](../tests/test_core.py).

Blender-facing validation runs separately:

```powershell
blender --background --factory-startup --python tests\blender_smoke.py
```

It checks bulk topology transfer, coordinate conversion, modern UV attribute
assignment, smooth flags, batched vertex weights, terrain creation, the
pure-Python mesh fallback, native file-backed DDS loading, combined subset
material indices and secondary UV layers (including Blender's degenerate-face
cleanup), linked map instances, placement matrices, map texture roles, layered
OcRGB/detail construction, and the verified DXT5nm node graph.

An optional real-media integration runner performs a complete import, saves the
blend, reopens it, and checks placement count, linked mesh count, exact child
collection counts, terrain contact and water bounds, plausible placement
extents, Base Color graph coverage, file-backed image availability, surface
match methods, shader-key aliases, and diagnostics persistence:

```powershell
blender --background --factory-startup `
  --python tests\blender_map_integration.py -- `
  --iso <PC_ISO> --cache <CACHE_DIRECTORY> `
  --output <OUTPUT_BLEND> --level MP_001
```

## Known heuristics

These behaviors are useful but need broader format research:

- MeshData payload-header search.
- Half-float versus float position scoring across all related subsets sharing
  a vertex stride.
- UV offset selection from stride and value ranges.
- Skinned vertex influence offsets.
- Mesh-set-to-MeshData resolution by path.
- Terrain sample-endian selection by smoothness.
- Small FbRB declared-payload discrepancy tolerance.
- Binary DBX 32-bit integer-versus-float rendering.
- Grouped Xbox mip handling currently recovers only the base surface.
- Non-MedKit DXT5nm decode and normal strength.
- Cutout-family classification and Blender dithered-alpha approximation.
- Index-based subset-to-surface fallback when normalized names do not match.
- Terrain detail-repeat scale and source-family selection.

Tests for a heuristic should include at least one sample that would make a
naive alternative choose incorrectly.

## Unsupported or incomplete areas

- Full vertex declarations, normals, tangents, colors, and arbitrary
  additional UV sets.
- Complete shader arithmetic, constants, swizzles, UV sources, and all texture
  semantics.
- Granny skeleton/model and animation conversion.
- Full DBX type coverage and lossless round trip.
- Nonstatic level systems such as effects, game logic, lights, sound,
  animation, destruction states, and collision.
- Exact terrain shader blending, decals, atlas masks, and lossless terrain
  writing.
- Exact WaterMesh shader reproduction.
- Audio, SWF, Bink, physics, and most other resource decoders.
- FbRB writing and repacking.
- Cabinet, MSI, ISO9660, XDVDFS, and XGD2 writing.

## How to record new findings

When adding knowledge:

1. State the platform and resource type.
2. Separate byte-level observation from interpretation.
3. Record offsets relative to a named structure.
4. State byte order per field.
5. Say how the claim was validated: parser invariant, synthetic fixture,
   cross-platform comparison, decoded output, or shader inspection.
6. Mark unresolved fields and preserve their bytes.
7. Update this document's coverage or unknowns list.

Do not include personal disc paths, copyrighted payloads, credentials, or
decompiled source. Small independently authored structural diagrams and numeric
test fixtures are preferred.
