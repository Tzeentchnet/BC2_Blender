# Bad Company 2 Frostbite Importer for Blender 5.2

Bring assets from **Battlefield: Bad Company 2** and the Xbox LIVE Arcade
release of **Battlefield 1943** into Blender from legally owned source media.
The extension browses the game archives, imports supported assets as native
Blender data, and never writes to the source file.

No game files are included.

## Highlights

- Browse PC retail, Xbox 360 XGD2, and BF1943 Xbox LIVE Frostbite archives
  from direct source files or a ZIP containing one source file.
- Import meshes, textures, terrain, and water from both platforms.
- Import PC characters and verified BF1943 characters with source materials,
  native armatures, and Actions.
- Import verified BF1943 composite vehicles with source textures and
  default-state part visibility.
- Assemble PC vehicles with named parts, authored transforms, and supported
  track components.
- Build PC and BF1943 map scenes with source-backed material keys, texture
  bindings, placed objects, terrain, and water.
- Extract unsupported resources as raw files for inspection.

Support varies by asset and platform. See the
[detailed support table](docs/user-guide.md#native-blender-import-support)
before planning a large import.

## Requirements

- **Blender 5.2**
- A legally owned **PC retail ISO**, **Xbox 360 XGD2 ISO**, or BF1943
  **Xbox LIVE STFS package**, either directly or as the only identifiable
  source member in a ZIP
- Free disk space for the reusable asset cache

PC retail extraction currently requires Windows because it uses the disc's
Windows Installer database and `expand.exe`. Xbox 360 XGD2 and STFS reading
are platform-independent.

> **BF1943 status:** The verified extensionless Xbox LIVE package exposes 85
> FbRB archives and 29,850 assets. All 1,209 MeshData resources parse; 1,505
> of 1,510 Xenon textures convert. The five format-code-5 textures remain
> explicit raw-only resources. All five map graphs resolve, all 31
> GrannyModel records and all 787 GrannyAnimation records decode, and
> representative maps and an Action pass Blender save/reopen validation. All
> seven version-117 Xbox shader databases parse to exact EOF; all-five-map
> material audits resolve 1,068 keys exactly plus four unique full-partition
> suffixes, and representative maps retain 91/207 source images after reopen.
> The Coral Sea carrier's three UV fields and base/AO/palette texture routes
> are verified from its linked layout, compiled programs, and Japanese/US deck
> landmarks. Complete Xenos shader arithmetic and general physical UV routing
> remain unsupported, so other Blender node roles and UV selection are
> documented preview heuristics rather than a claim of original-renderer
> parity.

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
2. Choose the PC/Xbox 360 ISO, BF1943 Xbox LIVE package, or a ZIP containing
   one of those source files. Scanning runs in the background, so Blender
   remains usable.
3. For a PC or BF1943 map, select a discovered level and choose **Import Map**.
   To browse that map's individual objects first, choose
   **Index Map Objects**.
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
  from the source file.
- A ZIP source is materialized once into the fingerprinted cache so the
  filesystem readers retain bounded random access. Allow free space for the
  member's full uncompressed size; later imports reuse that cached copy.
- Complete-map import supports PC retail and the verified BF1943 Xbox LIVE
  package. Xbox BC2 whole-map assembly remains unverified and disabled.
- BF1943 map geometry, placements, terrain, water, material identity, and
  texture selection are source-backed. The Coral Sea carrier additionally
  uses verified byte-24/28/32 base, AO, and palette routes. Complete Xbox
  compiled-shader arithmetic and general physical UV routing remain
  unsupported; the generated Blender graph records that limitation
  explicitly.
- Direct descriptor-backed BF1943 mesh sets can use the same indexed
  version-117 material databases. Xbox BC2 and PS3 direct materials remain
  geometry-only and unverified.
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
