# Project guidelines

## Documentation contract

Before completing any change, review its documentation impact.

- Update [`docs/`](docs/README.md) in the same change when parser behavior,
  offsets, byte order, format support, platform handling, shader/material
  interpretation, cache/catalog behavior, validation evidence, limitations, or
  export constraints change.
- Update the root [`README.md`](README.md) when user-facing installation,
  workflow, support, or limitations change.
- If no documentation edit is needed, still verify that the change introduces
  no new format fact, heuristic, behavior, or limitation.
- Keep [`docs/README.md`](docs/README.md) and all relative links current when
  adding, moving, or removing documentation.

## Documentation review

Review technical documentation against the implementation and tests, not
memory alone.

- Label claims as verified, implemented, heuristic, or unknown.
- Distinguish PC, Xbox 360, and cross-platform behavior.
- State structure-relative offsets, field widths, and byte order.
- Record evidence and known sample scope without publishing game payloads or
  personal source-media paths.
- Document lossy conversions and round-trip blockers prominently.
- Do not present preview-import heuristics as exporter-safe specifications.

## Architecture and implementation

Start with [`docs/README.md`](docs/README.md). Keep disc access, platform
resolution, FbRB parsing, resource decoding, and Blender conversion as separate
layers. Preserve unknown source data and fail explicitly for unsupported
formats.

Source media is read-only. Never add disc images, extracted assets, shader
bytecode, caches, or generated game data to the project.

## Validation

For parser or importer changes, run the smallest relevant tests and then the
core suite:

```powershell
python -m compileall -q bc2_frostbite_importer
python -m unittest discover -s tests -v
```

For Blender-facing mesh, material, image, or API changes, also run:

```powershell
blender --background --factory-startup --python tests\blender_smoke.py
```

Add synthetic fixtures for newly understood binary behavior. When behavior was
derived from real media, document the observation but keep proprietary inputs
outside the repository.
