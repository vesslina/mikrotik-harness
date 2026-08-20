from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _configure_windows_api(user32: ctypes.WinDLL, kernel32: ctypes.WinDLL) -> None:
    """Declare pointer-sized Win32 clipboard signatures for 64-bit Python."""

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE

    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL


def _windows_libraries() -> tuple[ctypes.WinDLL, ctypes.WinDLL] | None:
    if os.name != "nt":
        return None
    libraries = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL(
        "kernel32", use_last_error=True
    )
    _configure_windows_api(*libraries)
    return libraries


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
    try:
        opened = _open(user32)
    except (OSError, ctypes.ArgumentError, ValueError):
        return None
    if not opened:
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
    except (OSError, ctypes.ArgumentError, ValueError):
        return None
    finally:
        try:
            if pointer and handle:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()


def write_text(text: str) -> bool:
    """Write Unicode text to the Windows clipboard without shelling out."""

    libraries = _windows_libraries()
    if libraries is None:
        return False
    user32, kernel32 = libraries
    try:
        opened = _open(user32)
    except (OSError, ctypes.ArgumentError, ValueError):
        return False
    if not opened:
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
    except (OSError, ctypes.ArgumentError, ValueError):
        return False
    finally:
        try:
            user32.CloseClipboard()
        finally:
            if handle and not transferred:
                kernel32.GlobalFree(handle)
