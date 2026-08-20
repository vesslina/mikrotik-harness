from __future__ import annotations

import ctypes
from ctypes import wintypes

from mth.ui.textual.clipboard import _configure_windows_api


class _Function:
    def __init__(self) -> None:
        self.argtypes: list[object] | None = None
        self.restype: object | None = None


class _Library:
    def __init__(self, names: tuple[str, ...]) -> None:
        for name in names:
            setattr(self, name, _Function())


def test_windows_clipboard_uses_pointer_sized_handle_signatures() -> None:
    user32 = _Library(
        (
            "OpenClipboard",
            "CloseClipboard",
            "IsClipboardFormatAvailable",
            "GetClipboardData",
            "EmptyClipboard",
            "SetClipboardData",
        )
    )
    kernel32 = _Library(
        ("GlobalAlloc", "GlobalLock", "GlobalUnlock", "GlobalFree")
    )

    _configure_windows_api(user32, kernel32)  # type: ignore[arg-type]

    assert user32.GetClipboardData.restype is wintypes.HANDLE
    assert user32.SetClipboardData.argtypes == [wintypes.UINT, wintypes.HANDLE]
    assert kernel32.GlobalAlloc.argtypes == [wintypes.UINT, ctypes.c_size_t]
    assert kernel32.GlobalLock.argtypes == [wintypes.HGLOBAL]
    assert kernel32.GlobalLock.restype is wintypes.LPVOID
    assert kernel32.GlobalFree.argtypes == [wintypes.HGLOBAL]
