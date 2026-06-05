<img width="1920" height="1080" alt="Screenshot (1042)" src="https://github.com/user-attachments/assets/d2e96a82-04f0-43c9-9566-864c149d85cd" />
# Saints Row Package Tool (Python port)

A Python port of the package (`.vpp`) read/write core from
[gibbed/Gibbed.SaintsRow2](https://github.com/gibbed/Gibbed.SaintsRow2),
with **Xbox 360 big-endian support** and **zlib compress/decompress**, plus a
Tkinter GUI.

## Files

| File | What it is |
|------|-----------|
| `sr2_package.py` | The core: `PackageFile.read / extract_entry / extract_all / from_directory / write / write_payloads`. No third-party deps. |
| `sr2_gui.py` | Tkinter GUI: open, list, **edit** (.lua/.xtbl/text), extract, build, save. |
| `sr2_cli.py` | Command-line wrapper for headless testing. |

## Supported file names

All platform suffixes are recognized for opening and saving:
`.vpp`, `.vpp_xbox2` (Xbox 360), `.vpp_pc` (PC), `.vpp_ps3`, and the matching
`.str2*` streaming containers. The on-disk layout is identical across
platforms; the suffix only signals the source, and endianness is detected from
the file's magic when reading.

## Requirements

Python 3.10+. Only the standard library is used. The GUI needs Tkinter, which
ships with Python on Windows/macOS; on Debian/Ubuntu install `python3-tk`.

## Quick start

GUI:

```
python Saints_Row_Package_Tool.py
```

## Format notes (version 4)

- 384-byte header, then **index / names / extensions / data** blocks, each
  padded to a 2048-byte boundary.
- Magic `0x51890ACE` = little-endian, `0xCE0A8951` = big-endian (Xbox 360).
- Header flag bit 0 (`Flags & 1`) set => entry data is zlib-compressed. The
  360 retail packages use this.
- Per-entry index is 28 bytes: name offset, ext offset, unknown, data offset,
  uncompressed size, compressed size, unknown. `compressed_size == -1` means
  that entry is stored raw.

## Testing checklist for the 360 `.vpp`

1. `info` should report **big-endian (360)** and **compressed**, with a
   sensible file list. If magic doesn't match, the file may be a different
   container (e.g. `.str2` streaming packages) or LZX-packed rather than zlib.
2. `extract` and check what the `.lua` files actually are. If they're plain
   text, the editor phase is trivial. If they're Lua bytecode, you'll need a
   decompiler/recompiler for the matching Lua version.
3. **Repack fidelity is the risk area.** gibbed's original `Write` only emitted
   *uncompressed* packages; this port adds real zlib compression for the 360
   layout, which the original tool never round-tripped. So: repack an
   unmodified extraction, and confirm the game loads it before trusting edits.
   If the game rejects it, the likely culprits are the `flags` value, the
   per-entry alignment of the compressed data region, or the
   `UncompressedDataSize` accounting in the header — all isolated in
   `PackageFile.write`.

## Editing .lua / .xtbl in the GUI

The `.lua` and `.xtbl` files in SR2 360 packages are plain text (ASCII, CRLF),
so they edit directly:

1. Open a package. Every entry is decompressed into memory.
2. Double-click a `.lua`/`.xtbl` entry (or select and click **Edit**) to open
   it in a text tab. Other extensions can be opened as text on confirmation.
3. Edit, then **Save package as\u2026**. All entries are repacked from memory
   (edited buffers plus untouched ones) via `write_payloads`. The source
   platform (endianness) and flags are preserved, and the build checkboxes let
   you re-target PC/360 or toggle compression on save.

Newline style is preserved: files load and save as CRLF, so an open-and-save
with no edits is byte-identical to the original.

**Repack fidelity note still applies.** gibbed's original tool only ever
emitted *uncompressed* packages; the compressed 360 layout here is an
extension. The round-trip is verified byte-for-byte against the data, but
always confirm an edited package loads in-game before trusting it. If the game

rejects a repack, the likely culprits (header flags, compressed-region
alignment, `UncompressedDataSize` accounting) are isolated in
`PackageFile._write_core`.

**Saints Row 1, 2 & 3 Supported.** This tool supports v3, v4 & v6 .vpp archives.
