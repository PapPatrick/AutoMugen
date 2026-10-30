"""Read-only round observer for the exact inspected MUGEN 1.1 executable."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import struct
import threading
import time


SUPPORTED_SHA256 = "30bdfba28e9d946e0dde859d07a6bab5d5abcf5fef9f48c62e9d13904f52ab2e"
# Verified against the registered roundstate/roundno Lua functions and the
# engine's KO/time-over branches. Addresses are relative to the module base.
ENGINE_POINTER_RVA = 0x1040E8
ROUND_BLOCK_OFFSET = 0x12728
BLOCK_SIZE = 0x3C


def supported(exe: Path) -> bool:
    return hashlib.sha256(exe.read_bytes()).hexdigest() == SUPPORTED_SHA256


class ObservationError(Exception):
    pass


class MemoryReadError(ObservationError):
    pass


class WindowsMemory:
    def __init__(self, pid: int, expected_module: str = "Mugen.exe", image_path: Path | None = None):
        if os.name != "nt":
            raise ObservationError("Automatic winner observation requires Windows.")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.api.ReadProcessMemory.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
        ]
        self.api.ReadProcessMemory.restype = wintypes.BOOL
        self.api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

        class Module(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                ("szModule", wintypes.WCHAR * 256), ("szExePath", wintypes.WCHAR * 260),
            ]

        self.api.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Module)]
        self.api.Module32FirstW.restype = wintypes.BOOL
        self.handle = self.api.OpenProcess(0x0010 | 0x1000, False, pid)
        if not self.handle:
            raise ObservationError(f"Cannot read launched game process {pid}: Windows error {ctypes.get_last_error()}.")
        try:
            if image_path is not None:
                # This pinned 32-bit image has relocations stripped. Verify its
                # mapped PE header directly, avoiding startup/WOW64 Toolhelp 299.
                image = image_path.read_bytes()
                if hashlib.sha256(image).hexdigest() != SUPPORTED_SHA256:
                    raise ObservationError("Unsupported executable image.")
                pe = struct.unpack_from("<I", image, 60)[0]
                if not struct.unpack_from("<H", image, pe + 22)[0] & 1:
                    raise ObservationError("Expected fixed-base executable; relocations are not stripped.")
                self.base = struct.unpack_from("<I", image, pe + 52)[0]
                for attempt in range(100):
                    try:
                        if self.read(self.base, 512) != image[:512]:
                            raise ObservationError("Mapped game image does not match the inspected executable.")
                        return
                    except MemoryReadError:
                        if attempt == 99:
                            raise
                        time.sleep(0.05)
            for attempt in range(20):
                snapshot = self.api.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
                if snapshot != ctypes.c_void_p(-1).value or ctypes.get_last_error() not in (24, 299):
                    break
                # Windows documents ERROR_BAD_LENGTH while loader modules change.
                time.sleep(0.05)
            if snapshot == ctypes.c_void_p(-1).value:
                raise ObservationError(f"Cannot inspect game modules: Windows error {ctypes.get_last_error()}.")
            try:
                module = Module()
                module.dwSize = ctypes.sizeof(Module)
                if not self.api.Module32FirstW(snapshot, ctypes.byref(module)):
                    raise ObservationError(f"Cannot locate game module: Windows error {ctypes.get_last_error()}.")
                if module.szModule.casefold() != expected_module.casefold():
                    raise ObservationError("Launched process module is not Mugen.exe.")
                self.base = module.modBaseAddr
            finally:
                self.api.CloseHandle(snapshot)
        except Exception:
            self.close()
            raise

    def read(self, address: int, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if not self.api.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(count)) or count.value != size:
            raise MemoryReadError(f"Game memory read failed: Windows error {ctypes.get_last_error()}.")
        return buffer.raw

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def decode_round(block: bytes) -> dict:
    if len(block) != BLOCK_SIZE:
        raise ObservationError("Incomplete engine round snapshot.")
    values = struct.unpack("<15i", block)
    return {
        "round": values[0], "state": values[11],
        "side": values[12], "finish": values[13],
    }


class RoundEvidence:
    def __init__(self):
        self.active_seen = False
        self.candidate = None
        self.samples = 0
        self.result = None

    def feed(self, sample: dict):
        if sample["state"] not in range(5) or sample["round"] not in (0, 1):
            raise ObservationError("Unexpected engine round/state; refusing to infer a result.")
        if sample["round"] != 1:
            return
        if sample["state"] == 2:
            if self.candidate is not None:
                raise ObservationError("Fight resumed after a completed-round candidate; manual review required.")
            self.active_seen = True
        if not self.active_seen or sample["state"] not in (3, 4):
            return
        side, finish = sample["side"], sample["finish"]
        if side not in (0, 1, 2) or finish not in (0, 1, 2, 3):
            raise ObservationError("Invalid engine outcome values.")
        if finish == 0:
            return
        candidate = (side, finish)
        if self.result is not None and candidate != (self.result["side"], self.result["finish"]):
            raise ObservationError("Conflicting engine results; manual review required.")
        if candidate == self.candidate:
            self.samples += 1
        else:
            self.candidate, self.samples = candidate, 1
        if self.samples >= 3:
            self.result = {
                "side": side, "finish": finish,
                "outcome": "winner" if side in (1, 2) and finish in (1, 3) else "draw",
                "samples": self.samples, "round": 1,
            }


class WinnerObserver:
    def __init__(self, pid: int, exe: Path):
        if not supported(exe):
            raise ObservationError("Unsupported Mugen.exe build; automatic detection is disabled.")
        self.pid = pid
        self.exe = exe
        self.evidence = RoundEvidence()
        self.error = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"winner-{pid}", daemon=True)
        self.thread.start()

    def _run(self):
        memory = None
        try:
            memory = WindowsMemory(self.pid, image_path=self.exe)
            pointer = None
            initialized = False
            while not self.stop_event.is_set():
                raw = memory.read(memory.base + ENGINE_POINTER_RVA, 4)
                address = struct.unpack("<I", raw)[0]
                if address:
                    if pointer is not None and address != pointer:
                        raise ObservationError("Engine pointer changed during observation.")
                    pointer = address
                    # The engine is initialized before its first round.
                    block = memory.read(address + ROUND_BLOCK_OFFSET, BLOCK_SIZE)
                    again = memory.read(address + ROUND_BLOCK_OFFSET, BLOCK_SIZE)
                    if block == again:
                        sample = decode_round(block)
                        if sample["round"] == 1 and sample["state"] in (1, 2):
                            initialized = True
                        if initialized:
                            self.evidence.feed(sample)
                self.stop_event.wait(0.02)
        except (ObservationError, OSError) as exc:
            # A final read can race with normal process teardown. A result
            # already observed in a stable round snapshot remains usable.
            if self.evidence.result is None or not isinstance(exc, MemoryReadError):
                self.error = str(exc)
        finally:
            if memory:
                memory.close()

    def finish(self) -> dict:
        self.stop_event.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            return {"error": "Winner observer did not stop safely."}
        if self.error:
            return {"error": self.error}
        if self.evidence.result is None:
            return {"error": "No stable completed-round result was observed."}
        return {"pid": self.pid, "build": SUPPORTED_SHA256, **self.evidence.result}
