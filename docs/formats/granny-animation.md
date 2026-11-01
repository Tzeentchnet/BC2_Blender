# Granny characters and animation

## Scope and confidence

The native implementation is intentionally limited to the **PC retail**
little-endian Granny resources validated here:

- the shared `Characters/Skeletons/ske01` human skeleton;
- the `Characters/US/US_Models/US_01_Body` skinned body;
- the rifle third-person `StandIdle` and `RunForward` clips;
- `US_01_SkeletonPose` as a rest-pose cross-check.

The Frostbite wrappers and Granny v7 structures below are **implemented** with
bounded reads and synthetic fixtures. Field meanings are **verified** where
the skeleton inverse-bind matrices, pose clip, or Blender output provide an
independent invariant. Xbox 360 and PS3 Granny variants are not advertised as
native support.

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
uint32_le wrapper_flags         // 0 in tested clips
uint32_le string_count
repeat string_count:
    uint16_le byte_length
    byte[byte_length] latin-1 string
padding
Granny v7 file
```

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

The parser evaluates degree 0, 1, and 2 curves. Quaternion results are
normalized. Unsupported codecs and degrees fail explicitly instead of
producing identity motion.

All selected clips use `DaIdentity` for scale/shear. Any nonidentity animated
scale/shear codec is currently rejected.

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
and a channel bag. Translation and quaternion F-Curves use linear interpolation
between native samples, and quaternion signs are made continuous. Root motion
is retained.

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
| vertices / triangles | 10,513 / 9,883 |
| source bones used by the mesh | 61 |
| exact visible material keys | 13 / 13 |
| source-backed character images | 34 |
| retained character UV layers | 4 |
| `StandIdle` duration / inclusive frames | about 3.0 s / 91 at 30 Hz |
| `RunForward` duration / inclusive frames | about 0.6 s / 19 at 30 Hz |
| transform tracks in each clip | 39 |
| saved-scene bind deformation error | below `2.5e-7` Blender units |
| maximum sampled pose-matrix error | below `3.8e-6` |

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
- establish quaternion quantization and degree-2 fitting error bounds;
- retain unsupported vector and scale/shear tracks;
- verify game playback and byte-stable no-edit round trips.

Xbox 360 and PS3 byte order, outer wrappers, skeleton object layouts, and
compression/marshalling variants need separate validation.

See [`granny.py`](../../bc2_frostbite_importer/core/granny.py),
[`mesh.py`](../../bc2_frostbite_importer/core/mesh.py), and
[`blender_addon.py`](../../bc2_frostbite_importer/blender_addon.py).
