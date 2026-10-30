"""Read-only, dependency-free MUGEN portraits.

API: PortraitCache(Path(game_root), Path(cache_dir), Path(overrides_dir)).
get(entry: str) -> Path | None returns a normalized RGBA PNG in cache_dir.
errors[entry] explains the most recent failure; success removes that diagnostic.
Calls must be serialized. No game code is imported or executed, no downloads occur.

Overrides: overrides_dir / (sha256(entry.encode("utf-8")).hexdigest() + ".png").
The exact roster entry (including case, spaces and separators) is hashed. A valid
override takes precedence, even if the character is missing. Invalid overrides
fail explicitly rather than silently selecting a different picture.

Prefer sprite 9000,1, falling back to 9000,0. Supported: SFF v1 indexed 8-bit
PCX (raw/RLE, shared palettes and linked sprites); SFF v2 indexed raw/RLE8/
RLE5/LZ5, and embedded PNG (formats 10/11/12), linked sprites and linked palettes.
Unknown formats are explicitly unsupported. PNG inputs support
noninterlaced 8-bit grayscale, RGB, indexed, gray-alpha and RGBA with all five
filters and tRNS. Index zero is transparent for PCX/raw/RLE8 sprites. PNG alpha
is preserved. External ACT palettes are not applied (SFF's own palette is used).

Cache keys include decoder version, resolved asset paths, sizes and nanosecond
mtimes (not a content hash); changing a DEF/SFF/override invalidates its cache.
Cache files are validated before reuse. Old versions are not automatically
pruned. cache_dir must be outside game_root, so installed assets stay read-only.
Limits: 1 GiB SFF, 100,000 records, 16 MiB sprite/PNG, 2048 pixels per axis,
4 million pixels and 20 MiB decoded data. Only selected payloads are read.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import struct
import zlib


MAX_FILE = 1 << 30
MAX_RECORDS = 100_000
MAX_INPUT = 16 << 20
MAX_DECODED = 20 << 20
MAX_PIXELS = 4_000_000
SIGNATURE = b"\x89PNG\r\n\x1a\n"
VERSION = b"portraits-3"


class PortraitError(ValueError):
    """Malformed, missing, unsafe or unsupported portrait asset."""


def _check(condition: bool, reason: str) -> None:
    if not condition:
        raise PortraitError(reason)


def _dimensions(w: int, h: int) -> None:
    _check(0 < w <= 2048 and 0 < h <= 2048 and w * h <= MAX_PIXELS,
           "Invalid or oversized portrait dimensions")


def _contained(base: Path, relative: str) -> Path:
    relative = relative.replace("\\", "/")
    _check(bool(relative) and not relative.startswith("/") and ":" not in relative
           and not any(ord(c) < 32 for c in relative), "Unsafe asset path")
    result = (base / relative).resolve()
    _check(result.is_relative_to(base.resolve()), "Asset path escapes its folder")
    return result


def _read(file, offset: int, length: int, size: int, limit: int = MAX_INPUT) -> bytes:
    _check(0 <= length <= limit and 0 <= offset <= size and length <= size - offset,
           "Invalid or oversized asset data bounds")
    file.seek(offset)
    data = file.read(length)
    _check(len(data) == length, "Truncated asset")
    return data


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def _png(w: int, h: int, rgba: bytes) -> bytes:
    rows = b"".join(b"\0" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b""))


def _decode_png(data: bytes, sff_palette: bytes | None = None) -> tuple[int, int, bytes]:
    _check(len(data) <= MAX_INPUT and data.startswith(SIGNATURE), "Invalid PNG signature/size")
    pos, header, palette, transparency = 8, None, b"", b""
    compressed = bytearray()
    seen = set()
    ended = False
    while pos < len(data):
        _check(pos + 12 <= len(data), "Truncated PNG chunk")
        n = struct.unpack_from(">I", data, pos)[0]
        kind = data[pos + 4:pos + 8]
        _check(n <= len(data) - pos - 12, "Invalid PNG chunk bounds")
        payload = data[pos + 8:pos + 8 + n]
        crc = struct.unpack_from(">I", data, pos + 8 + n)[0]
        _check(zlib.crc32(kind + payload) & 0xFFFFFFFF == crc, "PNG checksum mismatch")
        _check(header is not None or kind == b"IHDR", "PNG header must be first")
        if kind == b"IHDR":
            _check(kind not in seen and n == 13, "Invalid PNG header")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"PLTE":
            _check(kind not in seen and b"IDAT" not in seen and 0 < n <= 768 and n % 3 == 0,
                   "Invalid PNG palette")
            palette = payload
        elif kind == b"tRNS":
            _check(kind not in seen and b"IDAT" not in seen, "Invalid PNG transparency")
            transparency = payload
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            _check(n == 0 and pos + 12 == len(data), "Invalid PNG end")
            ended = True
            break
        else:
            _check(bool(kind[0] & 32), "Unsupported critical PNG chunk")
        seen.add(kind)
        pos += n + 12
    _check(ended and header is not None and b"IDAT" in seen, "Incomplete PNG")
    w, h, depth, color, comp, filt, interlace = header
    _dimensions(w, h)
    _check(depth == 8 and color in (0, 2, 3, 4, 6) and (comp, filt, interlace) == (0, 0, 0),
           "Unsupported PNG depth, color type or interlacing")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    if color == 3:
        _check(bool(palette) and len(transparency) <= len(palette) // 3,
               "Missing PNG palette or invalid transparency")
        if sff_palette is not None:
            _check(0 < len(sff_palette) <= 1024 and len(sff_palette) % 4 == 0,
                   "Invalid SFF PNG palette")
            # SFF format 10 stores indices in PNG, not authoritative PNG colors.
            palette = b"".join(sff_palette[i:i + 3] for i in range(0, len(sff_palette), 4))
            transparency = b"\0" + b"\xff" * (len(sff_palette) // 4 - 1)
    elif transparency or b"tRNS" in seen:
        _check((color == 0 and len(transparency) == 2)
               or (color == 2 and len(transparency) == 6), "Invalid PNG transparency")
    stride = w * channels
    expected = h * (stride + 1)
    _check(expected <= MAX_DECODED, "Oversized PNG decoded data")
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(compressed, expected + 1)
    except zlib.error as exc:
        raise PortraitError("Invalid PNG compressed data") from exc
    _check(len(raw) == expected and decoder.eof and not decoder.unused_data
           and not decoder.unconsumed_tail, "Invalid PNG decompressed size")
    previous = bytearray(stride)
    rgba = bytearray()
    for y in range(h):
        start = y * (stride + 1)
        method = raw[start]
        _check(method <= 4, "Invalid PNG filter")
        row = bytearray(raw[start + 1:start + 1 + stride])
        for x in range(stride):
            a = row[x - channels] if x >= channels else 0
            b = previous[x]
            c = previous[x - channels] if x >= channels else 0
            if method == 4:
                p = a + b - c
                distances = (abs(p - a), abs(p - b), abs(p - c))
                predictor = (a, b, c)[distances.index(min(distances))]
            else:
                predictor = (0, a, b, (a + b) // 2)[method]
            row[x] = (row[x] + predictor) & 255
        for x in range(0, stride, channels):
            pixel = row[x:x + channels]
            if color == 3:
                i = pixel[0]
                _check(i * 3 + 3 <= len(palette), "PNG palette index out of bounds")
                rgba.extend(palette[i * 3:i * 3 + 3])
                rgba.append(transparency[i] if i < len(transparency) else 255)
            elif color in (0, 4):
                rgba.extend(pixel[:1] * 3)
                rgba.append(pixel[1] if color == 4 else
                            (0 if transparency == struct.pack(">H", pixel[0]) else 255))
            else:
                rgba.extend(pixel[:3])
                rgba.append(pixel[3] if color == 6 else
                            (0 if transparency == struct.pack(">HHH", *pixel) else 255))
        previous = row
    return w, h, bytes(rgba)


def _indexed(w: int, h: int, indices: bytes, palette: bytes) -> tuple[int, int, bytes]:
    _dimensions(w, h)
    _check(len(indices) == w * h and len(palette) % 4 == 0, "Invalid indexed sprite")
    rgba = bytearray()
    for i in indices:
        _check(i * 4 + 4 <= len(palette), "Sprite palette index out of bounds")
        rgba.extend(palette[i * 4:i * 4 + 3])
        rgba.append(0 if i == 0 else 255)
    return w, h, bytes(rgba)


def _pcx(data: bytes, palette: bytes | None) -> tuple[int, int, bytes]:
    _check(len(data) >= 128 and data[0] == 10 and data[2] in (0, 1)
           and data[3] == 8 and data[65] == 1, "Unsupported or truncated PCX")
    xmin, ymin, xmax, ymax = struct.unpack_from("<4H", data, 4)
    w, h = xmax - xmin + 1, ymax - ymin + 1
    _dimensions(w, h)
    stride = struct.unpack_from("<H", data, 66)[0]
    _check(w <= stride <= 4096 and stride * h <= MAX_DECODED, "Invalid PCX scanline size")
    end = len(data)
    if len(data) >= 897 and data[-769] == 12:
        end -= 769
        if palette is None:
            palette = data[-768:]
    _check(palette is not None and len(palette) == 768, "Missing PCX shared/embedded palette")
    decoded = bytearray()
    pos = 128
    while len(decoded) < stride * h:
        _check(pos < end, "Truncated PCX pixels")
        value = data[pos]
        pos += 1
        count = 1
        if data[2] == 1 and value & 0xC0 == 0xC0:
            count = value & 63
            _check(count > 0 and pos < end, "Invalid PCX run")
            value = data[pos]
            pos += 1
        _check(len(decoded) + count <= stride * h, "PCX run exceeds decoded size")
        decoded.extend(bytes([value]) * count)
    # Shared-palette PCX exports may retain only the palette marker.
    _check(pos == end or (palette is not None and data[pos:end] == b"\x0c"),
           "Unexpected trailing PCX pixel data")
    indices = b"".join(decoded[y * stride:y * stride + w] for y in range(h))
    pal4 = b"".join(palette[i:i + 3] + b"\xff" for i in range(0, 768, 3))
    return _indexed(w, h, indices, pal4)


def _follow(records: list, index: int, linked, empty) -> int:
    visited = set()
    while True:
        _check(0 <= index < len(records) and index not in visited, "Invalid/cyclic SFF link")
        visited.add(index)
        if not empty(records[index]):
            return index
        index = linked(records[index])


def _five_bit(data: bytes, expected: int, lz: bool) -> bytes:
    """Decode bounded SFF five-bit RLE packets and optional LZ backreferences."""
    codec = "LZ5" if lz else "RLE5"
    _check(0 < expected <= MAX_PIXELS and len(data) >= 4
           and struct.unpack_from("<I", data)[0] == expected,
           f"Invalid {codec} decoded size")
    output = bytearray()
    pos = 4

    def byte():
        nonlocal pos
        _check(pos < len(data), f"Truncated {codec} packet")
        value = data[pos]
        pos += 1
        return value

    def run(color, count):
        _check(len(output) + count <= expected, f"{codec} run exceeds decoded size")
        output.extend(bytes([color]) * count)

    if not lz:
        while len(output) < expected:
            count, packet = byte() + 1, byte()
            color = byte() if packet & 128 else 0
            _check(color < 32, "Invalid RLE5 color")
            run(color, count)
            for _ in range(packet & 127):
                packed = byte()
                run(packed & 31, (packed >> 5) + 1)
    else:
        recycled, fragments = 0, 0
        while len(output) < expected:
            control = byte()
            for bit in range(8):
                if len(output) == expected:
                    break
                packet = byte()
                if not control & (1 << bit):
                    color = packet & 31
                    count = packet >> 5
                    run(color, count if count else byte() + 8)
                else:
                    count = packet & 63
                    if count:
                        recycled |= (packet & 192) >> (2 * fragments)
                        fragments += 1
                        if fragments == 4:
                            distance = recycled + 1
                            recycled, fragments = 0, 0
                        else:
                            distance = byte() + 1
                        count += 1
                    else:
                        distance = ((packet & 192) << 2) + byte() + 1
                        count = byte() + 3
                    _check(0 < distance <= len(output), "Invalid LZ5 backreference")
                    _check(len(output) + count <= expected, "LZ5 copy exceeds decoded size")
                    # Overlap is valid: newly written pixels may be copied again.
                    for _ in range(count):
                        output.append(output[-distance])
    _check(pos == len(data), f"Unexpected trailing {codec} data")
    return bytes(output)


def _sff1(file, size: int, header: bytes):
    count, offset, subsize = struct.unpack_from("<III", header, 20)
    _check(0 < count <= MAX_RECORDS and subsize == 32, "Invalid SFF v1 header")
    records, visited = [], set()
    for _ in range(count):
        _check(offset >= 512 and offset not in visited, "Invalid/cyclic SFF v1 subheader")
        visited.add(offset)
        row = struct.unpack_from("<IIhhHHHB", _read(file, offset, 32, size))
        _check(row[1] <= MAX_FILE and offset + 32 + row[1] <= size, "Invalid SFF sprite bounds")
        records.append((offset, row))
        offset = row[0]
        if not offset:
            break
    # Fighter Factory exports may use EOF instead of zero as the final link.
    _check(len(records) == count and offset in (0, size), "SFF v1 sprite count mismatch")
    failures = []
    for number in (1, 0):
        for i, (_, row) in enumerate(records):
            if row[4:6] != (9000, number):
                continue
            try:
                actual = _follow(records, i, lambda r: r[1][6], lambda r: r[1][1] == 0)
                start, sprite = records[actual]
                palette = None
                if sprite[7]:
                    # A shared palette is inherited from the preceding real,
                    # non-shared PCX, not from an arbitrary linked sprite.
                    for prev in range(actual - 1, -1, -1):
                        poff, prior = records[prev]
                        if prior[1] and not prior[7]:
                            _check(prior[1] >= 897, "Missing preceding PCX palette")
                            tail = _read(file, poff + 32 + prior[1] - 769, 769, size)
                            _check(tail[0] == 12, "Missing preceding PCX palette")
                            palette = tail[1:]
                            break
                    _check(palette is not None, "Missing shared PCX palette")
                return _pcx(_read(file, start + 32, sprite[1], size), palette)
            except PortraitError as exc:
                failures.append(f"9000,{number}: {exc}")
            break
    raise PortraitError("; ".join(failures) or "No portrait sprite 9000,1 or 9000,0")


def _sff2(file, size: int, header: bytes):
    so, count, po, pcount, lo, llen, to, tlen = struct.unpack_from("<8I", header, 36)
    _check(0 < count <= MAX_RECORDS and pcount <= MAX_RECORDS, "Invalid SFF v2 record count")
    sprites = _read(file, so, count * 28, size)
    palettes = _read(file, po, pcount * 16, size)
    records = list(struct.iter_unpack("<HHHHhhHBBIIHH", sprites))
    pals = list(struct.iter_unpack("<HHHHII", palettes))
    for base, length in ((lo, llen), (to, tlen)):
        _check(base <= size and length <= size - base, "Invalid SFF data block")

    def payload(base, blocksize, offset, length):
        _check(offset <= blocksize and length <= blocksize - offset, "SFF payload escapes data block")
        return _read(file, base + offset, length, size)

    def sprite_palette(index):
        pi = _follow(pals, index, lambda r: r[3], lambda r: r[5] == 0)
        _, _, colors, _, paloff, pallen = pals[pi]
        _check(0 < colors <= 256 and pallen == colors * 4, "Invalid SFF palette size")
        return payload(lo, llen, paloff, pallen)

    failures = []
    for number in (1, 0):
        for i, row in enumerate(records):
            if row[:2] != (9000, number):
                continue
            try:
                actual = _follow(records, i, lambda r: r[6], lambda r: r[10] == 0)
                row = records[actual]
                _, _, w, h, _, _, _, fmt, depth, offset, length, pal, flags = row
                _dimensions(w, h)
                _check(flags in (0, 1), "Unsupported SFF sprite flags")
                data = payload(to if flags else lo, tlen if flags else llen, offset, length)
                if fmt in (10, 11, 12):
                    _check(len(data) >= 4, "Truncated SFF PNG prefix")
                    _check(struct.unpack_from("<I", data)[0] <= MAX_DECODED,
                           "Oversized SFF PNG decoded size")
                    result = _decode_png(data[4:], sprite_palette(pal) if fmt == 10 else None)
                    _check(result[:2] == (w, h), "SFF/PNG dimension mismatch")
                    return result
                _check((depth == 8 and fmt in (0, 2)) or (depth in (5, 8) and fmt in (3, 4)),
                       f"Unsupported SFF v2 codec {fmt}, depth {depth}")
                palette = sprite_palette(pal)
                if fmt in (3, 4):
                    data = _five_bit(data, w * h, lz=fmt == 4)
                elif fmt == 2:
                    _check(len(data) >= 4 and struct.unpack_from("<I", data)[0] == w * h,
                           "Invalid RLE8 decoded size")
                    decoded, pos = bytearray(), 4
                    while pos < len(data):
                        value = data[pos]
                        pos += 1
                        run = 1
                        if value & 0xC0 == 0x40:
                            run = value & 63
                            _check(run > 0 and pos < len(data), "Invalid RLE8 run")
                            value = data[pos]
                            pos += 1
                        _check(len(decoded) + run <= w * h, "RLE8 run exceeds decoded size")
                        decoded.extend(bytes([value]) * run)
                    data = bytes(decoded)
                return _indexed(w, h, data, palette)
            except PortraitError as exc:
                failures.append(f"9000,{number}: {exc}")
            break
    raise PortraitError("; ".join(failures) or "No portrait sprite 9000,1 or 9000,0")


class PortraitCache:
    """See module docstring for the public API and supported formats."""

    def __init__(self, game_root: Path, cache_dir: Path, overrides_dir: Path):
        self.game_root = Path(game_root).resolve()
        self.cache_dir = Path(cache_dir).resolve()
        self.overrides_dir = Path(overrides_dir).resolve()
        if self.cache_dir.is_relative_to(self.game_root):
            raise ValueError("Portrait cache must be outside the read-only game folder")
        self.errors: dict[str, str] = {}

    @staticmethod
    def _stamp(path: Path):
        stat = path.stat()
        return str(path), stat.st_size, stat.st_mtime_ns

    def _definition(self, entry: str) -> Path:
        _check(bool(entry) and not entry.startswith("-"), "Unsafe/empty character entry")
        relative = entry.replace("\\", "/")
        if not relative.lower().endswith(".def"):
            relative += "/" + relative.rsplit("/", 1)[-1] + ".def"
        result = _contained(self.game_root / "chars", relative)
        _check(result.is_file(), f"Missing character definition: {entry}")
        return result

    def _sprite(self, definition: Path) -> Path:
        with definition.open("rb") as file:
            raw = file.read((1 << 20) + 1)
        _check(len(raw) <= 1 << 20, "Oversized character definition")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
        section, sprite = "", ""
        for rawline in text.splitlines():
            quoted, line = False, ""
            for char in rawline:
                if char == '"':
                    quoted = not quoted
                if char == ";" and not quoted:
                    break
                line += char
            line = line.strip()
            if line.startswith("[") and "]" in line:
                section = line[1:line.index("]")].strip().lower()
            elif section == "files" and "=" in line:
                key, value = line.split("=", 1)
                if key.strip().lower() == "sprite":
                    sprite = value.strip().strip('"')
        _check(bool(sprite), "Missing [Files] sprite in character definition")
        path = _contained(definition.parent, sprite)
        _check(path.is_file(), f"Missing sprite asset: {sprite}")
        return path

    def get(self, entry: str) -> Path | None:
        """Return a cache PNG or None; errors[entry] contains a failure reason."""
        try:
            key = hashlib.sha256(entry.encode("utf-8")).hexdigest()
            override = _contained(self.overrides_dir, key + ".png")
            definition = None
            if override.is_file():
                source = override
                stamp = (VERSION, self._stamp(source), "override")
            else:
                definition = self._definition(entry)
                source = self._sprite(definition)
                stamp = (VERSION, self._stamp(definition), self._stamp(source))
            version = hashlib.sha256(repr(stamp).encode("utf-8")).hexdigest()
            destination = _contained(self.cache_dir, key + "-" + version + ".png")
            if destination.is_file():
                try:
                    with destination.open("rb") as file:
                        _decode_png(file.read(MAX_INPUT + 1))
                    self.errors.pop(entry, None)
                    return destination
                except PortraitError:
                    pass
            with source.open("rb") as file:
                size = os.fstat(file.fileno()).st_size
                if definition is None:
                    result = _decode_png(_read(file, 0, size, size))
                else:
                    _check(96 <= size <= MAX_FILE, "Invalid or oversized SFF file")
                    header = _read(file, 0, 96, size)
                    _check(header[:12] == b"ElecbyteSpr\0", "Invalid SFF signature")
                    if header[15] == 1:
                        result = _sff1(file, size, header)
                    elif header[15] == 2:
                        result = _sff2(file, size, header)
                    else:
                        raise PortraitError(f"Unsupported SFF major version {header[15]}")
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            # Exclusive creation avoids following pre-existing files/symlinks.
            staging = _contained(self.cache_dir, destination.name + ".writing")
            created = False
            try:
                with staging.open("xb") as file:
                    created = True
                    file.write(_png(*result))
                staging.replace(destination)
            finally:
                # Only remove staging files created by this call.
                if created:
                    staging.unlink(missing_ok=True)
            self.errors.pop(entry, None)
            return destination
        except (OSError, ValueError, struct.error, OverflowError) as exc:
            self.errors[entry] = str(exc) or type(exc).__name__
            return None
