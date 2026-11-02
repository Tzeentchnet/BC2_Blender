# Bad Company 2 Frostbite Importer for Blender 5.2

Bring assets from **Battlefield: Bad Company 2** into Blender directly from a
legally owned PC or Xbox 360 disc image. The extension browses the game
archives, imports supported assets as native Blender data, and never writes to
the source image.

No game files are included.

## Highlights

- Browse PC retail and Xbox 360 Frostbite archives without unpacking the whole
  disc first.
- Import meshes, textures, terrain, and water from both platforms.
- Import PC characters with armatures and selected animations.
- Assemble PC vehicles with named parts, authored transforms, and supported
  track components.
- Build PC map scenes with placed objects, terrain, water, and source-backed
  materials.
- Extract unsupported resources as raw files for inspection.

Support varies by asset and platform. See the
[detailed support table](docs/user-guide.md#native-blender-import-support)
before planning a large import.

## Requirements

- **Blender 5.2**
- A legally owned **PC retail ISO** or **Xbox 360 XGD2 ISO**
- Free disk space for the reusable asset cache

PC retail extraction currently requires Windows because it uses the disc's
Windows Installer database and `expand.exe`. Xbox 360 disc reading is
platform-independent.

> **PS3 status:** Several parsers recognize PS3-oriented resource variants and
> byte-order conventions, but PS3 disc images and end-to-end imports are
> unverified because the maintainer does not own the PS3 release. Treat this
> as experimental recognition, not supported media.

## Installation

1. Build or download the extension ZIP.
2. In Blender, open **Edit > Preferences > Get Extensions**.
3. Choose **Install from Disk** and select the ZIP.
4. Enable **Bad Company 2 Frostbite Importer** if needed.

Developers working from a source checkout can install the repository root as
an extension. Build and test commands are in
[Development and validation](docs/user-guide.md#development-and-validation).

## Quick start

1. Open **File > Import > Bad Company 2 / Frostbite 1**, or open the
   **Frostbite** tab in the 3D View sidebar.
2. Choose the PC or Xbox 360 ISO. Scanning runs in the background, so Blender
   remains usable.
3. For a PC map, select a discovered level and choose **Import Map**. To browse
   that map's individual objects first, choose **Index Map Objects**.
4. For other PC content, choose an archive and select
   **Index Selected Archive**. Xbox assets are indexed during the initial scan.
5. Filter or search the asset list, select an item, and choose
   **Import Selected** or **Extract Raw**.

The default PC scan is intentionally lightweight. Use **Index All (Slow)** only
when you need complete catalog coverage. Full workflow details, search
options, material modes, and character or vehicle assembly behavior are in the
[user guide](docs/user-guide.md#workflow).

## Good to know

- Source media is always read-only.
- The cache can become large, especially after **Index All (Slow)** or a map
  import. Its location is configurable, and generated data can be recreated
  from the ISO.
- Complete PC map import is supported, but Xbox whole-map assembly is not.
- Static map import does not recreate game logic, collision, destruction,
  audio, lights, weather, or every shader effect.
- Export and archive rebuilding are not implemented.

Read the [complete limitations](docs/user-guide.md#current-limitations) and
[cache behavior](docs/user-guide.md#cache-behavior) for details.

## Documentation

- [Detailed usage, settings, support, and validation](docs/user-guide.md)
- [Format and architecture documentation](docs/README.md)
- [PC platform notes](docs/platforms/pc.md)
- [Xbox 360 platform notes](docs/platforms/xbox-360.md)
- [Validation evidence and open questions](docs/validation-and-unknowns.md)

## Credits and legal

Format behavior was studied and validated with help from:

- [AnirohDev/FBOneTools](https://github.com/AnirohDev/FBOneTools)
- [Powback/Bad-Company-2-Map-Editor](https://github.com/Powback/Bad-Company-2-Map-Editor)
- Local Frostbite research tools supplied for this project

This project does not bundle code, executables, or copyrighted game assets
from those sources. Battlefield, Frostbite, Electronic Arts, and DICE are
trademarks of their respective owners. Use the importer only with media and
assets you are legally permitted to access.
