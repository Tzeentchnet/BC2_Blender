# Import performance

## Design goal

Acceleration must preserve decoded output and must not make the dependency-free
core unusable outside Blender. NumPy is therefore optional:

- Blender 5.2's bundled NumPy enables accelerated paths automatically.
- Core modules continue to import and run when NumPy is absent.
- Pure-Python paths remain regression-tested against accelerated output.
- Unsupported or malformed data still raises the same explicit parser errors.

## Blender mesh construction

The original Blender adapter used `Mesh.from_pydata`, assigned UVs one loop at
a time, and enabled smooth shading one polygon at a time.

The accelerated path converts parsed tuples to contiguous NumPy arrays and
uses Blender RNA bulk access:

- `vertices.foreach_set("co", ...)`
- `loops.foreach_set("vertex_index", ...)`
- `polygons.foreach_set("loop_start", ...)`
- `polygons.foreach_set("loop_total", ...)`
- `MeshUVLoopLayer.uv.foreach_set("vector", ...)`
- `polygons.foreach_set("use_smooth", ...)`

Axis conversion and unit scaling are vectorized before coordinates are sent to
Blender. Terrain uses the same topology helper. Bone assignments are grouped by
bone and identical weight so `VertexGroup.add` can receive batches of vertex
indices.

Whole-map import combines MeshData subsets into one datablock per resolved
geometry/material variant. `MP_001` has 5,380 static visual placements but only
178 unique MeshData resources and 211 shader-set variants. Placement objects
link those shared datablocks instead of duplicating geometry per instance.

If NumPy is unavailable, the adapter retains `from_pydata`, per-loop UV
assignment, and per-polygon smoothing.

## Xbox texture conversion

Xbox untile address calculations are vectorized with NumPy for allocations of
at least 1024 blocks or pixels. Work is limited to chunks of 262,144 source
elements so large L8 textures do not require one set of full-surface coordinate
arrays.

The 16-bit byte swap used by compressed BC blocks is also vectorized for inputs
of at least 4096 bytes. Smaller inputs use the Python path to avoid array setup
overhead.

Both implementations produce bytes, and parity tests cover:

- pitches of 1, 4, 8, and 16 bytes;
- L8, uncompressed color, and BC block-sized addressing behavior;
- inputs large enough to cross vectorization chunk boundaries;
- accelerated and fallback 16-bit swaps.

## Native Blender image path

Converted resources are written atomically to source-specific DDS cache files
and loaded with:

```python
bpy.data.images.load(dds_path, check_existing=True)
```

This is the preferred Blender 5.2 path because it:

- keeps the image file-backed with `Image.source == "FILE"`;
- delegates DDS and BC-format handling to Blender's native image subsystem;
- avoids constructing four-channel Python or NumPy float pixel buffers;
- allows Blender to reuse an image already loaded from the same absolute path;
- preserves the cache as a disposable derivative of the source ISO.

The importer does not read `Image.pixels`, decode DDS into Python RGBA, or pack
images into the `.blend` by default. Packing is useful for standalone example
files but adds memory, save-time, and file-size costs to the normal browsing
workflow.

## Repository and map caches

An open `AssetRepository` caches parsed FbRB metadata by archive ID. More
importantly, whole-map preparation materializes gzip-compressed FbRB payloads
once under the source-fingerprint cache. Hundreds of DBX, mesh, shader, and
texture reads can then seek directly into the decompressed payload instead of
inflating from byte zero for every asset.

For the three `MP_001` archives, one measured run spent about 7.35 seconds
materializing approximately 730 MB of payloads. The complete level resolver
then fell from approximately 165 seconds with repeated gzip traversal to about
1.0 second with direct cached reads. These numbers are hardware- and
cache-dependent, but the asymptotic difference is structural.

Parsed metadata caches are cleared when a fresh disc scan resets the catalog.
Materialized payload files are fingerprint-scoped disposable derivatives and
are reused across Blender sessions while their size matches the archive
metadata.

Mesh resource bytes are parsed once per unique path during a map import.
Converted DDS images and Blender material datablocks are also reused by source
path and shader key respectively. Whole-map progress covers archive indexing,
direct-reference resolution, compact groups, mesh/material construction,
placement creation, terrain, and water.

## Measured Blender 5.2 results

The reproducible benchmark is
[`tests/benchmark_blender_import.py`](../tests/benchmark_blender_import.py).
Repeated Windows runs used Blender 5.2.2 LTS and bundled NumPy 2.3.4.

| Operation | Observed result |
|---|---:|
| 65,536 vertices, 130,050 triangles, UVs, smooth flags | 5.2x to 6.2x faster |
| 1024 x 1024 Xbox L8 untile | 41x to 69x faster |
| Initial native 1024 x 1024 L8 DDS load | 45 to 55 ms, file-backed |
| Reuse of the same native DDS image | 16 to 22 microseconds |

These are directional microbenchmarks, not performance guarantees. Hardware,
Blender builds, mesh layout, cache state, and asset size affect timings.

With the three map payloads and converted DDS files warm in the cache, the
real-media Blender integration runner assembled, saved, reopened, and validated
the complete `MP_001` foundation in about 5.5 seconds on the tested machine.
Cold cabinet extraction, payload materialization, and texture conversion add
source- and hardware-dependent time.

## Remaining candidates

Potential future work should be benchmarked before implementation:

- structured NumPy views for MeshData position and UV decoding;
- vectorized TerrainHeightfield sampling and face generation;
- lower overhead when repeatedly opening the same staged PC archive;
- background extraction that respects Blender's main-thread data API;
- explicit progress reporting for large conversions.

Blender datablock creation must remain on Blender's supported thread. Parallel
workers, if added, should stop at byte parsing or array preparation.
