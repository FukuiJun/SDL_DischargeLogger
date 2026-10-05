"""Windows でウィンドウのアイコンを確実に設定する

Tk の iconbitmap だけではタイトルバーのアイコン（小）は変わっても、タスクバー・Alt+Tab に使われる
大きいアイコンが Tk の既定（羽根）のままになることがある。そこで .ico から小・大のアイコンを読み込み、
WM_SETICON でウィンドウに、SetClassLongPtr でウィンドウクラスにも設定する。Windows 以外では何もしない。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger("sdl.winicon")

WM_SETICON = 0x0080
WM_GETICON = 0x007F
ICON_SMALL = 0
ICON_BIG = 1
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXICON, SM_CYICON, SM_CXSMICON, SM_CYSMICON = 11, 12, 49, 50
GCLP_HICON = -14
GCLP_HICONSM = -34


def toplevel_hwnd(root) -> int:
    """Tk のルートウィンドウの外枠（タイトルバーを持つウィンドウ）のハンドル"""
    import ctypes

    root.update_idletasks()
    inner = root.winfo_id()
    return ctypes.windll.user32.GetParent(inner) or inner


def apply(root, ico: Path) -> tuple[int, int] | None:
    """.ico をタイトルバー（小）とタスクバー（大）の両方に設定する。設定したアイコンのハンドル（大, 小）を返す"""
    if sys.platform != "win32" or not Path(ico).exists():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        set_class = getattr(user32, "SetClassLongPtrW", None) or user32.SetClassLongW
        set_class.restype = ctypes.c_size_t
        set_class.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_size_t]

        hwnd = toplevel_hwnd(root)
        big = user32.LoadImageW(None, str(ico), IMAGE_ICON, user32.GetSystemMetrics(SM_CXICON),
                                user32.GetSystemMetrics(SM_CYICON), LR_LOADFROMFILE)
        small = user32.LoadImageW(None, str(ico), IMAGE_ICON, user32.GetSystemMetrics(SM_CXSMICON),
                                  user32.GetSystemMetrics(SM_CYSMICON), LR_LOADFROMFILE)
        if not big or not small:
            log.warning("アイコンを読み込めません: %s", ico)
            return None
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
        # 同じクラスのウィンドウ（ダイアログなど）とタスクバーがクラスのアイコンを使う場合に備える
        set_class(hwnd, GCLP_HICON, big)
        set_class(hwnd, GCLP_HICONSM, small)
        return big, small
    except Exception as e:  # noqa: BLE001 - アイコンが付かなくても動作は続ける
        log.warning("ウィンドウのアイコンを設定できません: %s", e)
        return None


def current(root) -> tuple[int, int]:
    """ウィンドウに今設定されているアイコンのハンドル（大, 小）。確認用"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    hwnd = toplevel_hwnd(root)
    return (user32.SendMessageW(hwnd, WM_GETICON, ICON_BIG, 0), user32.SendMessageW(hwnd, WM_GETICON, ICON_SMALL, 0))
