"""画面の表示書式（docs/design/DESIGN.md 8 章）"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import recorder

DASH = "---"


def fmt_voltage(v: float) -> str:
    return f"{v:.3f} V"


def fmt_current(i: float) -> str:
    return f"{i:.3f} A"


def fmt_power(p: float) -> str:
    """10 W 未満は小数 3 桁、100 W 未満は 2 桁、それ以上は 1 桁（丸めた結果で判定）"""
    if abs(round(p, 3)) < 10:
        return f"{p:.3f} W"
    if abs(round(p, 2)) < 100:
        return f"{p:.2f} W"
    return f"{p:.1f} W"


def fmt_mah(mah: float) -> str:
    return f"{mah:.1f} mAh"


def fmt_wh(wh: float) -> str:
    return f"{wh:.3f} Wh"


def fmt_elapsed(seconds: float) -> str:
    """HH:MM:SS（100 時間以上は時の桁が増える）"""
    s = int(max(seconds, 0))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_message(text: str, now: datetime | None = None) -> str:
    return f"{(now or datetime.now()):%H:%M:%S} {text}"


def chip_values(current: float | None, cutoff: float | None, interval: float | None,
                full_voltage: str | None, maker: str | None) -> list[str]:
    """②-6 条件チップの値（電流 / 終止電圧 / 取得周期 / 満充電 / メーカー）。未選択・未入力は ---"""
    return [
        fmt_current(current) if current is not None else DASH,
        fmt_voltage(cutoff) if cutoff is not None else DASH,
        f"{recorder.format_interval(interval)} s" if interval is not None else DASH,
        f"{full_voltage[:-1]} V" if full_voltage else DASH,
        maker or DASH,
    ]


def planned_name_pattern(full_voltage: str | None, maker: str | None, number: str | None = None) -> str:
    """待機中の「保存予定」に出すパターン（出力フォルダ YYYYMMDD_HHMM_SDL の中のファイル名）"""
    parts = ["YYYYMMDD_HHMM"]
    if full_voltage:
        parts.append(recorder.full_voltage_tag(full_voltage))
    if maker:
        parts.append(recorder.MAKER_ABBR[maker])
    if number:
        parts.append("no" + number)
    return str(Path("YYYYMMDD_HHMM" + recorder.RUN_DIR_SUFFIX) / ("_".join(parts) + ".csv"))
