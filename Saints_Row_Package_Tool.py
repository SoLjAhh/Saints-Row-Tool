"""
Saints Row Package Tool - single-file GUI

A standalone Tkinter application to open, extract, edit, and rebuild Saints Row
package archives (.vpp / .vpp_xbox2 / .vpp_pc and .str2* containers), with
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
VERSION_V3 = 3 # This is for Saints Row 1
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

        pkg = cls(version=VERSION_V3, flags=flags, big_endian=big_endian)
        compressed_package = (flags & FLAG_COMPRESSED) == FLAG_COMPRESSED

        running = data_base
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
                # Like the v4 360 case, the stored data_offset is not usable;
                # compressed entries are laid out sequentially, each padded to
                # 2048 bytes.
                entry_offset = running
                running += align_up(compressed_size)
                stored_csize = compressed_size
            else:
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

        pkg = cls(version=VERSION_V4, flags=flags, big_endian=big_endian)
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

        pkg = cls(version=VERSION_V6, flags=flags, big_endian=big_endian)

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

        Used by the editor: extract once, edit some buffers in memory, then
        repack without staging to a temp folder. Any entry not present in
        `payloads` raises KeyError, so callers should supply every filename.
        """
        def provider(entry: PackageEntry) -> bytes:
            return payloads[entry.filename]

        self._write_core(out_stream, provider, progress)

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
        e = self._endian
        compress = (self.flags & FLAG_COMPRESSED) == FLAG_COMPRESSED

        # Build names / extensions string blobs (sorted, deduped).
        names = sorted({en.name for en in self.entries})
        exts = sorted({en.extension for en in self.entries})
        name_offsets, names_blob = _build_string_blob(names)
        ext_offsets, ext_blob = _build_string_blob(exts)

        # First pass: fetch + (optionally) compress each entry's payload so we
        # know sizes before writing the index.
        payloads: list[bytes] = []
        total = len(self.entries)
        uncompressed_total = 0
        for i, entry in enumerate(self.entries, 1):
            raw = payload_provider(entry)
            entry.uncompressed_size = len(raw)
            uncompressed_total += align_up(len(raw), 16)
            if compress:
                payload = zlib.compress(raw, 9)
                entry.compressed_size = len(payload)
            else:
                payload = raw
                entry.compressed_size = -1
            payloads.append(payload)
            if progress:
                progress(i, total, f"packing {entry.filename}")

        # Build index blob.
        index = io.BytesIO()
        running_offset = 0
        for entry, payload in zip(self.entries, payloads):
            if compress:
                # Data laid out sequentially, each entry block-aligned.
                stored_offset = running_offset
                running_offset += align_up(len(payload))
            else:
                stored_offset = running_offset
                running_offset += align_up(len(payload), 16)
            index.write(struct.pack(
                e + "iiIiiiI",
                name_offsets[entry.name],
                ext_offsets[entry.extension],
                entry.unknown08,
                stored_offset,
                entry.uncompressed_size,
                entry.compressed_size,
                entry.unknown1c,
            ))
        index_blob = index.getvalue()

        # Header.
        header = bytearray(HEADER_SIZE)
        struct.pack_into("<I", header, _OFF_MAGIC,
                         MAGIC_BE if self.big_endian else MAGIC_LE)
        struct.pack_into(e + "I", header, _OFF_VERSION, SUPPORTED_VERSION)
        struct.pack_into(e + "I", header, _OFF_FLAGS, self.flags)
        struct.pack_into(e + "i", header, _OFF_INDEX_COUNT, len(self.entries))
        struct.pack_into(e + "i", header, _OFF_INDEX_SIZE, len(index_blob))
        struct.pack_into(e + "i", header, _OFF_NAMES_SIZE, len(names_blob))
        struct.pack_into(e + "i", header, _OFF_EXTENSIONS_SIZE, len(ext_blob))
        struct.pack_into(e + "i", header, _OFF_UNCOMPRESSED_DATA_SIZE,
                         uncompressed_total)
        struct.pack_into(e + "I", header, _OFF_COMPRESSED_DATA_SIZE, 0xFFFFFFFF)

        package_size = (align_up(HEADER_SIZE)
                        + align_up(len(index_blob))
                        + align_up(len(names_blob))
                        + align_up(len(ext_blob))
                        + uncompressed_total)
        struct.pack_into(e + "i", header, _OFF_PACKAGE_SIZE, package_size)

        # Write everything, block-aligned.
        _write_block(out_stream, bytes(header))
        _write_block(out_stream, index_blob)
        _write_block(out_stream, names_blob)
        _write_block(out_stream, ext_blob)
        data_align = BLOCK_ALIGN if compress else 16
        for payload in payloads:
            out_stream.write(payload)
            _pad_to(out_stream, data_align)

    def _write_v6_core(self, out_stream: io.BufferedWriter, payload_provider,
                       progress=None) -> None:
        """Write a Saints Row: The Third (v6) package.

        Entries are stored raw (compressed_size = 0xFFFFFFFF), with the data
        region laid out sequentially, each entry padded to 2048 bytes. The
        names blob holds full filenames; there is no extensions blob.
        v6 compression of inner entries is not emitted here.
        """
        e = self._endian

        # v6 names are full filenames, in entry order (offsets reference them).
        names_blob = io.BytesIO()
        name_offsets: list[int] = []
        for entry in self.entries:
            name_offsets.append(names_blob.tell())
            names_blob.write(entry.filename.encode("ascii") + b"\x00")
        names_blob = names_blob.getvalue()

        # First pass: fetch payloads and record sizes.
        payloads: list[bytes] = []
        total = len(self.entries)
        data_size = 0
        for i, entry in enumerate(self.entries, 1):
            raw = payload_provider(entry)
            entry.uncompressed_size = len(raw)
            entry.compressed_size = -1   # stored raw
            payloads.append(raw)
            data_size += align_up(len(raw))   # 2048-aligned per entry
            if progress:
                progress(i, total, f"packing {entry.filename}")

        # Build the 24-byte index entries.
        index = io.BytesIO()
        running = 0
        for entry, payload, noff in zip(self.entries, payloads, name_offsets):
            index.write(struct.pack(
                e + "IIIIiI",
                noff,
                entry.unknown08,            # unknown / zero
                running,                    # data_offset (relative to data block)
                entry.uncompressed_size,
                -1,                         # compressed_size: raw
                entry.unknown1c,            # flags / unknown
            ))
            running += align_up(len(payload))
        index_blob = index.getvalue()

        # Header (v4 offsets, v6 semantics: 0x164 carries data_size).
        header = bytearray(HEADER_SIZE)
        struct.pack_into("<I", header, _OFF_MAGIC,
                         MAGIC_BE if self.big_endian else MAGIC_LE)
        struct.pack_into(e + "I", header, _OFF_VERSION, VERSION_V6)
        struct.pack_into(e + "I", header, _OFF_FLAGS, self.flags)
        struct.pack_into(e + "i", header, _OFF_INDEX_COUNT, len(self.entries))
        struct.pack_into(e + "i", header, _OFF_INDEX_SIZE, len(index_blob))
        struct.pack_into(e + "i", header, _OFF_NAMES_SIZE, len(names_blob))
        struct.pack_into(e + "i", header, _OFF_EXTENSIONS_SIZE, data_size)
        struct.pack_into(e + "I", header, _OFF_UNCOMPRESSED_DATA_SIZE, 0xFFFFFFFF)

        package_size = (align_up(HEADER_SIZE)
                        + align_up(len(index_blob))
                        + align_up(len(names_blob))
                        + data_size)
        struct.pack_into(e + "i", header, _OFF_PACKAGE_SIZE, package_size)

        _write_block(out_stream, bytes(header))
        _write_block(out_stream, index_blob)
        _write_block(out_stream, names_blob)
        for payload in payloads:
            out_stream.write(payload)
            _pad_to(out_stream, BLOCK_ALIGN)

    def _write_v3_core(self, out_stream: io.BufferedWriter, payload_provider,
                       progress=None) -> None:
        """Write a Saints Row 1 / Red Faction era (v3) package.

        28-byte entries with full filenames in the names blob (no extensions
        block). When FLAG_COMPRESSED is set each entry is zlib-deflated; data
        is laid out sequentially, each entry padded to 2048 bytes, and the
        stored data_offset is the cumulative aligned position. The per-file crc
        field is preserved from the source entry (0 for newly-built packages).
        """
        e = self._endian
        compress = (self.flags & FLAG_COMPRESSED) == FLAG_COMPRESSED

        # Names blob: full filenames, in entry order.
        names_io = io.BytesIO()
        name_offsets: list[int] = []
        for entry in self.entries:
            name_offsets.append(names_io.tell())
            names_io.write(entry.filename.encode("ascii") + b"\x00")
        names_blob = names_io.getvalue()

        # First pass: fetch + (optionally) compress payloads.
        payloads: list[bytes] = []
        total = len(self.entries)
        for i, entry in enumerate(self.entries, 1):
            raw = payload_provider(entry)
            entry.uncompressed_size = len(raw)
            if compress:
                payload = zlib.compress(raw, 9)
                entry.compressed_size = len(payload)
            else:
                payload = raw
                entry.compressed_size = -1
            payloads.append(payload)
            if progress:
                progress(i, total, f"packing {entry.filename}")

        # Index: data laid out sequentially, 2048-aligned.
        index = io.BytesIO()
        running = 0
        data_size = 0
        for entry, payload, noff in zip(self.entries, payloads, name_offsets):
            stored_csize = entry.compressed_size if compress else len(payload)
            index.write(struct.pack(
                e + "IIIIIII",
                noff,
                0,
                running,
                entry.crc & 0xFFFFFFFF,
                entry.uncompressed_size,
                (stored_csize & 0xFFFFFFFF) if compress else 0xFFFFFFFF,
                0,
            ))
            running += align_up(len(payload))
            data_size += align_up(len(payload))
        index_blob = index.getvalue()

        header = bytearray(HEADER_SIZE)
        struct.pack_into("<I", header, _OFF_MAGIC,
                         MAGIC_BE if self.big_endian else MAGIC_LE)
        struct.pack_into(e + "I", header, _OFF_VERSION, VERSION_V3)
        struct.pack_into(e + "I", header, _OFF_FLAGS, self.flags)
        struct.pack_into(e + "i", header, _OFF_INDEX_COUNT, len(self.entries))
        struct.pack_into(e + "i", header, _OFF_INDEX_SIZE, len(index_blob))
        struct.pack_into(e + "i", header, _OFF_NAMES_SIZE, len(names_blob))

        package_size = (align_up(HEADER_SIZE)
                        + align_up(len(index_blob))
                        + align_up(len(names_blob))
                        + data_size)
        struct.pack_into(e + "i", header, _OFF_PACKAGE_SIZE, package_size)

        _write_block(out_stream, bytes(header))
        _write_block(out_stream, index_blob)
        _write_block(out_stream, names_blob)
        for payload in payloads:
            out_stream.write(payload)
            _pad_to(out_stream, BLOCK_ALIGN)


def _build_string_blob(strings: list[str]) -> tuple[dict[str, int], bytes]:
    offsets: dict[str, int] = {}
    buf = io.BytesIO()
    for s in strings:
        offsets[s] = buf.tell()
        buf.write(s.encode("ascii") + b"\x00")
    return offsets, buf.getvalue()


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
        self.title("Saints Row Package Tool")
        self.geometry("960x640")
        self.minsize(720, 460)

        self._pkg: PackageFile | None = None
        self._pkg_path: str | None = None
        self._buffers: dict[str, bytes] = {}
        self._editors: dict[str, "EditorTab"] = {}
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
            self.title(f"Saints Row Package Tool - "
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
                buffers = {e.filename: pkg.extract_entry(f, e) for e in pkg.entries}
        except PackageError as exc:
            messagebox.showerror("Cannot open package", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Error", f"Unexpected error:\n{exc}")
            return

        for tab in list(self._editors.values()):
            self.nb.forget(tab)
        self._editors.clear()

        self._pkg = pkg
        self._pkg_path = path
        self._buffers = buffers
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

    def _selected_entries(self) -> "list[PackageEntry]":
        assert self._pkg is not None
        return [self._pkg.entries[int(iid)] for iid in self.tree.selection()]

    def on_edit(self) -> None:
        for entry in self._selected_entries():
            self._open_editor(entry)

    def _open_editor(self, entry: "PackageEntry") -> None:
        fn = entry.filename
        if fn in self._editors:
            self.nb.select(self._editors[fn])
            return
        if not entry.looks_textual:
            if not messagebox.askyesno(
                    "Open as text?",
                    f"{fn} doesn't look like a text file. Open it as text anyway?"):
                return
        try:
            text = self._buffers[fn].decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = self._buffers[fn].decode("latin-1")
            encoding = "latin-1"

        tab = EditorTab(self.nb, fn, text, encoding,
                        on_modified=lambda: self._mark_dirty(True))
        self.nb.add(tab, text=fn)
        self.nb.select(tab)
        self._editors[fn] = tab

    def _commit_open_editors(self) -> None:
        for fn, tab in self._editors.items():
            self._buffers[fn] = tab.get_bytes()

    def on_extract_all(self) -> None:
        if self._pkg:
            self._extract(self._pkg.entries)

    def on_extract_selected(self) -> None:
        entries = self._selected_entries()
        if not entries:
            messagebox.showinfo("Nothing selected", "Select one or more files first.")
            return
        self._extract(entries)

    def _extract(self, entries: "list[PackageEntry]") -> None:
        out_dir = filedialog.askdirectory(title="Choose extraction folder")
        if not out_dir:
            return
        self._commit_open_editors()
        self._run_worker(self._extract_worker, entries, out_dir)

    def _extract_worker(self, entries: "list[PackageEntry]", out_dir: str) -> None:
        os.makedirs(out_dir, exist_ok=True)
        total, ok, fail = len(entries), 0, 0
        for i, entry in enumerate(entries, 1):
            try:
                with open(os.path.join(out_dir, entry.filename), "wb") as out:
                    out.write(self._buffers[entry.filename])
                ok += 1
                self._log(f"  {entry.filename}")
            except Exception as exc:
                fail += 1
                self._log(f"  FAILED {entry.filename}: {exc}")
            self._set_progress(i, total)
        self._log(f"Done. {ok} extracted, {fail} failed.")
        self._set_status(f"Extracted {ok}/{total} to {out_dir}")

    def on_save_package(self) -> None:
        if not self._pkg:
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
        self._pkg.big_endian = self.var_big_endian.get()
        # The compress flag applies to v3 and v4; v6 stores entries raw, so
        # leave its flags untouched and ignore the checkbox there.
        if self._pkg.version in (VERSION_V3, VERSION_V4):
            if self.var_compress.get():
                self._pkg.flags |= FLAG_COMPRESSED
            else:
                self._pkg.flags &= ~FLAG_COMPRESSED
        self._log(f"Saving v{self._pkg.version} package, "
                  f"{len(self._pkg.entries)} entries -> {out_path}")
        try:
            with open(out_path, "wb") as out:
                self._pkg.write_payloads(
                    out, self._buffers,
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
