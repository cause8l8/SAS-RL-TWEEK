"""Cross-process guard preventing overlapping optimization sessions."""

from __future__ import annotations

import ctypes
import threading
from contextlib import AbstractContextManager

from sas_booster.utils.windows import is_windows


class SessionBusyError(RuntimeError):
    pass


class SessionMutex(AbstractContextManager["SessionMutex"]):
    _fallback = threading.Lock()

    def __init__(self, name: str = r"Local\SASGameBoosterOptimization") -> None:
        self.name = name
        self._handle: int | None = None
        self._fallback_owned = False

    def __enter__(self) -> "SessionMutex":
        if not is_windows():
            if not self._fallback.acquire(blocking=False):
                raise SessionBusyError("Another optimization operation is already running")
            self._fallback_owned = True
            return self

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.ReleaseMutex.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError("Unable to create the optimization session mutex")
        result = int(kernel32.WaitForSingleObject(handle, 0))
        if result not in {0x00000000, 0x00000080}:  # acquired or abandoned
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise SessionBusyError("Another SAS optimization operation is already running")
        self._handle = int(handle)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._fallback_owned:
            self._fallback.release()
            self._fallback_owned = False
        if self._handle is not None:
            handle = ctypes.c_void_p(self._handle)
            ctypes.windll.kernel32.ReleaseMutex(handle)
            ctypes.windll.kernel32.CloseHandle(handle)
            self._handle = None
