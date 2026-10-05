"""測定中に Windows が画面を消したり・ロックしたり・スリープしたりしないようにする

SetThreadExecutionState を使う。操作がないことによる自動の画面オフ・ロック・スリープを止める。
手動のスリープ、カバーを閉じたときのスリープ、Win+L によるロックは止められない。
Windows 以外では何もしない。
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("sdl.power")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

_active = False


def keep_awake(on: bool) -> bool:
    """on=True で画面オフ・ロック・スリープを止める。False で元に戻す。

    同じスレッド（画面のスレッド）から呼ぶこと。Windows で設定できたら True
    """
    global _active
    if on == _active:
        return True
    _active = on
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0)
        ok = ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0
    except Exception as e:  # noqa: BLE001
        log.warning("画面オフ・スリープの設定を変えられません: %s", e)
        return False
    log.info("画面オフ・ロック・スリープを%s", "止めました" if on else "元に戻しました")
    return ok


def is_active() -> bool:
    return _active
