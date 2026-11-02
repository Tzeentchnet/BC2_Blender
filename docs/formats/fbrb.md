# FbRB archives

FbRB is the common Frostbite archive layer reached after either platform's disc
containers have been resolved. Archive metadata is always gzip-compressed in
the tested data. The resource payload can be raw or a single gzip stream.

All documented integers in the FbRB header and decompressed metadata are
big-endian.

## Outer layout

| File offset | Size | Meaning |
|---:|---:|---|
| `0x00` | 4 | ASCII `FbRB` |
| `0x04` | 4 | compressed metadata size |
| `0x08` | variable | gzip-compressed metadata |
| `0x08 + compressed_size` | variable | raw or gzip-compressed payload |

The payload offset is therefore `8 + compressed_metadata_size`.

## Decompressed metadata

| Relative offset | Size | Meaning |
|---:|---:|---|
| `0x00` | 4 | unknown/version-like value; currently preserved only implicitly |
| `0x04` | 4 | string-table size |
| `0x08` | variable | NUL-terminated UTF-8 strings |
| after strings | 4 | entry count |
| after count | `count * 24` | entry records |
| after records | 1 | payload-compressed flag |
| next | 4 | declared decompressed payload size |

Each 24-byte entry contains six `uint32` values:

| Entry offset | Meaning |
|---:|---|
| `0x00` | path string-table offset |
| `0x04` | flags |
| `0x08` | offset in the decompressed payload |
| `0x0C` | resource size |
| `0x10` | alternate size; semantics are not yet established |
| `0x14` | resource-type string-table offset |

`flags == 0` identifies a deleted record in tested archives. Active entries can
have other values, including `0x10000`; readers should not reduce the field to
a guessed boolean when preserving metadata for export.

## Payload extraction

For an uncompressed payload:

```text
asset_file_offset = payload_offset + entry.offset
```

For a compressed payload, `entry.offset` is relative to the decompressed gzip
stream. The current reader opens the stream and discards exactly that many
bytes before copying `entry.size` bytes. This is intentionally bounded but is
not random access; repeated extraction from a large compressed payload can be
expensive.

One real archive declared a payload 64 bytes shorter than the end of its last
valid active entry. The importer accepts a discrepancy no larger than:

```text
max(1 MiB, declared_payload_size / 1000)
```

and expands the effective size to the last active entry. Larger discrepancies
remain errors. This is a compatibility policy, not proof of padding semantics.

See [`fbrb.py`](../../bc2_frostbite_importer/core/fbrb.py).

## Catalog behavior

The SQLite catalog stores:

- source fingerprint and platform;
- archive path, source locator, size, indexing state, payload facts, and error;
- active resource path, type, category, payload offset, both sizes, and flags.

Deleted records are intentionally omitted from the asset browser. Resource
categories are derived from the type string, with non-resource `.dbx`,
`.dbmanifest`, and `.xml` files categorized as level data.

The Indexed Assets browser also provides mutually exclusive semantic content
groups. These are **implemented path heuristics evaluated at query time**, not
stored Frostbite fields: known `Characters`/soldier, vehicle, weapon,
building, vegetation, level, effect, UI, and audio prefixes map to their named
groups; remaining `Objects` paths map to Props; paths outside all known groups
map to Other. Content group, resource category, and case-insensitive word
search predicates are combined in SQLite before ordering and pagination.

Mesh-set resources are resolved to a matching `MeshData` path using the pattern
`<base>_lod%_data.res`, preferring `_lod0_data.res` and then the same archive.
This is a naming convention and lookup heuristic, not a decoded mesh-set
reference graph.

See [`catalog.py`](../../bc2_frostbite_importer/core/catalog.py).

## External-reference coverage

The audited BF3/BF4 MaxScripts start from already extracted `.mesh`, `.chunk`,
or text-dump files. They contain no FbRB signature, metadata, compression,
entry, deletion, or string-table handling. They neither confirm nor conflict
with this archive implementation. See the
[external MaxScript audit](../maxscript-reference-audit.md).

The separately audited Frostbite 1 packer does write FbRB archives.
**Verified (synthetic)** output was readable in both raw- and gzip-payload
modes, but the packer is reconstructive rather than lossless: it hard-codes
metadata word zero to `2`, assigns flags from whether a file is empty, makes
`alternate_size == size`, removes payload padding, regenerates strings and
entry order from the filesystem, omits unknown filename extensions, and does
not fix the gzip timestamp.

One **Verified (PC retail)** `MP_001` archive also used metadata word `2`,
flag `0x10000` for every active entry, equal size fields, contiguous active
ranges, and zero-byte deleted records. That agreement is sample-specific and
does not establish other archives, builds, or Xbox 360 output. See the
[external writer audit](../external-writer-reference-audit.md).

## Writer requirements

Before implementing FbRB output, establish and test:

1. The meaning of the metadata's first word and `alternate_size`.
2. Alignment rules between resources.
3. Whether deleted records must retain their old ranges.
4. How each game build chooses flags.
5. Deterministic gzip settings expected by mod tools.
6. Whether consumers tolerate metadata or payload size changes.

A lossless editor should retain all unknown metadata, original string order,
entry order, flags, and padding. Rebuilding only from the current catalog would
discard information and is not sufficient for safe round-tripping. The
external packer demonstrates a readable reconstruction algorithm, not a
replacement for these preservation requirements.
