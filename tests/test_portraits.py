"""Run: python -m unittest discover -s tests -p test_portraits.py -v

The optional installed-game test reads ten roster entries without launching
anything. Scratch/cache files are confined to a unique project-local directory.
"""

import hashlib
import os
from pathlib import Path
import shutil
import struct
import unittest
import uuid
import zlib

from portraits import PortraitCache, PortraitError, _chunk, _decode_png, _five_bit, _png, SIGNATURE


PALETTE = b"\xff\x00\xff\x00" + b"\x0a\x14\x1e\xff" + b"\x64\x6e\x78\xff"
PIXELS = b"\x00\x01\x02\x01"
RGBA = b"\xff\x00\xff\x00\x0a\x14\x1e\xff\x64\x6e\x78\xff\x0a\x14\x1e\xff"


def pcx(shared=False, encoding=1):
    header = bytearray(128)
    header[:4] = bytes((10, 5, encoding, 8))
    struct.pack_into("<4H", header, 4, 0, 0, 1, 1)
    header[65] = 1
    struct.pack_into("<H", header, 66, 2)
    palette = b"".join(PALETTE[i:i + 3] for i in range(0, 12, 4)) + bytes(759)
    return bytes(header) + PIXELS + (b"" if shared else b"\x0c" + palette)


def sff1(sprites):
    """sprites: (group, number, payload, link, shared)."""
    header = bytearray(512)
    header[:16] = b"ElecbyteSpr\0\0\1\0\1"
    struct.pack_into("<4I", header, 16, 1, len(sprites), 512, 32)
    result = bytearray(header)
    for i, (group, number, data, link, shared) in enumerate(sprites):
        following = len(result) + 32 + len(data) if i + 1 < len(sprites) else 0
        sub = bytearray(32)
        struct.pack_into("<IIhhHHHB", sub, 0, following, len(data), 0, 0,
                         group, number, link, shared)
        result.extend(sub + data)
    return bytes(result)


def sff2(fmt=2, *, linked=False, palette_link=False, flags=0):
    header = bytearray(512)
    header[:16] = b"ElecbyteSpr\0\0\0\0\2"
    if fmt == 2:
        payload = struct.pack("<I", 4) + PIXELS
    elif fmt == 3:
        payload = struct.pack("<I", 4) + b"\x00\x03\x01\x02\x01"
    elif fmt == 4:
        payload = struct.pack("<I", 4) + b"\x00\x20\x21\x22\x21"
    elif fmt in (10, 11, 12):
        payload = struct.pack("<I", 16) + _png(2, 2, RGBA)
    else:
        payload = PIXELS
    count, pcount = 1 + linked, 1 + palette_link
    so, po = 512, 512 + count * 28
    lo = po + pcount * 16
    ld = PALETTE + (b"" if flags else payload)
    td = payload if flags else b""
    struct.pack_into("<8I", header, 36, so, count, po, pcount, lo, len(ld),
                     lo + len(ld), len(td))
    row = struct.pack("<HHHHhhHBBIIHH", 9000, 0 if linked else 1, 2, 2, 0, 0,
                      0, fmt, 5 if fmt in (3, 4) else 8, 0 if flags else len(PALETTE), len(payload),
                      1 if palette_link else 0, flags)
    if linked:
        row += struct.pack("<HHHHhhHBBIIHH", 9000, 1, 2, 2, 0, 0, 0, fmt, 8,
                           0, 0, 0, 0)
    pals = struct.pack("<HHHHII", 1, 1, 3, 0, 0, len(PALETTE))
    if palette_link:
        pals += struct.pack("<HHHHII", 1, 2, 3, 0, 0, 0)
    return bytes(header) + row + pals + ld + td


class PortraitTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(__file__).resolve().parents[1] / (".portrait-test-" + uuid.uuid4().hex)
        self.work.mkdir()
        self.addCleanup(shutil.rmtree, self.work)
        self.game = self.work / "game"
        self.char = self.game / "chars" / "Fighter"
        self.char.mkdir(parents=True)
        self.cache = PortraitCache(self.game, self.work / "cache", self.work / "overrides")
        self.definition = self.char / "Fighter.def"
        self.definition.write_text('[Files]\nsprite = "fighter.sff" ; comment\n', encoding="utf-8")
        self.asset = self.char / "fighter.sff"

    def extract(self, data):
        self.asset.write_bytes(data)
        path = self.cache.get("Fighter")
        self.assertIsNotNone(path, self.cache.errors)
        self.assertEqual(_decode_png(path.read_bytes()), (2, 2, RGBA))
        return path

    def reject(self, data, reason):
        self.asset.write_bytes(data)
        self.assertIsNone(self.cache.get("Fighter"))
        self.assertIn(reason, self.cache.errors["Fighter"])

    def test_v1_pcx_and_shared_palette_and_sprite_link(self):
        for encoding in (0, 1):
            with self.subTest(encoding=encoding):
                self.extract(sff1([(9000, 1, pcx(encoding=encoding), 0, 0)]))
        self.extract(sff1([(0, 0, pcx(), 0, 0),
                           (9000, 0, pcx(shared=True), 0, 1),
                           (9000, 1, b"", 1, 0)]))
        data = bytearray(sff1([(0, 0, pcx(), 0, 0),
                              (9000, 1, pcx(shared=True) + b"\x0c", 0, 1)]))
        struct.pack_into("<I", data, 512 + 32 + len(pcx()), len(data))
        self.extract(data)

    def test_v1_preference_and_fallback(self):
        self.extract(sff1([(9000, 0, pcx(), 0, 0), (9000, 1, b"bad", 0, 0)]))
        self.extract(sff1([(9000, 0, b"bad", 0, 0), (9000, 1, pcx(), 0, 0)]))

    def test_v2_codecs_and_links(self):
        for fmt in (0, 2, 3, 4, 10, 11, 12):
            for flags in (0, 1):
                with self.subTest(fmt=fmt, flags=flags):
                    self.extract(sff2(fmt, linked=True, palette_link=True, flags=flags))

    def test_sff_indexed_png_uses_sff_palette_not_dummy_black_png_colors(self):
        indexed_png = (
            SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 3, 0, 0, 0))
            + _chunk(b"PLTE", bytes(9))
            + _chunk(b"IDAT", zlib.compress(b"\0" + PIXELS[:2] + b"\0" + PIXELS[2:]))
            + _chunk(b"IEND", b"")
        )
        self.assertEqual(_decode_png(indexed_png, PALETTE), (2, 2, RGBA))
        for flags in (0, 1):
            for linked in (False, True):
                data = bytearray(sff2(10, flags=flags, linked=linked, palette_link=True))
                payload = struct.pack("<I", 4) + indexed_png
                old_length = struct.unpack_from("<I", data, 532)[0]
                data[-old_length:] = payload
                struct.pack_into("<I", data, 532, len(payload))
                if flags:
                    struct.pack_into("<I", data, 64, len(payload))
                else:
                    struct.pack_into("<I", data, 56, len(PALETTE) + len(payload))
                    struct.pack_into("<I", data, 60, len(data))
                self.extract(data)

    def test_rle_runs(self):
        data = bytearray(sff2())
        data[-4:] = b"\x41\x00\x01\x02\x01"
        struct.pack_into("<I", data, 512 + 20, 9)
        struct.pack_into("<I", data, 56, len(PALETTE) + 9)
        struct.pack_into("<I", data, 60, len(data))
        self.extract(data)
        pcxdata = pcx().replace(PIXELS, b"\xc1\x00\x01\x02\x01", 1)
        self.extract(sff1([(9000, 1, pcxdata, 0, 0)]))

    def test_invalid_sff_and_bounds(self):
        self.reject(b"bad", "SFF file")
        data = bytearray(sff2())
        data[:12] = bytes(12)
        self.reject(data, "signature")
        data = bytearray(sff2())
        data[15] = 9
        self.reject(data, "version 9")
        self.reject(sff2(9), "Unsupported SFF v2 codec 9")
        data = bytearray(sff2())
        struct.pack_into("<H", data, 516, 65535)
        self.reject(data, "dimensions")
        data = bytearray(sff2())
        struct.pack_into("<I", data, 528, 0xFFFFFFFF)
        self.reject(data, "data block")
        data = bytearray(sff2())
        data[-1] = 255
        self.reject(data, "palette index")
        data = bytearray(sff2())
        data[-2:] = b"\x7f\x01"
        self.reject(data, "exceeds decoded")
        self.reject(sff1([(9000, 1, pcx(shared=True), 0, 1)]), "shared PCX palette")
        self.reject(sff1([(9000, 1, b"", 0, 0)]), "cyclic SFF link")
        data = bytearray(sff1([(9000, 1, pcx(), 0, 0)]))
        struct.pack_into("<I", data, 512, 512)
        self.reject(data, "count mismatch")
        data = bytearray(sff2(linked=True))
        struct.pack_into("<H", data, 512 + 28 + 12, 1)
        # Remove fallback sprite's portrait identity to expose the broken link.
        struct.pack_into("<H", data, 512, 0)
        self.reject(data, "cyclic SFF link")
        data = bytearray(sff2(palette_link=True))
        struct.pack_into("<H", data, 512 + 28 + 16 + 6, 1)
        self.reject(data, "cyclic SFF link")

    def test_missing_and_path_escape(self):
        for entry in ("../escape", r"..\escape", r"C:\escape", "/escape", "-bad", "bad\0"):
            self.assertIsNone(self.cache.get(entry), entry)
        for path in ("../../outside.sff", r"C:\outside.sff", "/outside.sff"):
            self.definition.write_text(f"[Files]\nsprite={path}")
            self.assertIsNone(self.cache.get("Fighter"))
            self.assertRegex(self.cache.errors["Fighter"], "escapes|Unsafe")
        self.definition.write_text("[Info]\nsprite=ignored.sff")
        self.assertIsNone(self.cache.get("Fighter"))
        self.assertIn("[Files]", self.cache.errors["Fighter"])
        self.definition.write_text("[Files]\nsprite=missing.sff")
        self.assertIsNone(self.cache.get("Fighter"))
        self.assertIn("Missing sprite", self.cache.errors["Fighter"])
        with self.assertRaises(ValueError):
            PortraitCache(self.game, self.game / "cache", self.work / "overrides")

    def test_rle5_packet_counts_explicit_color_and_zero_runs(self):
        payload = b"\x02\x82\x07\x21\x42"  # 7x3, 1x2, 2x3.
        self.assertEqual(_five_bit(struct.pack("<I", 8) + payload, 8, False),
                         b"\x07" * 3 + b"\x01" * 2 + b"\x02" * 3)
        self.assertEqual(_five_bit(struct.pack("<I", 256) + b"\xff\x00", 256, False), bytes(256))

    def test_lz5_short_long_runs_and_overlapping_copy(self):
        # Two short RLE packets, an overlapping short copy, then a long RLE.
        payload = b"\x04\x21\x22\x03\x01\x03\x00"
        expected = b"\x01\x02" * 3 + b"\x03" * 8
        self.assertEqual(_five_bit(struct.pack("<I", 14) + payload, 14, True), expected)
        # One pixel followed by a long copy at distance one (3+2 pixels).
        self.assertEqual(_five_bit(struct.pack("<I", 6) + b"\x02\x21\x00\x00\x02", 6, True),
                         b"\x01" * 6)

    def test_lz5_recycled_offset_and_control_rollover(self):
        # Four short copies: high bits recycle 00,00,00,01 into distance two.
        payload = b"\x3c\x21\x22\x01\x01\x01\x01\x01\x01\x41"
        self.assertEqual(_five_bit(struct.pack("<I", 10) + payload, 10, True), b"\x01\x02" * 5)
        # Eight literal packets consume one control byte, the ninth another.
        self.assertEqual(_five_bit(struct.pack("<I", 9) + b"\x00" + b"\x21" * 8 + b"\x00\x22", 9, True),
                         b"\x01" * 8 + b"\x02")

    def test_five_bit_malformed_packets_fail_closed(self):
        for lz, expected, payload in (
            (False, 1, b""), (False, 1, b"\x00"), (False, 1, b"\x00\x80"),
            (False, 1, b"\x00\x80\x20"), (False, 1, b"\x01\x00"),
            (False, 1, b"\x00\x01"), (False, 1, b"\x00\x00\x00"),
            (True, 1, b""), (True, 1, b"\x00"), (True, 1, b"\x00\x00"),
            (True, 1, b"\x01\x01\x00"), (True, 1, b"\x01\x00\x00"),
            (True, 1, b"\x00\x41"), (True, 2, b"\x02\x21\x01\x00"),
            (True, 1, b"\x00\x21\x00"),
        ):
            with self.subTest(lz=lz, payload=payload), self.assertRaises(PortraitError):
                _five_bit(struct.pack("<I", expected) + payload, expected, lz)
        for lz in (False, True):
            with self.assertRaisesRegex(PortraitError, "decoded size"):
                _five_bit(struct.pack("<I", 999999999), 4, lz)

    def test_symlink_escape_when_available(self):
        outside = self.work / "outside.sff"
        outside.write_bytes(sff2())
        try:
            self.asset.symlink_to(outside)
        except OSError:
            self.skipTest("Symlink creation unavailable")
        self.assertIsNone(self.cache.get("Fighter"))
        self.assertIn("escapes", self.cache.errors["Fighter"])

    def test_override_naming_validation_and_precedence(self):
        overrides = self.work / "overrides"
        overrides.mkdir()
        key = hashlib.sha256(b"missing").hexdigest()
        override = overrides / (key + ".png")
        override.write_bytes(_png(2, 2, RGBA))
        result = self.cache.get("missing")
        self.assertIsNotNone(result, self.cache.errors)
        self.assertNotEqual(result, override)
        self.assertEqual(_decode_png(result.read_bytes()), (2, 2, RGBA))
        override.write_bytes(b"not PNG")
        self.assertIsNone(self.cache.get("missing"))
        self.assertIn("PNG", self.cache.errors["missing"])
        override.write_bytes(_png(2, 2, RGBA))
        self.assertIsNotNone(self.cache.get("missing"))
        self.assertNotIn("missing", self.cache.errors)

    def test_cache_reuse_invalidation_and_corruption(self):
        original = self.extract(sff2())
        mtime = original.stat().st_mtime_ns
        self.assertEqual(self.cache.get("Fighter"), original)
        self.assertEqual(original.stat().st_mtime_ns, mtime)
        original.write_bytes(b"corrupt")
        self.assertEqual(self.cache.get("Fighter"), original)
        self.assertEqual(_decode_png(original.read_bytes()), (2, 2, RGBA))
        stat = self.asset.stat()
        os.utime(self.asset, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        updated = self.cache.get("Fighter")
        self.assertNotEqual(updated, original)
        self.definition.write_text("[Files]\nsprite=fighter.sff\n; updated")
        self.assertNotEqual(self.cache.get("Fighter"), updated)

    def test_png_filters_and_color_types(self):
        for color, channels, rawpixel, expected in (
            (0, 1, b"\x15", b"\x15\x15\x15\xff"),
            (2, 3, b"\x01\x02\x03", b"\x01\x02\x03\xff"),
            (3, 1, b"\x01", b"\x0a\x14\x1e\xff"),
            (4, 2, b"\x15\x80", b"\x15\x15\x15\x80"),
            (6, 4, b"\x01\x02\x03\x80", b"\x01\x02\x03\x80"),
        ):
            for method in range(5):
                # Identical pixels on two rows exercise left/up/diagonal filters.
                prior = bytes(channels * 2)
                filtered = bytearray()
                for _ in range(2):
                    row = rawpixel * 2
                    filtered.append(method)
                    for x, val in enumerate(row):
                        a = row[x - channels] if x >= channels else 0
                        b = prior[x]
                        c = prior[x - channels] if x >= channels else 0
                        p = a + b - c
                        ds = (abs(p - a), abs(p - b), abs(p - c))
                        pred = (0, a, b, (a + b) // 2, (a, b, c)[ds.index(min(ds))])[method]
                        filtered.append((val - pred) & 255)
                    prior = row
                data = SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, color, 0, 0, 0))
                if color == 3:
                    data += _chunk(b"PLTE", b"\xff\0\xff\x0a\x14\x1e")
                    data += _chunk(b"tRNS", b"\0\xff")
                data += _chunk(b"IDAT", zlib.compress(filtered)) + _chunk(b"IEND", b"")
                self.assertEqual(_decode_png(data), (2, 2, expected * 4))

    def test_png_rejects_bombs_crc_truncation_and_interlacing(self):
        good = _png(2, 2, RGBA)
        for data in (good[:-1], good[:-5] + b"bad!!", good + b"trailing"):
            with self.assertRaises(PortraitError):
                _decode_png(data)
        for w, interlace, compressed in (
            (65535, 0, zlib.compress(b"")),
            (2, 1, zlib.compress(b"")),
            (2, 0, zlib.compress(bytes(1_000_000))),
        ):
            data = (SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, 2, 8, 6, 0, 0, interlace))
                    + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b""))
            with self.assertRaises(PortraitError):
                _decode_png(data)

    def test_png_rgb_and_grayscale_transparency(self):
        for color, pixel, transparent in (
            (0, b"\x15", struct.pack(">H", 21)),
            (2, b"\x01\x02\x03", struct.pack(">HHH", 1, 2, 3)),
        ):
            data = (SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, color, 0, 0, 0))
                    + _chunk(b"tRNS", transparent)
                    + _chunk(b"IDAT", zlib.compress(b"\0" + pixel)) + _chunk(b"IEND", b""))
            self.assertEqual(_decode_png(data)[2][-1], 0)

    def test_limits_and_diagnostics(self):
        data = bytearray(sff2())
        struct.pack_into("<I", data, 40, 100_001)
        self.reject(data, "record count")
        data = bytearray(sff2())
        struct.pack_into("<I", data, 512 + 20, 16 * 1024 * 1024 + 1)
        self.reject(data, "data block")
        self.reject(sff1([(0, 0, pcx(), 0, 0)]), "No portrait sprite")
        self.definition.write_bytes(b"x" * ((1 << 20) + 1))
        self.assertIsNone(self.cache.get("Fighter"))
        self.assertIn("Oversized character definition", self.cache.errors["Fighter"])

    def test_existing_staging_file_is_not_removed(self):
        destination = self.extract(sff2())
        destination.unlink()
        staging = destination.with_name(destination.name + ".writing")
        staging.write_bytes(b"belongs to another caller")
        self.assertIsNone(self.cache.get("Fighter"))
        self.assertEqual(staging.read_bytes(), b"belongs to another caller")

    def test_installed_roster_read_only(self):
        root = Path(r"E:\Games\Jump Force Mugen V13")
        if not (root / "data" / "select.def").is_file():
            self.skipTest("Optional installed game unavailable")
        cache = PortraitCache(root, self.work / "real-cache", self.work / "real-overrides")
        entries = ("Goku OP", "Goku_TP", "Xeno_Goku", "Goku_SS",
                   "majin goku", "GokuSS3", "Goku Xeno SSJ3",
                   "Rock Lee 8 portoes", "Zeref_JUS", "Tokito Oni")
        roster = (root / "data" / "select.def").read_text(encoding="cp1252")
        for entry in entries:
            self.assertIn(entry, roster)
            directory = root / "chars" / entry
            sources = list(directory.glob("*.def")) + list(directory.glob("*.sff"))
            before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in sources}
            result = cache.get(entry)
            self.assertIsNotNone(result, f"{entry}: {cache.errors}")
            w, h, rgba = _decode_png(result.read_bytes())
            self.assertEqual(len(rgba), w * h * 4)
            self.assertTrue(any(rgba[3::4]), "Portrait is entirely transparent")
            self.assertTrue(any(max(rgba[i:i + 3]) > 35 and rgba[i + 3]
                                for i in range(0, len(rgba), 4)), "Portrait has no visible color")
            self.assertEqual(before, {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in sources})
            print(f"\nInstalled portrait: {entry}: {w}x{h}")


if __name__ == "__main__":
    unittest.main()
