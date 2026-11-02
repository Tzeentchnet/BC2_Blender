# Granny characters and animation

## Scope and confidence

The native implementation is intentionally limited to the **PC retail**
little-endian Granny resources validated here:

- the shared `Characters/Skeletons/ske01` human skeleton;
- the `Characters/US/US_Models/US_01_Body` skinned body;
- the linked `US_01_Head` and `US_01_Headgear` LOD0 modules;
- the rifle third-person `StandIdle` and `RunForward` clips;
- `US_01_SkeletonPose` as a rest-pose cross-check;
- the 17-bone Raven skeleton, stride-48 skinned mesh, and six clips in
  `Animations/Animals/Raven`;
- the Beach US Scout and Japanese infantry pose assets, their explicit facial
  and kit-joint animation trees, four shared facial delta clips, and eight
  rifle locomotion clips selected through their explicit shared tree.

The Frostbite wrappers and Granny v7 structures below are **implemented** with
bounded reads and synthetic fixtures. Field meanings are **verified** where
the skeleton inverse-bind matrices, pose clip, or Blender output provide an
independent invariant. Xbox 360 and PS3 Granny variants are not advertised as
native support.

`Objects/Soldiers/US/Assault.dbx` links these three mesh modules through
`Meshes3p`. Modular import verifies that every component references `ske01`,
creates the armature once, and binds all 23 visible subsets to it. Component
collections retain role and `RandomGroup` metadata so optional equipment can
be mixed without duplicating the rig.

## Frostbite wrapper

Both resources start with a Frostbite string table followed by padding and a
Granny file.

### PC `GrannyModel`

```text
uint32_le wrapper_version       // 10 in the tested skeleton
uint32_le string_count
repeat string_count:
    uint16_le byte_length
    byte[byte_length] latin-1 string
padding
Granny v7 file
```

### PC `GrannyAnimation`

```text
uint32_le wrapper_version       // 10
uint32_le additive_bone_count   // 0 for absolute clips
if additive_bone_count > 0:
    uint32_le[additive_bone_count] skeleton_bone_indices
    uint32_le reserved           // 0 in verified clips
uint32_le string_count
repeat string_count:
    uint16_le byte_length
    byte[byte_length] latin-1 string
padding
Granny v7 file
```

These offsets are relative to the start of the Frostbite wrapper. When
`additive_bone_count` is zero, `string_count` is at byte 8. Otherwise the
index array starts at byte 8, the reserved field follows the array, and
`string_count` follows the reserved field. All fields are little-endian.

The second field was previously treated as an unsupported flag. **Verified**
PC retail facial clips use it as a sparse `ske01` bone-index table:
`DeltaBlink` has 4 entries, `DeltaEyeIdle` has 6, and the two dead-pose deltas
have 21 and 14. Each table maps to exactly the clip's transform-track name set.
Their curves are identity/zero at time zero and encode pose-basis deltas. The
parser bounds the count and indices, rejects duplicates/nonzero reserved
markers, and retains the indices on the parsed animation. The index table does
not identify the complete pose used at runtime; applying these sparse
transforms directly over `ske01` rest was visually disproved by Japanese
infantry eye and face deformation.

The PC Granny file starts with this 32-byte magic:

```text
29 de 6c c0 ba a4 53 2b 25 f5 b7 a5 f6 66 e2 ee
c8 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

The importer searches only the first 64 bytes after the string table. It does
not accept an arbitrary same-looking signature elsewhere in a resource.

## Granny v7 container

Offsets in this section are relative to the start of the 32-byte magic.

The 72-byte little-endian file header begins at offset 32. Verified fields are:

| Offset | Type | Meaning |
|---:|---|---|
| 32 | `uint32` | Granny version, `7` |
| 36 | `uint32` | complete Granny-file size |
| 40 | `uint32` | CRC/check value; retained but not interpreted |
| 44 | `uint32` | section-table offset relative to the end of the magic; `72` in tested files |
| 48 | `uint32` | section count; `8` in tested files |

The section table therefore starts at `32 + section_table_offset`. Each
descriptor is 44 bytes / 11 little-endian `uint32` values:

```text
compression
data_offset
compressed_size
uncompressed_size
alignment
first_16bit_offset
first_8bit_offset
relocation_offset
relocation_count
mixed_marshalling_offset
mixed_marshalling_count
```

All validated PC sections use compression mode zero and have equal compressed
and uncompressed sizes. A relocation record is:

```text
uint32_le source_offset
uint32_le target_section
uint32_le target_offset
```

Source offsets are relative to the section that owns the relocation. Target
offsets are relative to the target section. Section 0 contains serialized
runtime objects and curve data. Section 6 contains 32-byte Granny type/member
records used to identify curve codecs through the wrapper string table.

Mixed-marshalling tables exist in the animation files, but the tested PC data
is already directly readable at the declared little-endian field locations.
The importer bounds those tables through the file descriptor but does not
interpret them.

## PC skeleton

In the validated PC Granny model, section 0 contains:

| Section-0 offset | Type | Meaning |
|---:|---|---|
| 172 | `uint32` | bone count |
| 176 | relocated pointer | 152-byte bone array |

Each verified bone record is:

| Record offset | Type | Meaning |
|---:|---|---|
| 0 | `uint32` | wrapper-string index for the bone name |
| 4 | `int32` | parent index, `-1` for a root |
| 8 | `uint32` | source flags |
| 12 | `float32[3]` | local translation |
| 24 | `float32[4]` | local quaternion in `x, y, z, w` order |
| 40 | `float32[9]` | local scale/shear matrix |
| 76 | `float32[16]` | inverse-world transform |
| 140 | `byte[12]` | not interpreted by the importer |

The inverse-world array is stored in the source's row-vector matrix layout.
Transposing it produces the conventional column-vector inverse used for
validation. Recursively composing local `translation * rotation *
scale/shear` matrices for all 147 `ske01` bones and multiplying by those
transposed inverses produced a maximum identity residual below `7.5e-7`.

The importer retains all 147 bones, including 11 names beginning with
`physJnt_`. It marks those bones with `bc2_physics_joint` metadata rather than
deleting or renumbering them. Source flags are retained as
`bc2_source_flags`.

## Transform animation object graph

The PC animation file-info object in section 0 contains an animation count at
offset 76 and a relocated animation-array pointer at offset 80. The validated
resources contain exactly one animation.

A relocated animation object has:

| Object offset | Type | Meaning |
|---:|---|---|
| 0 | `uint32` | name/string field |
| 4 | `float32` | duration in seconds |
| 8 | `float32` | time step in seconds |
| 12 | `float32` | oversampling |
| 16 | `uint32` | track-group count |
| 20 | relocated pointer | track-group pointer array |

A tested transform track group has its transform-track count at offset 12 and
its relocated track-array pointer at offset 16. A nonzero vector-track count
at offset 4 is rejected because vector-track semantics are not implemented.

Each transform-track record is 32 bytes:

| Record offset | Type | Meaning |
|---:|---|---|
| 0 | `uint32` | wrapper-string index for the bone name |
| 4 | `uint32` | track flags |
| 8, 12 | relocated type, data pointers | rotation curve |
| 16, 20 | relocated type, data pointers | translation curve |
| 24, 28 | relocated type, data pointers | scale/shear curve |

The type pointer targets section 6. A `CurveDataHeader_*` wrapper string names
the codec. The data pointer targets its codec-specific header or constant.

## Implemented curve codecs

The selected clips require:

### Rotation

- `D4nK16uC15u`
- `D4nK8uC7u`
- `D4Constant32f`
- `DaK32fC32f`
- `DaIdentity`

The `D4n` headers contain format and degree bytes, packed quaternion
scale/offset-table selectors, a reciprocal knot scale, a value count, and a
relocated packed-value array. Each key stores one knot and three quaternion
components; flag bits identify the omitted component and its sign. The fourth
component is reconstructed from unit length.

`D4Constant32f` stores four controls beginning four bytes into the curve data.
`DaK32fC32f` stores a knot count/pointer at offsets 4/8 and a scalar control
count/pointer at offsets 12/16.

### Translation

- `D3Constant32f`
- `D3I1K32fC32f`
- `D3I1K8uC8u`
- `D3I1K16uC16u`
- `D3K8uC8u`
- `D3K16uC16u`
- `DaIdentity`

`D3Constant32f` stores three controls beginning at offset 4. The packed `D3`
headers contain three control scales at offset 4, three offsets at 16, a value
count at 28, and a relocated value pointer at 32. The high 16 bits of the
stored truncated reciprocal reconstruct the float knot scale.

`D3I1` stores one quantized scalar per control and applies the three
scale/offset pairs to that scalar. `D3K` stores three quantized control
components per knot.

`D3I1K32fC32f` uses the same scalar-control reconstruction without
quantization. Relative to its curve-data object, bytes 0/1 are the `uint8`
format/degree, byte 2 is a `uint16` padding field, bytes 4/16 are three
little-endian `float32` control scales/offsets, byte 28 is the `uint32` scalar
count, and byte 32 is the relocated array pointer. The array stores `n`
`float32` knots followed by `n` `float32` scalar controls.

### Scale/shear

- `DaIdentity`
- degree-0 `DaConstant32f`

`DaConstant32f` stores a `uint32` control count at object-relative byte 4 and
a relocated pointer at byte 8. The validated scale/shear form has exactly nine
little-endian `float32` controls in row-major 3 x 3 order. Raven's
`CorpseEating` and `Idle04` use a verified uniform 0.8 matrix on
`exportAnimation`; the other Raven tracks use identity.

The parser evaluates degrees 0, 1, 2, and 3. A degree-3 interval uses the first
knot strictly greater than the sample time as index `i`, controls `i-3..i`,
and knots `i-3..i+2`, with endpoint indices clamped. The four weights are the
bounded cubic de Boor recurrence. This convention is **implemented** with a
synthetic `D3I1K32fC32f` fixture and **verified** by finite source-step samples
from all six Raven clips. Quaternion results are normalized. Unsupported
codecs and degrees fail explicitly instead of producing identity motion.

Time-varying scale/shear codecs remain unsupported. Blender Actions key the
decomposed scale for representable constant matrices and reject transforms
with a reconstruction residual of `1e-5` or greater rather than silently
discarding shear.

## Blender conversion

The source uses Y-up coordinates. With axis conversion enabled, points map as:

```text
(x, y, z) -> (x, -z, y)
```

The same orthogonal basis change is applied to skeleton rest matrices and
animation local transforms. Unit scale affects translations but not
orientation.

Armature creation:

1. Builds source local matrices from translation, quaternion, and scale/shear.
2. Recursively composes source world matrices.
3. Applies the Frostbite-to-Blender basis change.
4. Creates every source bone without filtering or renumbering.
5. Chooses a display length without changing the source rest matrix.
6. Names mesh vertex groups from the global palette indices.
7. Adds linear-blend Armature modifiers to every visible subset.

Character material resolution is separate from Granny decoding. The matching
`Render.SkinnedMeshAsset` DBX supplies the default surface set, while the
retail DX11 database supplies texture bindings and shader evidence. The tested
stride-64 mesh preserves four shader-coordinate channels and applies a
nondegenerate UV routing correction where the byte-16 field collapses. See
[Meshes](meshes.md) and
[Shaders and materials](../shaders-and-materials.md).

Animation import samples from time zero through the inclusive duration at the
source time step. Rest-local versus animated-local transforms are converted to
Blender pose-bone deltas. The Blender 5.2 Action uses a slot, a keyframe layer,
and a channel bag. Translation, quaternion, and scale F-Curves use linear
interpolation between native samples, and quaternion signs are made continuous.
Root motion is retained.

For an animation with a nonempty wrapper bone-index table, the importer first
requires the indexed skeleton-bone names to equal the transform-track names.
The verified Scout and Japanese facial trees each place their character-local,
static `FacePose` in the misspelled `ReplacmentAnimations` array. The importer
preserves that reference separately and creates a standalone Action using:

```text
absolute_source_local = FacePose_source_local @ delta_source_local
blender_basis = rest_blender_local^-1 @ convert(absolute_source_local)
```

Base tracks without a sparse delta are also keyed, so each resulting facial
Action has 46 baked tracks even though the source delta has only 4, 6, 21, or
14. `bc2_track_count` and `bc2_additive_bone_count` retain the source counts;
`bc2_baked_track_count`, `bc2_additive_baked`, and
`bc2_baked_base_asset_path` identify the standalone bake. `bc2_additive` and
`bc2_source_additive` describe the source wrapper, not an Action intended for
additive NLA blending. Direct import fails explicitly when a sparse additive
clip has no validated authored base pose.

Rigged PC mesh import can also create related Actions by default. Discovery is
an **implemented bounded policy**, not a general animation-set specification:

1. derive the mesh's exact referenced skeleton;
2. search only indexed `GrannyAnimation` resources in the mesh's and
   skeleton's exact parent folders;
3. for a direct PC soldier mesh, inspect at most 256 `Objects/Soldiers` DBX
   descriptors in the preferred archives and require an explicit matching
   `Meshes3p` reference;
4. follow the matched third-person `StaticPose`, `FacialAnimations`,
   `KitJointsAnimTree`, and `ReviveAnimTree` links;
5. read only explicit `AnimationList`, `AimAnimationList`, and replacement
   arrays from those character-specific trees;
6. when core gameplay import is enabled, follow `Shared` to its explicit
   `BaseSetAnimTrees`, select only `SoldierAnimTree3pRif`, and retain the exact
   `StandIdle`, `StandRelaxedIdle`, `CrouchIdle`, `WalkForward`,
   `WalkBackward`, `RunForward`, `RunBackward`, and `SneakForward` entries;
7. require every tree, base pose, and candidate clip to reference the exact
   skeleton;
8. deduplicate resource paths using archive preference ordering and stop when
   the combined folder/tree result exceeds 64 unique paths.

The complete verified `DefaultAISoldierAnimTree3p`/`SquadAISoldierAnimTree3p`
graph expands through 11 trees to about 644 unique clips. The rifle tree alone
lists 202. Importing that graph wholesale is deliberately unsupported as an
automatic policy because it would be slow and would overwhelm Blender's Action
list. The eight-clip subset is an **implemented preview heuristic**, not a
claim that these clips form the complete runtime state machine.

The add-on warns when any candidate fails or when archives remain unindexed,
because a lightweight catalog cannot prove that no additional related clips
exist. Related animation import and the core-gameplay subset have separate
default-on controls in Add-on Preferences and the session's Import Settings.
For direct soldier reverse lookup, a compressed preferred archive with
multiple candidate descriptors is materialized once in the fingerprint-scoped
disposable cache rather than reinflated for every DBX.

Searching the mesh folder remains required for characters such as
`US_Scout_Mesh`: its verified `FacePose` and `Us_Scout_SkeletonPose` clips sit
beside the mesh, while its shared `ske01` skeleton sits under
`Characters/Skeletons`. Its soldier DBX explicitly links
`USScoutFacialAnimTree`; Japanese infantry similarly links
`JapInfantryFacialAnimTree` plus its kit-joint tree. Each imports 14 Actions:
the two mesh-specific poses, four FacePose-baked facial Actions, and eight
core rifle locomotion Actions. The Japanese `combat_pose` is **verified** as a
static 46-track pose referenced by level and cutscene trees, not by the regular
soldier's bounded graph, so it is not automatically included.

## MaxScript skeleton cross-check

The three audited character bone-maker scripts consume pre-generated text
arrays named `Hierarchy`, `BoneNames`, and `ModelPose`. They corroborate the
general model of indexed parents, named bones, a root sentinel, and linear
transforms containing right/up/forward basis vectors plus translation.

They are not Granny decoders. Bone counts are hard-coded, full matrices and
scale/shear are not assigned, inverse binds are ignored, and an empirical
scale of 39 is used. The BF4 variant also rotates a scene `Reference` object
90 degrees around X. The two vehicle readers are byte-identical and create
unnamed, unparented helpers from linear transforms. None reads animation
sections, relocations, curve codecs, key times, or root motion.

The retail inverse-bind and pose-matrix invariants remain the authoritative BC2
validation. See the [external MaxScript audit](../maxscript-reference-audit.md).

## Validation sample

| Measurement | Result |
|---|---:|
| `ske01` source bones | 147 |
| physics-joint names retained | 11 |
| `US_01_Body` visible subsets | 13 |
| complete Assault visible subsets | 23 (13 body, 3 head, 7 headgear) |
| vertices / triangles | 10,513 / 9,883 |
| source bones used by the mesh | 61 |
| exact visible material keys | 13 / 13 |
| source-backed body images | 34 |
| source-backed complete-assembly images | 56 |
| retained character UV layers | 4 |
| `StandIdle` duration / inclusive frames | about 3.0 s / 91 at 30 Hz |
| `RunForward` duration / inclusive frames | about 0.6 s / 19 at 30 Hz |
| transform tracks in each clip | 39 |
| saved-scene bind deformation error | below `2.5e-7` Blender units |
| maximum sampled pose-matrix error | below `3.8e-6` |
| Raven skeleton / tracks per clip | 17 / 17 |
| unique Raven clips / indexed catalog rows | 6 / 10 |
| Raven duration range / sample rate | 4.7-6.67 s / 60 Hz |
| Raven related Actions after save/reopen | 6 |
| Scout skeleton / mesh subsets | 147 bones / 11 |
| Scout bounded related Actions | 14: 46/147-track local poses, four 46-track FacePose-baked facial Actions, and eight 39-track rifle locomotion Actions |
| Japanese infantry skeleton / mesh subsets | 147 bones / 8 |
| Japanese infantry bounded related Actions | 14; `dead_pose2_Delta` maximum/p99/mean FacePose-relative vertex displacement about 0.00905/0.00344/0.000160 |

`US_01_SkeletonPose` has 64 transform tracks. Most evaluated controls agree
with the skeleton local transforms to floating-point or quantization
precision, providing an independent check of quaternion order, constant-curve
layout, and local-transform semantics.

## Export and remaining unknowns

The native importer is not an exporter specification. A safe animation writer
would still need to:

- preserve wrapper strings and unknown fields;
- reproduce section layout, relocations, mixed marshalling, and CRC behavior;
- preserve or deliberately regenerate each original curve codec;
- establish quaternion quantization and degree-2/degree-3 fitting error bounds;
- retain unsupported vector and scale/shear tracks;
- verify game playback and byte-stable no-edit round trips.

Xbox 360 and PS3 byte order, outer wrappers, skeleton object layouts, and
compression/marshalling variants need separate validation.

See [`granny.py`](../../bc2_frostbite_importer/core/granny.py),
[`mesh.py`](../../bc2_frostbite_importer/core/mesh.py), and
[`blender_addon.py`](../../bc2_frostbite_importer/blender_addon.py).
