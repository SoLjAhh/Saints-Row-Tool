"""
Saints Row 2 Package Tool - single-file GUI

A standalone Tkinter application to open, extract, edit, and rebuild Saints Row
2 package archives (.vpp / .vpp_xbox2 / .vpp_pc and .str2* containers), with
Xbox 360 big-endian support and zlib compression. Edit .lua / .xtbl (and other
text) entries in tabbed panes and save the whole package back.

This file is fully self-contained: the package read/write core and the GUI are
combined here, with no third-party dependencies. Tkinter ships with Python on
Windows/macOS; on Debian/Ubuntu install it with `sudo apt install python3-tk`.

Run it with:  python sr2_tool.py
"""

# ============================================================================
#  PACKAGE CORE  (port of gibbed/Gibbed.SaintsRow2 PackageFile + Extractor)
# ============================================================================

from __future__ import annotations

import io
import os
import struct
import zlib
from dataclasses import dataclass, field

MAGIC_LE = 0x51890ACE
MAGIC_BE = 0xCE0A8951

# Two package generations share the magic but differ in layout:
#   v4  - Saints Row 2 / Red Faction 3 era. 28-byte index entries, separate
#         names + extensions blobs, optional zlib (whole-entry).
#   v6  - Saints Row: The Third era. 24-byte index entries, single names blob
#         that already includes the extension, no extensions blob.
VERSION_V3 = 3
VERSION_V4 = 4
VERSION_V6 = 6
SUPPORTED_VERSION = VERSION_V4  # default for newly-built packages
SUPPORTED_VERSIONS = (VERSION_V3, VERSION_V4, VERSION_V6)

BLOCK_ALIGN = 2048
INDEX_ENTRY_SIZE = 28        # v4
INDEX_ENTRY_SIZE_V6 = 24     # v6

FLAG_COMPRESSED = 1
FLAG_PRELOAD = 2  # gibbed's writer hardcodes this; meaning is uncertain.

# Recognized package file extensions across platforms. The data layout is the
# same; the suffix just signals the source platform.
#   .vpp        - generic
#   .vpp_xbox2  - Xbox 360
#   .vpp_pc     - PC
#   .str2*      - streaming sub-packages (same container)
PACKAGE_SUFFIXES = (
    ".vpp_xbox2", ".vpp_pc", ".vpp_ps3", ".vpp",
    ".str2_xbox2", ".str2_pc", ".str2_ps3", ".str2",
)


def is_package_filename(name: str) -> bool:
    """True if name ends with a recognized package suffix (case-insensitive)."""
    lower = name.lower()
    return any(lower.endswith(s) for s in PACKAGE_SUFFIXES)

# Header field offsets (within the 384-byte header).
_OFF_MAGIC = 0x000
_OFF_VERSION = 0x004
_OFF_FLAGS = 0x14C
_OFF_INDEX_COUNT = 0x154
_OFF_PACKAGE_SIZE = 0x158
_OFF_INDEX_SIZE = 0x15C
_OFF_NAMES_SIZE = 0x160
_OFF_EXTENSIONS_SIZE = 0x164
_OFF_UNCOMPRESSED_DATA_SIZE = 0x168
_OFF_COMPRESSED_DATA_SIZE = 0x16C
HEADER_SIZE = 384


class PackageError(Exception):
    """Base error for package operations."""


class NotAPackageFileError(PackageError):
    """Stream does not start with a recognized package magic."""


class UnsupportedVersionError(PackageError):
    """Package version is not supported (only version 4 is)."""


def align_up(value: int, alignment: int = BLOCK_ALIGN) -> int:
    """Round value up to the next multiple of alignment."""
    return (value + alignment - 1) & ~(alignment - 1)


def _read_asciiz(buf: bytes, offset: int) -> str:
    end = buf.find(b"\x00", offset)
    if end == -1:
        end = len(buf)
    return buf[offset:end].decode("ascii", errors="replace")


@dataclass
class PackageEntry:
    name: str
    extension: str
    offset: int = 0           # absolute offset of this entry's data in the file
    uncompressed_size: int = 0
    compressed_size: int = -1  # -1 == stored raw (not zlib-compressed)
    unknown08: int = 0
    unknown1c: int = 0
    crc: int = 0              # v3 per-file hash field (preserved on round-trip)

    @property
    def filename(self) -> str:
        if self.extension:
            return f"{self.name}.{self.extension}"
        return self.name

    @property
    def is_compressed(self) -> bool:
        return self.compressed_size != -1

    # Extensions known to be plain text in SR2 packages (editable as-is).
    TEXT_EXTENSIONS = frozenset({
        "lua", "xtbl", "xml", "txt", "cts", "csv", "ccmesh_xml",
    })

    @property
    def looks_textual(self) -> bool:
        """Best-guess whether this entry is editable as text (by extension)."""
        return self.extension.lower() in self.TEXT_EXTENSIONS


@dataclass
class PackageFile:
    version: int = SUPPORTED_VERSION
    flags: int = FLAG_PRELOAD
    big_endian: bool = False
    entries: list[PackageEntry] = field(default_factory=list)
    # The original 384-byte header as read, so a repack can preserve cosmetic
    # fields (e.g. the descriptive comment Volition's tools write between the
    # version and the structural fields). None for freshly-built packages.
    raw_header: bytes | None = None

    @property
    def _endian(self) -> str:
        return ">" if self.big_endian else "<"

    # ----------------------------------------------------------------- read

    @classmethod
    def read(cls, stream: io.BufferedReader) -> "PackageFile":
        """Parse a package's directory structure from an open binary stream.

        Entry data is NOT read here; only the index. Use extract_entry /
        extract_all to pull file data out afterward. Dispatches on the package
        version (4 = Saints Row 2 era, 6 = Saints Row: The Third era).
        """
        header = stream.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            raise NotAPackageFileError("File too small to contain a header.")

        magic = struct.unpack_from("<I", header, _OFF_MAGIC)[0]
        if magic == MAGIC_LE:
            big_endian = False
        elif magic == MAGIC_BE:
            big_endian = True
        else:
            raise NotAPackageFileError(f"Bad magic: 0x{magic:08X}")

        e = ">" if big_endian else "<"
        version = struct.unpack_from(e + "I", header, _OFF_VERSION)[0]

        if version == VERSION_V3:
            return cls._read_v3(stream, header, big_endian, e)
        if version == VERSION_V4:
            return cls._read_v4(stream, header, big_endian, e)
        if version == VERSION_V6:
            return cls._read_v6(stream, header, big_endian, e)
        raise UnsupportedVersionError(
            f"Unsupported version: {version} (supported: {SUPPORTED_VERSIONS})")

    @classmethod
    def _read_v3(cls, stream, header, big_endian, e) -> "PackageFile":
        """Saints Row 1 / Red Faction era packages.

        28-byte index entries, but unlike v4 there is no extensions block; the
        names blob holds full filenames. Entry layout (big-endian on 360):
          name_offset(u32), zero(u32), data_offset(u32, from data block),
          crc(u32), uncompressed_size(u32), compressed_size(u32), zero(u32)
        flag bit 0 set => entries are zlib-compressed. The stored data_offset
        is authoritative (entries are 2048-aligned but not strictly packed).
        Block order: header / index / names / data, each 2048-aligned.
        """
        flags = struct.unpack_from(e + "I", header, _OFF_FLAGS)[0]
        index_count = struct.unpack_from(e + "i", header, _OFF_INDEX_COUNT)[0]
        index_size = struct.unpack_from(e + "i", header, _OFF_INDEX_SIZE)[0]
        names_size = struct.unpack_from(e + "i", header, _OFF_NAMES_SIZE)[0]

        stream.seek(align_up(HEADER_SIZE))
        index_buf = _read_block(stream, index_size)
        names_buf = _read_block(stream, names_size)
        data_base = stream.tell()

        pkg = cls(version=VERSION_V3, flags=flags, big_endian=big_endian,
                  raw_header=bytes(header))
        compressed_package = (flags & FLAG_COMPRESSED) == FLAG_COMPRESSED

        # In SR1 the stored data_offset is a *logical* (uncompressed) offset
        # used by the game after decompression, not a file position. The
        # compressed blobs are physically packed in order, each padded to
        # align(compressed_size, 2048), so we track the physical position
        # ourselves.
        physical = data_base
        for i in range(index_count):
            off = i * INDEX_ENTRY_SIZE  # 28 bytes
            (name_off, zero1, data_offset, crc,
             uncompressed_size, compressed_size, zero2) = struct.unpack_from(
                e + "IIIIIII", index_buf, off)

            full_name = _read_asciiz(names_buf, name_off)
            name, _, ext = full_name.rpartition(".")
            if not name:
                name, ext = full_name, ""

            if compressed_package:
                entry_offset = physical
                physical += align_up(compressed_size)
                stored_csize = compressed_size
            else:
                # Uncompressed v3: stored offset is the physical position.
                entry_offset = data_base + data_offset
                stored_csize = -1

            pkg.entries.append(PackageEntry(
                name=name,
                extension=ext,
                offset=entry_offset,
                uncompressed_size=uncompressed_size,
                compressed_size=stored_csize,
                crc=crc,
            ))

        return pkg

    @classmethod
    def _read_v4(cls, stream, header, big_endian, e) -> "PackageFile":
        flags = struct.unpack_from(e + "I", header, _OFF_FLAGS)[0]
        index_count = struct.unpack_from(e + "i", header, _OFF_INDEX_COUNT)[0]
        index_size = struct.unpack_from(e + "i", header, _OFF_INDEX_SIZE)[0]
        names_size = struct.unpack_from(e + "i", header, _OFF_NAMES_SIZE)[0]
        ext_size = struct.unpack_from(e + "i", header, _OFF_EXTENSIONS_SIZE)[0]

        # Header occupies a full 2048-byte aligned block; each subsequent
        # block (index, names, extensions) is also padded to 2048 bytes.
        stream.seek(align_up(HEADER_SIZE))
        index_buf = _read_block(stream, index_size)
        names_buf = _read_block(stream, names_size)
        ext_buf = _read_block(stream, ext_size)

        base_offset = stream.tell()

        pkg = cls(version=VERSION_V4, flags=flags, big_endian=big_endian,
                  raw_header=bytes(header))
        compressed_package = (flags & FLAG_COMPRESSED) == FLAG_COMPRESSED

        for i in range(index_count):
            off = i * INDEX_ENTRY_SIZE
            (name_off, ext_off, unknown08, raw_offset,
             uncompressed_size, compressed_size, unknown1c) = struct.unpack_from(
                e + "iiIiiiI", index_buf, off
            )

            if compressed_package:
                # In compressed (360) packages the stored offsets are not
                # usable; data follows sequentially, each entry aligned.
                entry_offset = base_offset
                base_offset += align_up(compressed_size)
            else:
                entry_offset = raw_offset + base_offset

            entry = PackageEntry(
                name=_read_asciiz(names_buf, name_off),
                extension=_read_asciiz(ext_buf, ext_off),
                offset=entry_offset,
                uncompressed_size=uncompressed_size,
                compressed_size=compressed_size,
                unknown08=unknown08,
                unknown1c=unknown1c,
            )

            if entry.unknown08 != 0 or entry.unknown1c != 0:
                raise PackageError(
                    f"Unexpected nonzero unknown fields in entry {entry.filename}"
                )
            if not compressed_package and entry.compressed_size != -1:
                raise PackageError(
                    f"Uncompressed package but entry {entry.filename} "
                    f"has compressed_size={entry.compressed_size}"
                )

            pkg.entries.append(entry)

        return pkg

    @classmethod
    def _read_v6(cls, stream, header, big_endian, e) -> "PackageFile":
        """Saints Row: The Third era packages.

        Header reuses the v4 field offsets, but:
          - index entries are 24 bytes:
              name_offset(u32), unknown(u32), data_offset(u32, relative to the
              data block), uncompressed_size(u32), compressed_size(u32,
              0xFFFFFFFF = stored raw), flags/unknown(u32)
          - there is no extensions block; the names blob holds full filenames
            (e.g. "Mayhem.str2_xbox2"), and 0x164 instead carries data_size.
        Block order: header / index / names / data, each 2048-aligned.
        """
        flags = struct.unpack_from(e + "I", header, _OFF_FLAGS)[0]
        index_count = struct.unpack_from(e + "i", header, _OFF_INDEX_COUNT)[0]
        index_size = struct.unpack_from(e + "i", header, _OFF_INDEX_SIZE)[0]
        names_size = struct.unpack_from(e + "i", header, _OFF_NAMES_SIZE)[0]

        stream.seek(align_up(HEADER_SIZE))
        index_buf = _read_block(stream, index_size)
        names_buf = _read_block(stream, names_size)
        data_base = stream.tell()

        pkg = cls(version=VERSION_V6, flags=flags, big_endian=big_endian,
                  raw_header=bytes(header))

        for i in range(index_count):
            off = i * INDEX_ENTRY_SIZE_V6
            (name_off, unknown04, data_offset, uncompressed_size,
             compressed_size, unknown14) = struct.unpack_from(
                e + "IIIIiI", index_buf, off)

            # compressed_size 0xFFFFFFFF (-1 as signed) == stored raw.
            full_name = _read_asciiz(names_buf, name_off)
            name, _, ext = full_name.rpartition(".")
            if not name:  # no dot in the name
                name, ext = full_name, ""

            pkg.entries.append(PackageEntry(
                name=name,
                extension=ext,
                offset=data_base + data_offset,
                uncompressed_size=uncompressed_size,
                compressed_size=compressed_size,
                unknown08=unknown04,
                unknown1c=unknown14,
            ))

        return pkg

    # -------------------------------------------------------------- extract

    def extract_raw(self, stream: io.BufferedReader, entry: PackageEntry) -> bytes:
        """Return the entry's payload exactly as stored on disk.

        For compressed entries this is the raw zlib stream (compressed_size
        bytes); for raw entries it's the uncompressed bytes. Preserving these
        verbatim lets a no-edit repack reproduce the original byte-for-byte
        instead of recompressing (which changes sizes and offsets).
        """
        stream.seek(entry.offset)
        if entry.is_compressed:
            return stream.read(entry.compressed_size)
        return stream.read(entry.uncompressed_size)

    def extract_entry(self, stream: io.BufferedReader, entry: PackageEntry) -> bytes:
        """Return the decompressed bytes of a single entry."""
        stream.seek(entry.offset)
        if not entry.is_compressed:
            return stream.read(entry.uncompressed_size)

        # zlib stream. Feed exactly compressed_size bytes to a decompress
        # object and flush. (The one-shot zlib.decompress raises a spurious
        # "truncated" error on some Volition v3 streams; the object form with
        # an explicit flush handles them correctly.)
        stream.seek(entry.offset)
        compressed = stream.read(entry.compressed_size)
        decompressor = zlib.decompressobj()
        data = decompressor.decompress(compressed)
        data += decompressor.flush()
        if len(data) != entry.uncompressed_size:
            raise PackageError(
                f"{entry.filename}: inflated {len(data)} bytes, "
                f"expected {entry.uncompressed_size}"
            )
        return data

    def extract_all(self, stream: io.BufferedReader, out_dir: str,
                    progress=None) -> tuple[int, int]:
        """Extract every entry to out_dir. Returns (succeeded, failed)."""
        os.makedirs(out_dir, exist_ok=True)
        succeeded = failed = 0
        total = len(self.entries)
        for i, entry in enumerate(self.entries, 1):
            try:
                data = self.extract_entry(stream, entry)
                with open(os.path.join(out_dir, entry.filename), "wb") as f:
                    f.write(data)
                succeeded += 1
            except Exception as exc:  # keep going; report per-file
                failed += 1
                if progress:
                    progress(i, total, f"FAILED {entry.filename}: {exc}")
                continue
            if progress:
                progress(i, total, entry.filename)
        return succeeded, failed

    # ----------------------------------------------------------------- write

    @staticmethod
    def from_directory(in_dir: str, big_endian: bool = True,
                       compress: bool = True) -> "PackageFile":
        """Build a PackageFile listing every file in in_dir (non-recursive)."""
        pkg = PackageFile(big_endian=big_endian)
        pkg.flags = FLAG_PRELOAD | (FLAG_COMPRESSED if compress else 0)
        for fn in sorted(os.listdir(in_dir)):
            full = os.path.join(in_dir, fn)
            if not os.path.isfile(full):
                continue
            name, _, ext = fn.rpartition(".")
            if not name:  # no extension
                name, ext = fn, ""
            pkg.entries.append(PackageEntry(name=name, extension=ext))
        return pkg

    def write(self, out_stream: io.BufferedWriter, in_dir: str,
              progress=None) -> None:
        """Write a package, reading each entry's raw data from in_dir.

        If self.flags has FLAG_COMPRESSED set, each entry is zlib-deflated and
        the per-entry compressed_size is recorded; otherwise stored raw with
        compressed_size = -1.
        """
        def provider(entry: PackageEntry) -> bytes:
            with open(os.path.join(in_dir, entry.filename), "rb") as f:
                return f.read()

        self._write_core(out_stream, provider, progress)

    def write_payloads(self, out_stream: io.BufferedWriter,
                       payloads: dict[str, bytes], progress=None) -> None:
        """Write a package using in-memory raw bytes keyed by entry filename.

        Convenient when filenames are unique. For packages that may contain
        duplicate filenames (some SR1 packages do), prefer write_payload_list,
        which keys by position and so never collapses duplicates.
        """
        def provider(entry: PackageEntry) -> bytes:
            return payloads[entry.filename]

        self._write_core(out_stream, provider, progress)

    def write_payload_list(self, out_stream: io.BufferedWriter,
                           payloads: list[bytes], progress=None) -> None:
        """Write a package using in-memory raw bytes, one per entry by index.

        This is the editor's repack path: extract every entry once into a list
        parallel to self.entries, edit some in place, then repack without a
        temp folder. Keying by index (not filename) preserves entries that
        share a filename.
        """
        if len(payloads) != len(self.entries):
            raise PackageError(
                f"payload count {len(payloads)} != entry count {len(self.entries)}")
        it = iter(payloads)
        # The writer iterates entries in order, so a positional iterator lines
        # each payload up with its entry.
        order = {id(entry): i for i, entry in enumerate(self.entries)}

        def provider(entry: PackageEntry) -> bytes:
            return payloads[order[id(entry)]]

        self._write_core(out_stream, provider, progress)

    def repack(self, out_stream: io.BufferedWriter, prepared, progress=None) -> None:
        """Repack from per-entry prepared payloads, parallel to self.entries.

        Each item is a (kind, data) tuple:
          ("stored", bytes) - already in final on-disk form (e.g. the original
                              zlib stream); written verbatim. Use this for
                              untouched entries so the output matches the source
                              byte-for-byte.
          ("raw", bytes)    - uncompressed bytes; compressed here if the package
                              is compressed. Use this for edited entries.

        This is the correct path for repacking an opened package: it avoids
        recompressing untouched entries, which would change their bytes, sizes,
        and offsets relative to the original.
        """
        if len(prepared) != len(self.entries):
            raise PackageError(
                f"payload count {len(prepared)} != entry count {len(self.entries)}")
        compress = (self.flags & FLAG_COMPRESSED) == FLAG_COMPRESSED

        final = []  # (stored_bytes, uncompressed_size)
        total = len(self.entries)
        for i, (entry, (kind, data)) in enumerate(
                zip(self.entries, prepared), 1):
            if kind == "stored":
                # Verbatim. uncompressed_size comes from the entry as read.
                final.append((data, entry.uncompressed_size))
            elif kind == "raw":
                if compress:
                    stored = zlib.compress(data, 9)
                else:
                    stored = data
                final.append((stored, len(data)))
            else:
                raise PackageError(f"bad prepared kind: {kind!r}")
            if progress:
                progress(i, total, f"packing {entry.filename}")

        if self.version == VERSION_V4:
            self._write_v4_prepared(out_stream, final)
        elif self.version == VERSION_V3:
            self._write_v3_prepared(out_stream, final)
        elif self.version == VERSION_V6:
            self._write_v6_prepared(out_stream, final)
        else:
            raise UnsupportedVersionError(f"version {self.version}")

    def _write_core(self, out_stream: io.BufferedWriter, payload_provider,
                    progress=None) -> None:
        if self.version == VERSION_V6:
            self._write_v6_core(out_stream, payload_provider, progress)
        elif self.version == VERSION_V3:
            self._write_v3_core(out_stream, payload_provider, progress)
        else:
            self._write_v4_core(out_stream, payload_provider, progress)

    def _write_v4_core(self, out_stream: io.BufferedWriter, payload_provider,
                       progress=None) -> None:
        """payload_provider(entry) returns RAW (uncompressed) bytes; this path
        recompresses. Used by from_directory builds. For repacking an opened
        package without recompressing, the GUI uses _write_prepared instead.
        """
        e = self._endian
        compress = (self.flags & FLAG_COMPRESSED) == FLAG_COMPRESSED

        prepared = []  # (stored_bytes, uncompressed_size)
        total = len(self.entries)
        for i, entry in enumerate(self.entries, 1):
            raw = payload_provider(entry)
            if compress:
                stored = zlib.compress(raw, 9)
            else:
                stored = raw
            prepared.append((stored, len(raw)))
            if progress:
                progress(i, total, f"packing {entry.filename}")

        self._write_v4_prepared(out_stream, prepared)

    def _write_v4_prepared(self, out_stream, prepared) -> None:
        """Write a v4 package from already-final payloads.

        `prepared` is a list parallel to self.entries of (stored_bytes,
        uncompressed_size). stored_bytes is what physically goes on disk (the
        zlib stream for a compressed package, or the raw bytes otherwise). This
        does NOT recompress, so reusing the original stored bytes reproduces
        the source package faithfully.
        """
        e = self._endian
        compress = (self.flags & FLAG_COMPRESSED) == FLAG_COMPRESSED

        # Names / extensions blobs, in first-seen order (matches Volition's
        # tools; sorting would scramble the stored offsets relative to theirs).
        names_blob, name_offsets = _ordered_string_blob(
            [en.name for en in self.entries])
        ext_blob, ext_offsets = _ordered_string_blob(
            [en.extension for en in self.entries])

        # Index + data layout.
        index = io.BytesIO()
        running_offset = 0
        uncompressed_total = 0
        compressed_total = 0
        data_align = BLOCK_ALIGN if compress else 16
        for entry, (stored, usize) in zip(self.entries, prepared):
            csize = len(stored) if compress else -1
            index.write(struct.pack(
                e + "iiIiiiI",
                name_offsets[entry.name],
                ext_offsets[entry.extension],
                entry.unknown08,
                running_offset,
                usize,
                csize,
                entry.unknown1c,
            ))
            running_offset += align_up(len(stored), data_align)
            uncompressed_total += align_up(usize, 16)
            if compress:
                compressed_total += len(stored)
        index_blob = index.getvalue()

        # Assemble the data region so we can measure the true physical size
        # (the last entry is NOT padded out, matching Volition's output).
        data_region = io.BytesIO()
        for i, (stored, _usize) in enumerate(prepared):
            data_region.write(stored)
            if i != len(prepared) - 1:
                pad = align_up(len(stored), data_align) - len(stored)
                if pad:
                    data_region.write(b"\x00" * pad)
        data_bytes = data_region.getvalue()

        pre_data = (align_up(HEADER_SIZE)
                    + align_up(len(index_blob))
                    + align_up(len(names_blob))
                    + align_up(len(ext_blob)))
        physical_size = pre_data + len(data_bytes)

        # Seed from the original header (preserves Volition's cosmetic comment
        # region between version and the structural fields) when available and
        # the endianness still matches; otherwise start from zeros.
        header = _seed_header(self.raw_header, self.big_endian)
        struct.pack_into("<I", header, _OFF_MAGIC,
                         MAGIC_BE if self.big_endian else MAGIC_LE)
        struct.pack_into(e + "I", header, _OFF_VERSION, VERSION_V4)
        struct.pack_into(e + "I", header, _OFF_FLAGS, self.flags)
        struct.pack_into(e + "i", header, _OFF_INDEX_COUNT, len(self.entries))
        struct.pack_into(e + "i", header, _OFF_INDEX_SIZE, len(index_blob))
        struct.pack_into(e + "i", header, _OFF_NAMES_SIZE, len(names_blob))
        struct.pack_into(e + "i", header, _OFF_EXTENSIONS_SIZE, len(ext_blob))
        # PackageSize is the true physical byte length of the file.
        struct.pack_into(e + "i", header, _OFF_PACKAGE_SIZE, physical_size)
        # Preserve the original uncompressed-data-size when we seeded from the
        # source header (its exact derivation in Volition's tools isn't fully
        # known); only compute it for freshly-built packages.
        if self.raw_header is None:
            struct.pack_into(e + "i", header, _OFF_UNCOMPRESSED_DATA_SIZE,
                             uncompressed_total)
        if compress:
            # The compressed-data-size field is the physical byte length of the
            # data region (data start to EOF), matching Volition's tools.
            struct.pack_into(e + "I", header, _OFF_COMPRESSED_DATA_SIZE,
                             len(data_bytes))
        else:
            struct.pack_into(e + "I", header, _OFF_COMPRESSED_DATA_SIZE,
                             0xFFFFFFFF)

        _write_block(out_stream, bytes(header))
        _write_block(out_stream, index_blob)
        _write_block(out_stream, names_blob)
        _write_block(out_stream, ext_blob)
        # Data region written verbatim (already padded internally; no trailing
        # pad after the final entry).
        out_stream.write(data_bytes)

    def _write_v6_core(self, out_stream: io.BufferedWriter, payload_provider,
                       progress=None) -> None:
        """Build v6 from raw payloads (recompresses nothing; v6 stores raw)."""
        prepared = []
        total = len(self.entries)
        for i, entry in enumerate(self.entries, 1):
            raw = payload_provider(entry)
            prepared.append((raw, len(raw)))
            if progress:
                progress(i, total, f"packing {entry.filename}")
        self._write_v6_prepared(out_stream, prepared)

    def _write_v6_prepared(self, out_stream, prepared) -> None:
        """Write a Saints Row: The Third (v6) package from final payloads.

        Entries are stored raw, data region sequential and 2048-aligned. Names
        blob holds full filenames; there is no extensions blob.
        """
        e = self._endian

        names_blob = io.BytesIO()
        name_offsets: list[int] = []
        for entry in self.entries:
            name_offsets.append(names_blob.tell())
            names_blob.write(entry.filename.encode("ascii") + b"\x00")
        names_blob = names_blob.getvalue()

        index = io.BytesIO()
        running = 0
        for entry, (stored, usize), noff in zip(self.entries, prepared, name_offsets):
            index.write(struct.pack(
                e + "IIIIiI",
                noff,
                entry.unknown08,
                running,
                usize,
                -1,                         # compressed_size: raw
                entry.unknown1c,
            ))
            running += align_up(len(stored))
        index_blob = index.getvalue()

        header = _seed_header(self.raw_header, self.big_endian)
        struct.pack_into("<I", header, _OFF_MAGIC,
                         MAGIC_BE if self.big_endian else MAGIC_LE)
        struct.pack_into(e + "I", header, _OFF_VERSION, VERSION_V6)
        struct.pack_into(e + "I", header, _OFF_FLAGS, self.flags)
        struct.pack_into(e + "i", header, _OFF_INDEX_COUNT, len(self.entries))
        struct.pack_into(e + "i", header, _OFF_INDEX_SIZE, len(index_blob))
        struct.pack_into(e + "i", header, _OFF_NAMES_SIZE, len(names_blob))
        struct.pack_into(e + "I", header, _OFF_UNCOMPRESSED_DATA_SIZE, 0xFFFFFFFF)

        pre_data = (align_up(HEADER_SIZE) + align_up(len(index_blob))
                    + align_up(len(names_blob)))
        # physical size: last entry not padded out
        body = io.BytesIO()
        for i, (stored, _u) in enumerate(prepared):
            body.write(stored)
            if i != len(prepared) - 1:
                pad = align_up(len(stored)) - len(stored)
                if pad:
                    body.write(b"\x00" * pad)
        data_bytes = body.getvalue()
        # 0x164 holds the true data-region byte length (data start to EOF).
        struct.pack_into(e + "i", header, _OFF_EXTENSIONS_SIZE, len(data_bytes))
        struct.pack_into(e + "i", header, _OFF_PACKAGE_SIZE,
                         pre_data + len(data_bytes))

        _write_block(out_stream, bytes(header))
        _write_block(out_stream, index_blob)
        _write_block(out_stream, names_blob)
        out_stream.write(data_bytes)

    def _write_v3_core(self, out_stream: io.BufferedWriter, payload_provider,
                       progress=None) -> None:
        """Build v3 from raw payloads (recompresses if FLAG_COMPRESSED)."""
        e = self._endian
        compress = (self.flags & FLAG_COMPRESSED) == FLAG_COMPRESSED
        prepared = []
        total = len(self.entries)
        for i, entry in enumerate(self.entries, 1):
            raw = payload_provider(entry)
            stored = zlib.compress(raw, 9) if compress else raw
            prepared.append((stored, len(raw)))
            if progress:
                progress(i, total, f"packing {entry.filename}")
        self._write_v3_prepared(out_stream, prepared)

    def _write_v3_prepared(self, out_stream, prepared) -> None:
        """Write a Saints Row 1 / Red Faction era (v3) package from final
        payloads. 28-byte entries, full filenames in the names blob (no
        extensions block). Each entry reserves align(uncompressed_size, 2048)
        bytes (the compressed stream sits at the start of that region); the
        file ends on a 2048 boundary. The per-file crc field is preserved.
        """
        e = self._endian
        compress = (self.flags & FLAG_COMPRESSED) == FLAG_COMPRESSED

        names_io = io.BytesIO()
        name_offsets: list[int] = []
        for entry in self.entries:
            name_offsets.append(names_io.tell())
            names_io.write(entry.filename.encode("ascii") + b"\x00")
        names_blob = names_io.getvalue()

        index = io.BytesIO()
        logical = 0   # data_offset field: cumulative align(uncompressed_size)
        for entry, (stored, usize), noff in zip(self.entries, prepared, name_offsets):
            csize = len(stored) if compress else -1
            index.write(struct.pack(
                e + "IIIIIII",
                noff,
                0,
                logical,
                entry.crc & 0xFFFFFFFF,
                usize,
                (csize & 0xFFFFFFFF) if compress else 0xFFFFFFFF,
                0,
            ))
            # The stored data_offset is a *logical* (uncompressed) offset; the
            # game uses it after decompression. It advances by the uncompressed
            # size, even though what's physically stored is the compressed blob.
            logical += align_up(usize)
        index_blob = index.getvalue()

        header = _seed_header(self.raw_header, self.big_endian)
        struct.pack_into("<I", header, _OFF_MAGIC,
                         MAGIC_BE if self.big_endian else MAGIC_LE)
        struct.pack_into(e + "I", header, _OFF_VERSION, VERSION_V3)
        struct.pack_into(e + "I", header, _OFF_FLAGS, self.flags)
        struct.pack_into(e + "i", header, _OFF_INDEX_COUNT, len(self.entries))
        struct.pack_into(e + "i", header, _OFF_INDEX_SIZE, len(index_blob))
        struct.pack_into(e + "i", header, _OFF_NAMES_SIZE, len(names_blob))

        pre_data = (align_up(HEADER_SIZE) + align_up(len(index_blob))
                    + align_up(len(names_blob)))
        # Physical layout: compressed blobs packed sequentially, each padded to
        # align(compressed_size, 2048). The file ends on a 2048 boundary.
        body = io.BytesIO()
        for stored, _usize in prepared:
            body.write(stored)
            pad = align_up(len(stored)) - len(stored)
            if pad:
                body.write(b"\x00" * pad)
        data_bytes = body.getvalue()
        struct.pack_into(e + "i", header, _OFF_PACKAGE_SIZE,
                         pre_data + len(data_bytes))

        _write_block(out_stream, bytes(header))
        _write_block(out_stream, index_blob)
        _write_block(out_stream, names_blob)
        out_stream.write(data_bytes)


def _seed_header(raw_header, big_endian: bool) -> bytearray:
    """Return a 384-byte header buffer to start from.

    If we have the original header and aren't changing endianness, copy it so
    cosmetic fields (the descriptive comment Volition writes between the
    version field and the structural fields) are preserved; the caller then
    overwrites every structural field. Otherwise return zeros.
    """
    if raw_header is not None and len(raw_header) == HEADER_SIZE:
        stored_magic = struct.unpack_from("<I", raw_header, _OFF_MAGIC)[0]
        stored_be = (stored_magic == MAGIC_BE)
        if stored_be == big_endian:
            return bytearray(raw_header)
    return bytearray(HEADER_SIZE)


def _build_string_blob(strings: list[str]) -> tuple[dict[str, int], bytes]:
    offsets: dict[str, int] = {}
    buf = io.BytesIO()
    for s in strings:
        offsets[s] = buf.tell()
        buf.write(s.encode("ascii") + b"\x00")
    return offsets, buf.getvalue()


def _ordered_string_blob(values: list[str]) -> tuple[bytes, dict[str, int]]:
    """Build a NUL-terminated string blob in first-seen order, deduped.

    Returns (blob, {string: offset}). Matches the ordering Volition's own
    tools use (unique strings appended as first encountered), so the stored
    name/extension offsets line up with the originals.
    """
    offsets: dict[str, int] = {}
    buf = io.BytesIO()
    for s in values:
        if s in offsets:
            continue
        offsets[s] = buf.tell()
        buf.write(s.encode("ascii") + b"\x00")
    return buf.getvalue(), offsets


def _read_block(stream: io.BufferedReader, size: int) -> bytes:
    """Read size bytes, then advance the stream to the next 2048 boundary."""
    data = stream.read(size)
    if len(data) != size:
        raise PackageError(f"Truncated block: wanted {size}, got {len(data)}")
    stream.seek(align_up(stream.tell()))
    return data


def _write_block(stream: io.BufferedWriter, data: bytes) -> None:
    stream.write(data)
    _pad_to(stream, BLOCK_ALIGN)


def _pad_to(stream: io.BufferedWriter, alignment: int) -> None:
    pos = stream.tell()
    pad = align_up(pos, alignment) - pos
    if pad:
        stream.write(b"\x00" * pad)


# ============================================================================
#  GUI
# ============================================================================
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


_PKG_PATTERNS = "*.vpp *.vpp_xbox2 *.vpp_pc *.vpp_ps3 *.str2 *.str2_xbox2 *.str2_pc"
_OPEN_FILETYPES = [("SR2 packages", _PKG_PATTERNS), ("All files", "*.*")]


class PackageToolApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Saints Row 2 Package Tool")
        self.geometry("960x640")
        self.minsize(720, 460)

        self._pkg: PackageFile | None = None
        self._pkg_path: str | None = None
        # All lists/dicts are keyed by entry index (not filename) so duplicate
        # filenames, which occur in some SR1 packages, are never collapsed.
        # _raw_payloads holds each entry's bytes exactly as stored on disk (the
        # zlib stream for compressed packages). Untouched entries are repacked
        # from these verbatim, so a no-edit save reproduces the source.
        # _edited maps index -> new decompressed bytes for entries the user
        # changed; only those get recompressed on save.
        self._raw_payloads: list[bytes] = []
        self._edited: dict[int, bytes] = {}
        self._editors: dict[int, "EditorTab"] = {}
        self._dirty = False
        self._log_queue: "queue.Queue[str]" = queue.Queue()

        self._build_widgets()
        self.after(100, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_widgets(self) -> None:
        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar, text="Open\u2026", command=self.on_open).pack(side=tk.LEFT)
        self.btn_save = ttk.Button(toolbar, text="Save package as\u2026",
                                   command=self.on_save_package, state=tk.DISABLED)
        self.btn_save.pack(side=tk.LEFT, padx=(6, 0))

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)

        self.btn_edit = ttk.Button(toolbar, text="Edit", command=self.on_edit,
                                   state=tk.DISABLED)
        self.btn_edit.pack(side=tk.LEFT)
        self.btn_extract = ttk.Button(toolbar, text="Extract all\u2026",
                                      command=self.on_extract_all, state=tk.DISABLED)
        self.btn_extract.pack(side=tk.LEFT, padx=(6, 0))
        self.btn_extract_sel = ttk.Button(toolbar, text="Extract selected\u2026",
                                          command=self.on_extract_selected,
                                          state=tk.DISABLED)
        self.btn_extract_sel.pack(side=tk.LEFT, padx=(6, 0))

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        ttk.Button(toolbar, text="Build from folder\u2026",
                   command=self.on_build).pack(side=tk.LEFT)

        opts = ttk.Frame(toolbar)
        opts.pack(side=tk.RIGHT)
        self.var_big_endian = tk.BooleanVar(value=True)
        self.var_compress = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Xbox 360 (big-endian)",
                        variable=self.var_big_endian).pack(side=tk.LEFT)
        ttk.Checkbutton(opts, text="Compress (zlib)",
                        variable=self.var_compress).pack(side=tk.LEFT, padx=(8, 0))

        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        list_frame = ttk.Frame(paned)
        cols = ("name", "size", "comp")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings",
                                 selectmode="extended")
        self.tree.heading("name", text="File")
        self.tree.heading("size", text="Size")
        self.tree.heading("comp", text="Z")
        self.tree.column("name", width=240, anchor=tk.W)
        self.tree.column("size", width=90, anchor=tk.E)
        self.tree.column("comp", width=28, anchor=tk.CENTER)
        vsb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Double-1>", lambda e: self.on_edit())
        paned.add(list_frame, weight=1)

        self.nb = ttk.Notebook(paned)
        paned.add(self.nb, weight=3)

        bottom = ttk.Frame(self, padding=(8, 0))
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(side=tk.TOP, fill=tk.X)
        self.log = tk.Text(bottom, height=5, wrap=tk.NONE, state=tk.DISABLED,
                           font=("TkFixedFont", 9))
        self.log.pack(side=tk.TOP, fill=tk.X, pady=(4, 4))

        self.status = ttk.Label(self, text="Ready.", anchor=tk.W,
                                relief=tk.SUNKEN, padding=(6, 2))
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                self.log.configure(state=tk.NORMAL)
                self.log.insert(tk.END, msg + "\n")
                self.log.see(tk.END)
                self.log.configure(state=tk.DISABLED)
        except queue.Empty:
            pass
        self.after(100, self._drain_log)

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _mark_dirty(self, dirty: bool = True) -> None:
        self._dirty = dirty
        if self._pkg_path:
            star = "*" if dirty else ""
            self.title(f"Saints Row 2 Package Tool - "
                       f"{os.path.basename(self._pkg_path)}{star}")

    def on_open(self) -> None:
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(title="Open package",
                                          filetypes=_OPEN_FILETYPES)
        if not path:
            return
        try:
            with open(path, "rb") as f:
                pkg = PackageFile.read(f)
                raw_payloads = [pkg.extract_raw(f, e) for e in pkg.entries]
        except PackageError as exc:
            messagebox.showerror("Cannot open package", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Error", f"Unexpected error:\n{exc}")
            return

        for tab in list(self._editors.values()):
            self.nb.forget(tab)
        self._editors.clear()
        self._edited.clear()

        self._pkg = pkg
        self._pkg_path = path
        self._raw_payloads = raw_payloads
        self._populate_tree()
        self._mark_dirty(False)

        self.var_big_endian.set(pkg.big_endian)
        self.var_compress.set(bool(pkg.flags & FLAG_COMPRESSED))

        endian = "big-endian (360)" if pkg.big_endian else "little-endian"
        if pkg.version == VERSION_V6:
            comp = "stored raw"
        else:
            comp = "compressed" if (pkg.flags & FLAG_COMPRESSED) else "uncompressed"
        self._set_status(f"{os.path.basename(path)} - {len(pkg.entries)} files, "
                         f"v{pkg.version}, {endian}, {comp}, flags=0x{pkg.flags:X}")
        self._log(f"Opened {path}")
        for b in (self.btn_save, self.btn_edit, self.btn_extract,
                  self.btn_extract_sel):
            b.configure(state=tk.NORMAL)

    def _populate_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        assert self._pkg is not None
        for i, entry in enumerate(self._pkg.entries):
            self.tree.insert("", tk.END, iid=str(i),
                             values=(entry.filename,
                                     f"{entry.uncompressed_size:,}",
                                     "\u2022" if entry.is_compressed else ""))

    def _selected_indices(self) -> "list[int]":
        return [int(iid) for iid in self.tree.selection()]

    def _decompressed(self, idx: int) -> bytes:
        """Decompressed bytes for an entry: the edited override if present,
        otherwise inflated from the stored raw payload."""
        if idx in self._edited:
            return self._edited[idx]
        assert self._pkg is not None
        entry = self._pkg.entries[idx]
        raw = self._raw_payloads[idx]
        if not entry.is_compressed:
            return raw
        d = zlib.decompressobj()
        return d.decompress(raw) + d.flush()

    def on_edit(self) -> None:
        for idx in self._selected_indices():
            self._open_editor(idx)

    def _open_editor(self, idx: int) -> None:
        assert self._pkg is not None
        entry = self._pkg.entries[idx]
        fn = entry.filename
        if idx in self._editors:
            self.nb.select(self._editors[idx])
            return
        if not entry.looks_textual:
            if not messagebox.askyesno(
                    "Open as text?",
                    f"{fn} doesn't look like a text file. Open it as text anyway?"):
                return
        data = self._decompressed(idx)
        try:
            text = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = data.decode("latin-1")
            encoding = "latin-1"

        # Tab label disambiguates duplicate filenames by index.
        label = fn if [e.filename for e in self._pkg.entries].count(fn) == 1 \
            else f"{fn} #{idx}"
        tab = EditorTab(self.nb, fn, text, encoding,
                        on_modified=lambda: self._mark_dirty(True))
        self.nb.add(tab, text=label)
        self.nb.select(tab)
        self._editors[idx] = tab

    def _commit_open_editors(self) -> None:
        """Pull text from open tabs; record an edited override only when the
        content actually changed (so untouched files stay verbatim)."""
        for idx, tab in self._editors.items():
            new_bytes = tab.get_bytes()
            if new_bytes != self._decompressed(idx):
                self._edited[idx] = new_bytes

    def on_extract_all(self) -> None:
        if self._pkg:
            self._extract(list(range(len(self._pkg.entries))))

    def on_extract_selected(self) -> None:
        idxs = self._selected_indices()
        if not idxs:
            messagebox.showinfo("Nothing selected", "Select one or more files first.")
            return
        self._extract(idxs)

    def _extract(self, indices: "list[int]") -> None:
        out_dir = filedialog.askdirectory(title="Choose extraction folder")
        if not out_dir:
            return
        self._commit_open_editors()
        self._run_worker(self._extract_worker, indices, out_dir)

    def _extract_worker(self, indices: "list[int]", out_dir: str) -> None:
        assert self._pkg is not None
        os.makedirs(out_dir, exist_ok=True)
        total, ok, fail = len(indices), 0, 0
        for n, idx in enumerate(indices, 1):
            entry = self._pkg.entries[idx]
            try:
                # If a filename is duplicated, suffix later copies so files on
                # disk don't overwrite each other.
                name = entry.filename
                dupes = [j for j, e in enumerate(self._pkg.entries)
                         if e.filename == name]
                if len(dupes) > 1:
                    stem, dot, ext = name.rpartition(".")
                    pos = dupes.index(idx)
                    if pos > 0:
                        name = (f"{stem}_{pos}{dot}{ext}" if dot
                                else f"{name}_{pos}")
                with open(os.path.join(out_dir, name), "wb") as out:
                    out.write(self._decompressed(idx))
                ok += 1
                self._log(f"  {name}")
            except Exception as exc:
                fail += 1
                self._log(f"  FAILED {entry.filename}: {exc}")
            self._set_progress(n, total)
        self._log(f"Done. {ok} extracted, {fail} failed.")
        self._set_status(f"Extracted {ok}/{total} to {out_dir}")

    def on_save_package(self) -> None:
        if not self._pkg:
            return
        if self._pkg.version == VERSION_V3:
            messagebox.showwarning(
                "Saving not yet supported for SR1",
                "This is a Saints Row 1 (version 3) package. Reading and "
                "extracting work, but saving is disabled: the SR1 360 data "
                "layout isn't fully reverse-engineered yet, and writing it "
                "could produce a file that crashes the game. SR2 and SR3 "
                "packages can be saved normally.")
            self._log("Save skipped: SR1 (v3) writing is disabled for safety.")
            return
        self._commit_open_editors()
        suffix = ""
        if self._pkg_path:
            base = os.path.basename(self._pkg_path)
            for s in PACKAGE_SUFFIXES:
                if base.lower().endswith(s):
                    suffix = s
                    break
        out_path = filedialog.asksaveasfilename(
            title="Save package as",
            initialfile=f"edited{suffix or '.vpp'}",
            filetypes=_OPEN_FILETYPES)
        if not out_path:
            return
        self._run_worker(self._save_worker, out_path)

    def _save_worker(self, out_path: str) -> None:
        assert self._pkg is not None
        # Detect whether the user is re-targeting platform or compression. If
        # so, every entry must be re-encoded (we can't reuse stored bytes); if
        # not, untouched entries are written verbatim for a faithful repack.
        orig_be = self._pkg.big_endian
        orig_compressed = bool(self._pkg.flags & FLAG_COMPRESSED)
        new_be = self.var_big_endian.get()
        new_compressed = (self.var_compress.get()
                          if self._pkg.version in (VERSION_V3, VERSION_V4)
                          else orig_compressed)
        retargeting = (new_be != orig_be) or (new_compressed != orig_compressed)

        self._pkg.big_endian = new_be
        if self._pkg.version in (VERSION_V3, VERSION_V4):
            if new_compressed:
                self._pkg.flags |= FLAG_COMPRESSED
            else:
                self._pkg.flags &= ~FLAG_COMPRESSED

        # Build the prepared payload list. Untouched + not-retargeting -> use
        # the original stored bytes verbatim ("stored"). Edited or retargeting
        # -> hand over raw bytes ("raw") to be (re)compressed as needed.
        prepared = []
        for idx in range(len(self._pkg.entries)):
            if idx in self._edited:
                prepared.append(("raw", self._edited[idx]))
            elif retargeting:
                prepared.append(("raw", self._decompressed(idx)))
            else:
                prepared.append(("stored", self._raw_payloads[idx]))

        verb = "re-encoding all" if retargeting else \
            f"{len(self._edited)} edited, rest verbatim"
        self._log(f"Saving v{self._pkg.version} package, "
                  f"{len(self._pkg.entries)} entries ({verb}) -> {out_path}")
        try:
            with open(out_path, "wb") as out:
                self._pkg.repack(
                    out, prepared,
                    progress=lambda i, n, m: (self._log("  " + m),
                                              self._set_progress(i, n)))
        except Exception as exc:
            self._log(f"SAVE FAILED: {exc}")
            self._set_status("Save failed.")
            messagebox.showerror("Save failed", str(exc))
            return
        self._log("Done.")
        self.after(0, lambda: self._mark_dirty(False))
        self._set_status(f"Saved {os.path.basename(out_path)}")

    def on_build(self) -> None:
        in_dir = filedialog.askdirectory(title="Choose folder of files to pack")
        if not in_dir:
            return
        out_path = filedialog.asksaveasfilename(
            title="Save package as", defaultextension=".vpp",
            filetypes=_OPEN_FILETYPES)
        if not out_path:
            return
        self._run_worker(self._build_worker, in_dir, out_path)

    def _build_worker(self, in_dir: str, out_path: str) -> None:
        pkg = PackageFile.from_directory(
            in_dir, big_endian=self.var_big_endian.get(),
            compress=self.var_compress.get())
        total = len(pkg.entries)
        if total == 0:
            self._log("No files found in folder; nothing to pack.")
            return
        self._log(f"Packing {total} files -> {out_path}")
        try:
            with open(out_path, "wb") as out:
                pkg.write(out, in_dir,
                          progress=lambda i, n, m: (self._log("  " + m),
                                                    self._set_progress(i, n)))
        except Exception as exc:
            self._log(f"BUILD FAILED: {exc}")
            return
        self._log("Done.")
        self._set_status(f"Built {os.path.basename(out_path)} ({total} files)")

    def _run_worker(self, fn, *args) -> None:
        self._set_buttons(tk.DISABLED)

        def wrapped():
            try:
                fn(*args)
            finally:
                self.after(0, lambda: self._set_buttons(tk.NORMAL))

        threading.Thread(target=wrapped, daemon=True).start()

    def _set_buttons(self, state: str) -> None:
        enabled = state if self._pkg else tk.DISABLED
        for b in (self.btn_save, self.btn_edit, self.btn_extract,
                  self.btn_extract_sel):
            b.configure(state=enabled)

    def _set_progress(self, current: int, total: int) -> None:
        self.after(0, lambda: self.progress.configure(
            maximum=max(total, 1), value=current))

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        return messagebox.askyesno("Unsaved changes",
                                   "You have unsaved edits. Discard them?")

    def on_close(self) -> None:
        if self._confirm_discard():
            self.destroy()


class EditorTab(ttk.Frame):
    """A single text-editor tab for one package entry."""

    def __init__(self, master, filename: str, text: str, encoding: str,
                 on_modified=None) -> None:
        super().__init__(master)
        self.filename = filename
        self.encoding = encoding
        self._on_modified = on_modified
        # Remember the dominant newline style so saving doesn't silently
        # rewrite every line (Tk's Text widget works internally in LF).
        self._newline = "\r\n" if "\r\n" in text else "\n"
        normalized = text.replace("\r\n", "\n")

        txt = tk.Text(self, wrap=tk.NONE, undo=True, font=("TkFixedFont", 10))
        vsb = ttk.Scrollbar(self, orient=tk.VERTICAL, command=txt.yview)
        hsb = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=txt.xview)
        txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        txt.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        txt.insert("1.0", normalized)
        txt.edit_modified(False)
        txt.bind("<<Modified>>", self._handle_modified)
        self.text = txt

    def _handle_modified(self, _event=None) -> None:
        if self.text.edit_modified():
            self.text.edit_modified(False)
            if self._on_modified:
                self._on_modified()

    def get_bytes(self) -> bytes:
        content = self.text.get("1.0", "end-1c")
        if self._newline != "\n":
            content = content.replace("\n", self._newline)
        return content.encode(self.encoding)


if __name__ == "__main__":
    PackageToolApp().mainloop()
