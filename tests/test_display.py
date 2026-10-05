"""表示書式（DESIGN.md 8 章、D-AC-04）"""

from datetime import datetime
from pathlib import Path

import display


def test_power_digits_switch():
    """電力 9.999 / 10.00 / 100.0 W の切り替え"""
    assert display.fmt_power(9.999) == "9.999 W"
    assert display.fmt_power(10.0) == "10.00 W"
    assert display.fmt_power(99.99) == "99.99 W"
    assert display.fmt_power(100.0) == "100.0 W"
    assert display.fmt_power(3.491) == "3.491 W"
    # 丸めると桁が変わる値は、丸めた後の大きさで決める
    assert display.fmt_power(9.9996) == "10.00 W"
    assert display.fmt_power(99.996) == "100.0 W"


def test_other_formats():
    assert display.fmt_voltage(3.4912) == "3.491 V"
    assert display.fmt_current(1.0) == "1.000 A"
    assert display.fmt_mah(2209.74) == "2209.7 mAh"
    assert display.fmt_wh(8.3714) == "8.371 Wh"


def test_elapsed():
    assert display.fmt_elapsed(0) == "00:00:00"
    assert display.fmt_elapsed(2 * 3600 + 12 * 60 + 35) == "02:12:35"
    assert display.fmt_elapsed(123 * 3600 + 5) == "123:00:05"  # 100 時間以上は時の桁が増える


def test_message_has_time():
    assert display.fmt_message("放電開始（1.000 A / 終止 3.000 V）", datetime(2026, 10, 1, 14, 30, 5)) == \
        "14:30:05 放電開始（1.000 A / 終止 3.000 V）"


def test_chip_values_and_dash():
    assert display.chip_values(1.0, 3.0, 1.0, "4.1V", "Panasonic") == ["1.000 A", "3.000 V", "1.0 s", "4.1 V",
                                                                       "Panasonic"]
    assert display.chip_values(None, None, None, None, None) == ["---"] * 5


def test_planned_name_pattern():
    """出力フォルダ YYYYMMDD_HHMM_SDL の中のファイル名"""
    folder = Path("YYYYMMDD_HHMM_SDL")
    assert display.planned_name_pattern("4.1V", "Panasonic") == str(folder / "YYYYMMDD_HHMM_4v1_pana.csv")
    assert display.planned_name_pattern(None, "マクセル") == str(folder / "YYYYMMDD_HHMM_maxell.csv")
    assert display.planned_name_pattern(None, None) == str(folder / "YYYYMMDD_HHMM.csv")
    assert display.planned_name_pattern("4.1V", None, "3") == str(folder / "YYYYMMDD_HHMM_4v1_no3.csv")


def test_colors_defined_in_one_place():
    """D-AC-02：色は theme.COLORS にまとまっていて、画面のコードに色の値を直接書いていない"""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    for name in ("gui.py", "plotting.py"):
        text = (src / name).read_text(encoding="utf-8")
        # 白（#ffffff）と、ボタン枠の濃い色だけは部品の定義に残してよい
        found = set(re.findall(r'"#[0-9a-fA-F]{6}"', text)) - {'"#ffffff"', '"#000000"', '"#8e1c16"',
                                                                 '"#f2f2f2"', '"#c9c9c9"', '"#4a5056"'}
        assert not found, f"{name} に色が直接書かれています: {found}"
