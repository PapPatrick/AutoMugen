import ctypes
import os
import struct
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from winner import (
    BLOCK_SIZE, MemoryReadError, ObservationError, RoundEvidence,
    WinnerObserver, WindowsMemory, decode_round,
)


def sample(state=2, side=0, finish=0, round_number=1):
    return {"round": round_number, "state": state, "side": side, "finish": finish}


class EvidenceTests(unittest.TestCase):
    def test_exact_engine_field_layout(self):
        values = [0] * 15
        values[0], values[11], values[12], values[13] = 1, 3, 2, 1
        self.assertEqual(decode_round(struct.pack("<15i", *values)), sample(3, 2, 1))
        with self.assertRaises(ObservationError):
            decode_round(bytes(BLOCK_SIZE - 1))

    def test_both_player_sides_and_finish_types(self):
        for side in (1, 2):
            for finish in (1, 3):
                with self.subTest(side=side, finish=finish):
                    e = RoundEvidence()
                    e.feed(sample())
                    e.feed(sample(3, side, finish))
                    e.feed(sample(3, side, finish))
                    self.assertIsNone(e.result)
                    e.feed(sample(3, side, finish))
                    self.assertEqual(e.result["side"], side)
                    self.assertEqual(e.result["outcome"], "winner")

    def test_stale_result_without_observed_fight_never_counts(self):
        e = RoundEvidence()
        for _ in range(10):
            e.feed(sample(4, 1, 1))
        self.assertIsNone(e.result)

    def test_draw_and_double_ko_never_become_winner(self):
        for side, finish in ((0, 2), (0, 3), (1, 2), (2, 2)):
            e = RoundEvidence()
            e.feed(sample())
            for _ in range(3):
                e.feed(sample(3, side, finish))
            self.assertEqual(e.result["outcome"], "draw")

    def test_conflicting_and_invalid_results_are_rejected(self):
        e = RoundEvidence()
        e.feed(sample())
        for _ in range(3):
            e.feed(sample(3, 1, 1))
        with self.assertRaisesRegex(ObservationError, "Conflicting"):
            e.feed(sample(3, 2, 1))
        for bad in (sample(3, 7, 1), sample(3, 1, 9), sample(9), sample(round_number=2)):
            with self.subTest(bad=bad), self.assertRaises(ObservationError):
                e.feed(bad)

    def test_candidate_must_stabilize(self):
        e = RoundEvidence()
        e.feed(sample())
        e.feed(sample(3, 1, 1))
        e.feed(sample(3, 2, 1))
        e.feed(sample(3, 2, 1))
        self.assertIsNone(e.result)
        e.feed(sample(3, 2, 1))
        self.assertEqual(e.result["side"], 2)

    def test_round_reset_after_outcome_invalidates_prior_candidate(self):
        e = RoundEvidence()
        e.feed(sample())
        for _ in range(3):
            e.feed(sample(3, 1, 1))
        with self.assertRaisesRegex(ObservationError, "Fight resumed"):
            e.feed(sample())

    def test_unsupported_binary_never_opens_process(self):
        with patch("winner.supported", return_value=False), patch("winner.WindowsMemory") as memory:
            with self.assertRaisesRegex(ObservationError, "Unsupported"):
                WinnerObserver(123, None)
            memory.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows-only memory API validation")
    def test_fixed_image_attach_verifies_mapped_header_without_toolhelp(self):
        # A local buffer substitutes for the mapped PE header; only our own
        # process is opened, and no game is launched.
        image = bytearray(512)
        struct.pack_into("<I", image, 60, 128)
        struct.pack_into("<H", image, 150, 1)
        struct.pack_into("<I", image, 180, 0x400000)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Mugen.exe"
            path.write_bytes(image)
            with patch("winner.SUPPORTED_SHA256", __import__("hashlib").sha256(image).hexdigest()), patch.object(WindowsMemory, "read", return_value=bytes(image)) as read:
                memory = WindowsMemory(os.getpid(), image_path=path)
                try:
                    self.assertEqual(memory.base, 0x400000)
                    read.assert_called_once_with(0x400000, 512)
                finally:
                    memory.close()

    @unittest.skipUnless(os.name == "nt", "Windows-only memory API validation")
    def test_fixed_image_startup_read_retries_and_rejects_mismatch(self):
        image = bytearray(512)
        struct.pack_into("<I", image, 60, 128)
        struct.pack_into("<H", image, 150, 1)
        struct.pack_into("<I", image, 180, 0x400000)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Mugen.exe"
            path.write_bytes(image)
            with patch("winner.SUPPORTED_SHA256", __import__("hashlib").sha256(image).hexdigest()), patch("winner.time.sleep"), patch.object(WindowsMemory, "read", side_effect=[MemoryReadError("299"), bytes(image)]) as read:
                memory = WindowsMemory(os.getpid(), image_path=path)
                memory.close()
                self.assertEqual(read.call_count, 2)
            with patch("winner.SUPPORTED_SHA256", __import__("hashlib").sha256(image).hexdigest()), patch.object(WindowsMemory, "read", return_value=b"x" * 512):
                with self.assertRaisesRegex(ObservationError, "does not match"):
                    WindowsMemory(os.getpid(), image_path=path)

    @unittest.skipUnless(os.name == "nt", "Windows-only memory API validation")
    def test_read_process_memory_binding_reads_only_our_own_allocation(self):
        # Exercise the actual Windows API without starting or modifying a game.
        memory = WindowsMemory(os.getpid(), expected_module=Path(sys.executable).name)
        self.assertTrue(memory.handle)
        self.assertGreater(memory.base, 0)
        try:
            data = ctypes.create_string_buffer(b"automugen-read-only")
            self.assertEqual(memory.read(ctypes.addressof(data), ctypes.sizeof(data)), b"automugen-read-only\0")
            with self.assertRaises(MemoryReadError):
                memory.read(1, 4)
        finally:
            memory.close()
