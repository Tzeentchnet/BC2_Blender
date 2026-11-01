# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import time
import traceback
from pathlib import Path

import bpy
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bc2_frostbite_importer import blender_addon
from bc2_frostbite_importer.core.texture import (
    _untile_numpy,
    _untile_python,
    convert_texture,
)


def _elapsed(operation):
    started = time.perf_counter()
    result = operation()
    return time.perf_counter() - started, result


def _grid(size: int):
    positions = tuple(
        (float(column), float(row), float((row + column) % 11) / 10.0)
        for row in range(size)
        for column in range(size)
    )
    uvs = tuple(
        (column / (size - 1), row / (size - 1))
        for row in range(size)
        for column in range(size)
    )
    faces = []
    for row in range(size - 1):
        for column in range(size - 1):
            top_left = row * size + column
            top_right = top_left + 1
            bottom_left = top_left + size
            bottom_right = bottom_left + 1
            faces.append((top_left, bottom_left, top_right))
            faces.append((top_right, bottom_left, bottom_right))
    return positions, tuple(faces), uvs


def _build_from_pydata(positions, faces, uvs):
    mesh = bpy.data.meshes.new("BenchmarkFromPyData")
    mesh.from_pydata(positions, (), faces)
    mesh.update(calc_edges=True)
    mesh.validate(verbose=False, clean_customdata=False)
    layer = mesh.uv_layers.new(name="UVMap")
    for loop in mesh.loops:
        layer.data[loop.index].uv = uvs[loop.vertex_index]
    for polygon in mesh.polygons:
        polygon.use_smooth = True
    return mesh


def _build_accelerated(positions, faces, uvs):
    mesh = bpy.data.meshes.new("BenchmarkForeachSet")
    blender_addon._populate_triangle_mesh(
        mesh,
        positions,
        faces,
        uvs=uvs,
        smooth=True,
    )
    return mesh


def _benchmark_native_image():
    header = bytearray(92)
    struct.pack_into("<I", header, 8, 10)
    struct.pack_into("<II", header, 16, 1024, 1024)
    struct.pack_into("<I", header, 28, 1)
    struct.pack_into("<I", header, 32, 1024 * 1024)
    texture = convert_texture(
        bytes(header)
        + bytes(index & 0xFF for index in range(1024 * 1024)),
        "pc",
    )
    with tempfile.TemporaryDirectory(
        prefix="bc2-image-benchmark-"
    ) as temporary:
        path = Path(temporary) / "benchmark.dds"
        path.write_bytes(texture.dds)
        load_seconds, image = _elapsed(
            lambda: bpy.data.images.load(
                str(path), check_existing=False
            )
        )
        reuse_seconds, reused = _elapsed(
            lambda: bpy.data.images.load(
                str(path), check_existing=True
            )
        )
        if reused != image:
            raise RuntimeError(
                "Blender did not reuse the native DDS image"
            )
        pixels = np.empty(len(image.pixels), dtype=np.float32)
        decode_seconds, _ = _elapsed(
            lambda: image.pixels.foreach_get(pixels)
        )
        result = {
            "load_seconds": load_seconds,
            "reuse_seconds": reuse_seconds,
            "pixel_decode_seconds": decode_seconds,
            "source": image.source,
            "size": list(image.size),
            "depth": image.depth,
        }
        bpy.data.images.remove(image)
        return result


def main():
    positions, faces, uvs = _grid(256)
    baseline_seconds, baseline_mesh = _elapsed(
        lambda: _build_from_pydata(positions, faces, uvs)
    )
    accelerated_seconds, accelerated_mesh = _elapsed(
        lambda: _build_accelerated(positions, faces, uvs)
    )
    baseline_counts = (
        len(baseline_mesh.vertices),
        len(baseline_mesh.polygons),
        len(baseline_mesh.loops),
    )
    accelerated_counts = (
        len(accelerated_mesh.vertices),
        len(accelerated_mesh.polygons),
        len(accelerated_mesh.loops),
    )
    if baseline_counts != accelerated_counts:
        raise RuntimeError("Accelerated mesh topology differs")
    bpy.data.meshes.remove(baseline_mesh)
    bpy.data.meshes.remove(accelerated_mesh)

    width = height = 1024
    tiled = bytes(
        index & 0xFF for index in range(width * height)
    )
    python_seconds, python_result = _elapsed(
        lambda: _untile_python(tiled, width, height, 1)
    )
    numpy_seconds, numpy_result = _elapsed(
        lambda: _untile_numpy(tiled, width, height, 1)
    )
    if python_result != numpy_result:
        raise RuntimeError("Accelerated untile output differs")

    results = {
        "blender": bpy.app.version_string,
        "numpy": np.__version__,
        "mesh": {
            "vertices": len(positions),
            "triangles": len(faces),
            "baseline_seconds": baseline_seconds,
            "accelerated_seconds": accelerated_seconds,
            "speedup": baseline_seconds / accelerated_seconds,
        },
        "xbox_l8_untile": {
            "dimensions": [width, height],
            "python_seconds": python_seconds,
            "numpy_seconds": numpy_seconds,
            "speedup": python_seconds / numpy_seconds,
        },
        "native_dds_image": _benchmark_native_image(),
    }
    print(
        "BC2_IMPORT_BENCHMARK "
        + json.dumps(results, sort_keys=True),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
