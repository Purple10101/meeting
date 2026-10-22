"""Ensures only one copy runs; launching it again asks the running copy to show its window."""
import ctypes
import threading
from ctypes import wintypes

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
_kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_kernel32.WaitForSingleObject.restype = wintypes.DWORD

ERROR_ALREADY_EXISTS = 183
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0


class SingleInstance:
    """A named Windows event doubles as the instance lock and the 'show yourself' signal."""

    def __init__(self, name: str = "Local\\MeetingRecorder.Show"):
        self._handle = _kernel32.CreateEventW(None, False, False, name)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    def signal_existing(self) -> None:
        _kernel32.SetEvent(self._handle)

    def on_signal(self, callback) -> None:
        def wait():
            while _kernel32.WaitForSingleObject(self._handle, INFINITE) == WAIT_OBJECT_0:
                callback()

        threading.Thread(target=wait, name="single-instance", daemon=True).start()
