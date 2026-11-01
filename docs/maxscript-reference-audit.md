# External Frostbite MaxScript reference audit

## Scope

This audit compares the current Bad Company 2 importer with eight MaxScripts
from the supplied external `BFTools/Battlefield` reference set. The scripts
target Battlefield 3 or Battlefield 4 data and therefore later Frostbite
revisions. They are useful corroborating evidence, not a Frostbite 1
specification.

No script in this set reads a retail disc, MSI/CAB chain, XDVDFS filesystem,
FbRB archive, binary DBX document, terrain resource, shader database, or Granny
animation. The mesh importers expect already extracted `.mesh` and `.chunk`
files. The bone tools expect a pre-generated text dump.

Findings are classified as:

- **Corroborating**: a format principle also supported by BC2 retail evidence.
- **Heuristic only**: useful for directing further BC2 research, but not safe
  to implement without a retail invariant.
- **Later-engine-specific**: offsets, identifiers, or DCC choices that must not
  be copied into the Frostbite 1 parser.
- **No evidence**: the scripts do not address the import path.

## Audited sources

The files are external research inputs and are not distributed with this
repository.

| Script | Lines | SHA-256 | Purpose observed in the source |
|---|---:|---|---|
| `BF3 Mesh Importer v4.1.ms` | 489 | `3C843E5DF54E5F9D5A63B1E6D5E2144A6A23050DE0F9A2441E06B061DB456301` | BF3 mesh/chunk geometry, skinning, simple materials, and type-2 part separation |
| `bf3_mesh importer(V_3.0).ms` | 439 | `CB6F1AB19509295EA246A6A2C38F398C198B9437ADB31C66AE0598ED896AD4A5` | Earlier revision of the BF3 mesh importer |
| `BF4 Mesh Importer.ms` | 583 | `A54E7888F7CFF96FAD051286C30B7480BC97EF912C07968F89769DF073FDCBC6` | BF4 variant with different header/submesh offsets and an explicit DCC rotation |
| `BF3 Vehicle SkeletonReader.ms` | 84 | `649C5627CC50F4814E449080D8DB329DC35EA1F1F2B7F919C081F2B235F6F249` | Creates unparented helper bones from text-dumped linear transforms |
| `FB3_Vehicle_NodeReader.ms` | 84 | `649C5627CC50F4814E449080D8DB329DC35EA1F1F2B7F919C081F2B235F6F249` | Byte-identical copy of the vehicle skeleton reader |
| `BF4 Bone Maker v1.3.ms` | 121 | `FE7D1E11AA64A124BE008CE9D8D1A9DA88111A538667DF865E70C7A52A71E5D8` | Character bones from hierarchy, names, and model-pose text arrays |
| `Bone_Maker(1.3).ms` | 112 | `6F9C67B3B33F578E1D6E9ADFCA9D8911CE99AC80C384CA6A485372FCD6F670AB` | Earlier character bone-maker variant |
| `Bone_Maker(For BF3_100% WORKING-V2).ms` | 102 | `56E8BDDF40906B12DF46F3D2245C09F70DD914158D2F7A312A805F54D5E2784C` | BF3-specific character bone-maker variant |

The two BF3 mesh revisions share the same geometry and vertex-declaration
logic. Version 4.1 changes automatic companion-file selection, limits skinning
to object type 1, and adds optional type-2 part detachment. The BF4 importer
retains the same named vertex semantics but changes numerous structure-relative
offsets and submesh record sizes. That difference is direct evidence that
later-engine numeric layouts are version-specific.

## Mesh declaration findings

### Declaration-driven attributes

All three mesh scripts read a fixed-size declaration area and locate an
attribute by finding its semantic identifier, then using the following
declaration word as the byte offset into each vertex. They do not infer UV,
tangent, or skin fields from vertex stride alone.

The scripts assign these names to their later-engine identifiers:

| Identifier | Script name | Script interpretation |
|---:|---|---|
| `769` | `Vert_Positions_F` | three `float32` position components |
| `1793` | `Vert_Positions_HF` | three half-float position components |
| `1289` | `Vert_Unknown_Flag1` | unknown |
| `2054` | `Vert_BiNormals` | three half-float basis components |
| `2055` | `Vert_Tangents` | three half-float basis components |
| `1569` | `Vert_Normals_UV` | half-float coordinate pair |
| `1570` | `Vert_Diffuse_UV` | half-float coordinate pair |
| `3074` | `Vert_Bone_Indices` | four unsigned byte indices |
| `3332` | `Vert_Blend_Weights` | four unsigned byte weights |
| `3358` | `Vert_Unknown_Flag2` | marked as vehicle-related |
| `2056` | `Vert_Unknown_Flag3` | unknown |
| `1571` | `Vert_Unknown_Flag4` | unknown |

The numeric identifiers and declaration offsets are **later-engine-specific**.
The transferable principle is **corroborating**: Frostbite vertex fields
should be decoded from an identified declaration whenever that declaration is
available. BC2's verified static and character offsets still come from retail
resources and DX11 input declarations, not from these identifiers.

### Positions, indices, and winding

The first declared position semantic selects full-float or half-float reads.
Positions are multiplied by the script's global `39` DCC scale. Triangle-list
indices are read as unsigned 16-bit values and used in stored order.

The position formats and 16-bit triangle-list indices are **corroborating**
with tested BC2 MeshData. The multiplier is **later-engine-specific DCC
policy**, not an engine unit specification. The scripts do not validate
triangle winding against a handedness-changing conversion, so they are not
sufficient evidence for an exporter.

### UVs and tangent-frame data

The scripts treat the diffuse and normal coordinates as distinct half-float
pairs. Both channels receive an explicit V flip in 3ds Max. Tangent and
binormal semantics are read independently as half-float vectors.

This is **corroborating** evidence for:

- preserving multiple coordinate channels independently;
- applying the DirectX-to-DCC V convention exactly once;
- not interpreting tangent-frame bytes as UVs;
- eventually replacing stride/value UV guesses with declaration-driven
  decoding.

It does not identify BC2's byte-16 character field, prove that every declared
coordinate is a conventional 2D UV, or establish BC2 tangent handedness.

### Skin palettes and weights

For object type 1, the BF3 v4.1 and BF4 scripts:

1. read a per-submesh array of global skeleton indices;
2. add bones to the skin modifier in that palette order;
3. read four local unsigned byte indices per vertex;
4. read four unsigned byte weights and divide by `255`;
5. apply the local index/weight tuples to the vertex.

This is strong **corroborating** evidence for the current BC2 type-1 model:
vertex indices address a subset-local palette whose entries identify global
skeleton bones. It also supports the current four-byte normalization path.
The BC2 offsets remain independently verified at bytes 8 and 12 for the tested
stride-64 character declaration.

BF3 v3.0 attempted skinning whenever a bone array existed. BF3 v4.1 corrected
that behavior by requiring object type 1. This version delta supports the
current importer's explicit `mesh_type == 1` gate and its refusal to treat
every palette-bearing mesh as skinned.

### Composite and vehicle parts

BF3 v4.1 adds a separate object-type-2 path. It groups vertices by their
four-byte bone-index tuple and optionally detaches groups into separate 3ds Max
objects. The script calls this vehicle part handling.

This is a **heuristic-only** lead for BC2 `mesh_type == 2` composite data:
palette-like fields may encode rigid part membership rather than linear skin
weights. It is not sufficient to implement BC2 part splitting because:

- the script targets a later mesh/chunk layout;
- it determines eligibility through a weak same-index check;
- it does not reconstruct part names, parent transforms, or pivots;
- tested BC2 type-2 strides do not share the validated type-1 byte layout.

The current conservative BC2 fallback therefore remains unchanged. Future work
must correlate candidate per-vertex part identifiers with a BC2 DBX hierarchy
and retail transforms before exposing separate parts.

## Skeleton and transform findings

The three character bone-maker scripts read text sections named
`Hierarchy::array`, `BoneNames::array`, and `ModelPose::array`. Each model-pose
member contains named `right`, `up`, `forward`, and `trans` vectors. Parent
indices are converted from zero-based values to 3ds Max array positions;
`-1` becomes the root sentinel. The scripts assume parents occur before their
children.

This is **corroborating at the data-model level**:

- Frostbite model poses have named bones and indexed parent relationships;
- linear transforms expose three basis vectors and a translation;
- source order and root sentinels must be preserved.

The bone-maker implementation is not a reliable transform specification:

- bone counts are hard-coded to 211 or 213;
- every component is multiplied by an empirical scale of `39`;
- the complete matrix assignment is commented out;
- bones are aimed along normalized `right` with normalized `forward` used as
  the up vector;
- no inverse bind matrices or scale/shear values are preserved;
- the BF4 variant additionally rotates a scene object named `Reference` by
  90 degrees around X.

The two byte-identical vehicle readers scan every text
`Transform::LinearTransform` and create an unnamed, unparented helper bone at
its translation. They do not read part names, hierarchy, pivots, or animation.
They therefore provide no implementation-ready vehicle skeleton path.

The current PC Granny importer remains authoritative for BC2 character
skeletons because local transforms, parent indices, and transposed inverse-world
matrices satisfy an independent identity invariant across all 147 tested
bones. None of the MaxScripts decodes Granny sections, relocations, curve
codecs, key times, root motion, or animated scale/shear.

## Material findings

The BF3 mesh importers construct a 3ds Max standard material and look for
lower-cased PNG files named from the submesh material plus `_d`, `_s`, and
`_n`. The BF4 importer searches DDS files in the mesh directory or a
`textures` child directory and performs similar suffix matching from the
object name.

This is only a **filename heuristic**. The scripts do not decode:

- a Frostbite shader database or `MeshShaderSet`;
- DBX texture references;
- DXBC reflection or shader constants;
- sRGB/non-color state;
- channel swizzles or packed normal formats;
- camouflage, dirt, occlusion, detail repeats, cutout thresholds, or layered
  material arithmetic.

The BC2 importer's DBX, shader-database, and compiled-shader evidence is
strictly stronger. No material behavior changed as a result of this audit.

## Axis and unit findings

The BF3 mesh scripts leave imported mesh orientation unchanged. The BF4 mesh
script rotates every mesh 90 degrees around X, and the BF4 bone-maker rotates
the scene's `Reference` object by the same amount. All mesh and bone scripts
use the empirical scale `39`.

These are **conflicting, version-specific DCC choices**, not evidence that BC2
uses a scale of 39 or requires an additional 90-degree rotation. The current
orthogonal BC2 Y-up to Blender Z-up basis change and configurable unit scale
remain backed by retail placement, terrain, skeleton, and animation
invariants.

## Current import-path comparison

| Current path | MaxScript evidence | Classification and decision |
|---|---|---|
| PC ISO9660, MSI, CAB | None | **No evidence**; retain retail-media implementation |
| Xbox XGD2/XDVDFS | None | **No evidence**; scripts are not console readers |
| FbRB archive and catalog | None | **No evidence** |
| Static/rigid MeshData | Declaration-driven attributes, half/full positions, 16-bit triangle lists, two UV semantics | **Corroborating principles**; do not copy later offsets or IDs |
| Type-1 skinned MeshData | Local palette indices and four byte weights | **Corroborating**; current retail-verified implementation retained |
| Type-2 composite MeshData | Optional vertex-group detachment as vehicle parts | **Heuristic only**; no code change |
| Type-4 tree MeshData | None | **No evidence** |
| Mesh normals/tangents | Separate declared half-float basis fields | **Research lead**; BC2 semantics and handedness still need retail validation |
| WaterMesh | None | **No evidence** |
| ITexture/XenonTexture conversion | PNG/DDS filename lookup only | **No storage evidence**; no information about headers, mips, tiling, or block byte order |
| Materials and shaders | `_d`/`_s`/`_n` standard-material heuristic | **Superseded** by BC2 DBX, DX11 database, and shader disassembly |
| Granny skeleton | General parent/name/model-pose concepts only | **Corroborating model**, but not binary-layout evidence |
| Granny animation | None | **No evidence** |
| TerrainHeightfield and TerrainMaterialMap | None | **No evidence** |
| Binary DBX and complete maps | Text-dump field names only; no binary parser or placement graph | **No implementation evidence** |
| Instancing and compact groups | None | **No evidence** |
| Axis and scale | BF3/BF4 disagree; hard-coded scale 39 | **Non-transferable** |
| Performance | Repeated per-vertex seeks and per-element Max API calls | **Not a performance model**; retain bounded bulk/NumPy paths |
| Export | Implies declarations and local palettes must be preserved | **Design guidance only**; no writer or round-trip proof |

## Implementation decision

This audit caused no runtime parser or Blender behavior change:

- every directly transferable finding already agrees with a BC2 retail
  invariant or current type-1 gate;
- the newly identified type-2 part path lacks enough BC2 evidence;
- later-engine offsets, semantic identifiers, hard-coded counts, scale, and
  rotations are unsafe to import.

Addon version `0.5.1` therefore remains unchanged. The audit updates
documentation and research priorities only.

## Follow-up research

1. Locate or reconstruct BC2 vertex declarations from retail metadata and
   shader input signatures, then replace remaining stride/value guesses.
2. Decode and validate BC2 normal, tangent, and handedness fields against
   rendered shader behavior.
3. Correlate type-2 per-vertex data, palettes, DBX part names, parent
   transforms, and pivots before implementing composite separation.
4. Preserve full declarations, unknown attributes, and local palettes in any
   future mesh export sidecar.
5. Treat vehicle hierarchy and animation as separate research tasks; the two
   vehicle readers do not solve either one.
