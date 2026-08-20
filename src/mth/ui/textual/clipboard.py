from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _windows_libraries() -> tuple[ctypes.WinDLL, ctypes.WinDLL] | None:
    if os.name != "nt":
        return None
    return ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL(
        "kernel32", use_last_error=True
    )


def _open(user32: ctypes.WinDLL) -> bool:
    for _ in range(5):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def read_text() -> str | None:
    """Read Unicode text from the Windows clipboard, if available."""

    libraries = _windows_libraries()
    if libraries is None:
        return None
    user32, kernel32 = libraries
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.restype = wintypes.LPVOID
    if not _open(user32):
        return None
    handle: int | None = None
    pointer: int | None = None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        return ctypes.wstring_at(pointer)
    finally:
        if pointer and handle:
            kernel32.GlobalUnlock(handle)
        user32.CloseClipboard()


def write_text(text: str) -> bool:
    """Write Unicode text to the Windows clipboard without shelling out."""

    libraries = _windows_libraries()
    if libraries is None:
        return False
    user32, kernel32 = libraries
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.restype = wintypes.LPVOID
    user32.SetClipboardData.restype = wintypes.HANDLE
    if not _open(user32):
        return False
    handle: int | None = None
    transferred = False
    try:
        if not user32.EmptyClipboard():
            return False
        buffer = ctypes.create_unicode_buffer(text)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(buffer))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return False
        try:
            ctypes.memmove(pointer, ctypes.addressof(buffer), ctypes.sizeof(buffer))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False
        transferred = True
        return True
    finally:
        user32.CloseClipboard()
        if handle and not transferred:
            kernel32.GlobalFree(handle)
