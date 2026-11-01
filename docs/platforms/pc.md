# PC retail platform

## Container chain

The tested PC retail image uses this chain:

```text
ISO9660
  AUTORUN.DAT (Windows Installer database)
  C*.CAB (Microsoft Cabinet, LZX)
    installer record key
      installed *.fbrb path
```

The FbRB archives are not directly visible under their installed names on the
disc. The MSI database is required to connect an archive path to an installer
record and then to the cabinet holding that record.

## ISO9660 reader

The implemented reader uses 2048-byte sectors and starts volume-descriptor
processing at sector 16.

Verified/implemented behavior:

- Descriptor bytes `1..5` must equal `CD001`.
- Descriptor type `1` supplies the primary volume descriptor.
- The root directory record starts at byte `156` of that descriptor.
- Directory-record extent and size fields are stored in both little- and
  big-endian forms. The reader rejects records whose copies disagree.
- File version suffixes such as `;1` are removed.
- Lookup is case-insensitive and paths are normalized to `/`.
- Directory extents and file reads are bounded to the image size.

See [`iso9660.py`](../../bc2_frostbite_importer/core/iso9660.py).

## AUTORUN.DAT is an MSI database

Despite its extension, `AUTORUN.DAT` on the tested disc is queried through the
Windows Installer API. Four tables reconstruct the archive locations:

| Table | Used columns | Purpose |
|---|---|---|
| `Directory` | `Directory`, `Directory_Parent`, `DefaultDir` | Reconstruct the directory tree |
| `Component` | `Component`, `Directory_` | Map a component to its directory |
| `File` | `File`, `Component_`, `FileName`, `FileSize`, `Sequence` | Map an installer key to its installed path and size |
| `Media` | `DiskId`, `LastSequence`, `Cabinet` | Map file-sequence ranges to cabinets |

MSI names can contain both source and target forms separated by `:`, and short
and long names separated by `|`. The importer chooses the target/long form.
Installed paths are trimmed to the `Dist` or `Output` root when one is present.

Media rows define consecutive sequence ranges. A file's `Sequence` selects its
cabinet. Cabinet values beginning with `#` are embedded cabinets; the current
implementation only supports external cabinets present as ISO files.

The tested image exposes 625 unique `.fbrb` archives through this mapping.

See [`msi.py`](../../bc2_frostbite_importer/core/msi.py).

## Cabinet extraction

The tested cabinets use the `MSCF` format with LZX compression. Cabinet member
names are MSI file keys, for example an identifier resembling
`DistWin32..._fbrb`, rather than the final installed path.

The current extraction path:

1. Copies only the required cabinet from the ISO into the cache.
2. Calls Windows `expand.exe` with `-F:<installer-file-key>`.
3. Verifies that the extracted member exists and has the MSI-declared size.
4. Atomically moves it into the archive cache.

Consequences:

- PC retail extraction currently requires Windows.
- Scanning the disc only builds the archive list. An FbRB is extracted and
  indexed when selected, avoiding full-DVD expansion.
- Cache files are derivatives and can be regenerated from the ISO.

See [`source.py`](../../bc2_frostbite_importer/core/source.py).

## Complete multiplayer maps

Whole-map import indexes the level and shared multiplayer archives
automatically. The verified `MP_001` dependency set is:

```text
Dist/win32/levels/mp_001/level-00.fbrb
Dist/win32/levels/mp_001/terrain-00.fbrb
Dist/win32/levels/mp_common/level-00.fbrb
```

The shared archive is not optional: it supplies common blueprints, meshes, and
textures referenced by the level. Archive selection is exact and
case-insensitive, and duplicate asset paths prefer the requested level archive
order.

Complete-map assembly is currently PC-only because it uses the PC DX11 shader
database and has only been validated against the retail PC DBX graph. See
[Levels and complete maps](../formats/levels-and-maps.md).

## PC resource conventions

Observed conventions, not universal guarantees:

- MeshData fields and geometry are little-endian.
- TerrainHeightfield bounds and resolution are little-endian. Height-sample
  byte order remains independently detected.
- Bare `ITexture` headers are commonly 92 bytes and little-endian.
- DirectX UV orientation can require a V flip when creating Blender UVs.
- PC and Xbox versions of the tested MedKit geometry matched after byte-order
  handling.

## External-reference coverage

The audited BF3/BF4 MaxScripts use little-endian loose `.mesh`/`.chunk` files
or text dumps. They do not read ISO9660, Windows Installer tables, cabinets, or
FbRB archives and therefore add no evidence to the PC container chain. Their
different BF3/BF4 mesh offsets are a warning against transferring later-engine
layouts to BC2. See the
[external MaxScript audit](../maxscript-reference-audit.md).

## Export implications

Replacing a PC resource in place is not just an FbRB write:

1. The resource can change FbRB metadata, payload offsets, and compression.
2. The resulting archive can change the cabinet member size.
3. Rebuilding the cabinet can invalidate MSI file sizes and media layout.
4. Rebuilding or patching the ISO adds another allocation layer.

A first exporter should therefore target a separate mod workspace understood by
an existing mod loader. It should not rewrite retail ISOs, MSI databases, or
cabinets.
