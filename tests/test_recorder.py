"""ファイル名・積算・一時ファイル・最終 CSV（AC-02, AC-03, 5.2）"""

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import recorder
from recorder import Integrator, PartialWriter, Sample, TestInfo

START = datetime(2026, 10, 1, 14, 30, 5)


@pytest.mark.parametrize("full, maker, expected", [
    ("4.1V", "Panasonic", "20261001_1430_4v1_pana"),
    ("4.2V", "マクセル", "20261001_1430_4v2_maxell"),
    (None, "Panasonic", "20261001_1430_pana"),
    ("4.1V", None, "20261001_1430_4v1"),
    (None, None, "20261001_1430"),
])
def test_base_name_patterns(full, maker, expected):
    """AC-02：仕様書 5.2 の表の 5 パターン（日時は分まで。満充電電圧は 4v1 のようにドット・大文字なし）"""
    assert recorder.make_base_name(START, full, maker) == expected


def test_unique_base_name_adds_suffix(tmp_path):
    """AC-02：同名ファイルがあると _2、さらにあると _3"""
    base = "20261001_1430_4v1_pana"
    assert recorder.unique_base_name(tmp_path, base) == base
    (tmp_path / f"{base}.csv").write_text("x")
    assert recorder.unique_base_name(tmp_path, base) == f"{base}_2"
    (tmp_path / f"{base}_2_partial.csv").write_text("x")
    assert recorder.unique_base_name(tmp_path, base) == f"{base}_3"
    (tmp_path / f"{base}_3.png").write_text("x")
    assert recorder.unique_base_name(tmp_path, base) == f"{base}_4"


def test_capacity_1a_3600s():
    """AC-03：1.000A 一定・3600 秒で 1000.0 ± 0.1 mAh"""
    integ = Integrator()
    for t in range(3601):
        integ.add(float(t), 1.000, 4.0)
    assert integ.mah == pytest.approx(1000.0, abs=0.1)
    assert integ.wh == pytest.approx(4.0, abs=0.001)


def test_capacity_trapezoid_with_gap():
    """欠損区間（通信断からの復帰）も台形で積算する"""
    integ = Integrator()
    integ.add(0.0, 1.0, 4.0)
    integ.add(1.0, 1.0, 4.0)
    integ.add(11.0, 1.0, 4.0)  # 10 秒の欠損
    assert integ.mah == pytest.approx(11 / 3.6)


def test_sample_row_format():
    s = Sample(datetime(2026, 10, 1, 14, 30, 6, 121000), 1.0012, 4.1502, 1.0001, 4.1506, 0.2779, 0.00116,
               "4.15020", "1.0001", "4.1506")
    assert s.row() == ["2026/10/01 14:30:06.121", "1.001", "4.15020", "1.0001", "4.1506", "0.278", "0.0012"]


def _info(**kw):
    base = dict(start=START, end=datetime(2026, 10, 1, 17, 12, 40), end_reason="終止電圧到達", maker="Panasonic",
                full_voltage="4.1V", model="NCR18650B", current=1.0, cutoff=3.0, mah=2702.5, wh=9.874,
                interval=1.0, idn="Siglent Technologies,SDL1020X-E,SDL001,1.1", note="25℃恒温槽内。2回目",
                number="a-12")
    base.update(kw)
    return TestInfo(**base)


def _write(tmp_path, info, n=3):
    partial = tmp_path / "x_partial.csv"
    w = PartialWriter(partial)
    integ = Integrator()
    for k in range(n):
        mah, wh = integ.add(float(k), 1.0, 4.1)
        w.append(Sample(START + timedelta(seconds=k), float(k), 4.1, 1.0, 4.1, mah, wh))
    w.close()
    out = tmp_path / "x.csv"
    recorder.write_final_csv(out, info, recorder.read_measured(partial))
    return out


def test_final_csv_layout(tmp_path):
    out = _write(tmp_path, _info())
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM
    text = raw.decode("utf-8-sig")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    lines = text.split("\r\n")
    assert lines[0] == "試験情報"
    assert lines[1] == "開始日時,2026/10/01 14:30"  # 試験情報の日時は分まで
    assert lines[2] == "終了日時,2026/10/01 17:12"
    assert lines[3] == "終了理由,終止電圧到達"
    assert lines[4] == "メーカー,Panasonic"
    assert lines[5] == "満充電電圧[V],4.1"
    assert lines[6] == "型番,NCR18650B"
    assert lines[7] == "番号,a-12"
    assert lines[8] == "放電電流[A],1.000"
    assert lines[9] == "終止電圧[V],3.000"
    assert lines[10] == "放電容量[mAh],2702.5"
    assert lines[11] == "電力量[Wh],9.874"
    assert lines[12] == "取得周期[s],1.0"
    assert lines[13] == '機器,"Siglent Technologies,SDL1020X-E,SDL001,1.1"'
    assert lines[14] == '備考,"25℃恒温槽内。2回目"'
    assert lines[15] == ""
    assert lines[16] == "日時,経過時間[s],電圧[V],電流[A],電力[W],放電容量[mAh],電力量[Wh]"
    assert lines[17].startswith("2026/10/01 14:30:05.000,0.000,")
    assert len([ln for ln in lines[17:] if ln]) == 3


def test_final_csv_unselected_fields_are_blank(tmp_path):
    """AC-05：メーカー・満充電電圧が未選択、型番が未入力なら値が空欄（行は残す）"""
    out = _write(tmp_path, _info(maker=None, full_voltage=None, model="", number=""))
    rows = list(csv.reader(out.read_text(encoding="utf-8-sig").splitlines()))
    d = {r[0]: r[1:] for r in rows if r}
    assert d["メーカー"] == [""]
    assert d["満充電電圧[V]"] == [""]
    assert d["型番"] == [""]
    assert d["番号"] == [""]


def test_final_csv_note_with_newline_comma_quote(tmp_path):
    note = '1行目, カンマ\n2行目 "引用"'
    out = _write(tmp_path, _info(note=note))
    with open(out, encoding="utf-8-sig", newline="") as fp:
        rows = list(csv.reader(fp))
    d = {r[0]: r[1:] for r in rows if r}
    assert d["備考"] == [note]
    assert d["機器"] == ["Siglent Technologies,SDL1020X-E,SDL001,1.1"]


def test_final_csv_does_not_overwrite(tmp_path):
    (tmp_path / "x.csv").write_text("既存")
    with pytest.raises(FileExistsError):
        _write(tmp_path, _info())
    assert (tmp_path / "x.csv").read_text() == "既存"


def test_partial_writer_flushes_each_row(tmp_path):
    """8 章：1 行ごとに flush され、閉じる前でも読める"""
    p = tmp_path / "a_partial.csv"
    w = PartialWriter(p)
    w.append(Sample(START, 0.0, 4.1, 1.0, 4.1, 0.0, 0.0))
    assert p.read_bytes().count(b"\r\n") == 2
    w.close()


def test_format_interval():
    assert recorder.format_interval(1.0) == "1.0"
    assert recorder.format_interval(0.25) == "0.25"
    assert recorder.format_interval(60) == "60.0"


def test_check_writable(tmp_path):
    recorder.check_writable(tmp_path)
    with pytest.raises(OSError):
        recorder.check_writable(tmp_path / "なし")


def test_find_partial_files(tmp_path):
    """一時ファイルは <ベース名>_partial.csv。v1.0.5 までの <ベース名>.partial.csv も見つける"""
    (tmp_path / "a_partial.csv").write_text("x")
    (tmp_path / "b.partial.csv").write_text("x")
    (tmp_path / "c.csv").write_text("x")
    assert recorder.find_partial_files(tmp_path) == [tmp_path / "a_partial.csv", tmp_path / "b.partial.csv"]


def test_full_voltage_tag():
    assert recorder.full_voltage_tag("4.1V") == "4v1"
    assert recorder.full_voltage_tag("4.2V") == "4v2"
    for name in (recorder.output_paths(Path("."), recorder.make_base_name(START, "4.2V", "マクセル")).values()):
        stem = name.name[:-len(".csv")]
        assert "." not in stem and stem == stem.lower()


def test_run_dir_name_and_suffix(tmp_path):
    """出力フォルダは <YYYYMMDD_HHMM>_SDL。同名があれば _SDL_2, _SDL_3 …"""
    assert recorder.run_dir_name(START) == "20261001_1430_SDL"
    first = recorder.create_run_dir(tmp_path, START)
    assert first == tmp_path / "20261001_1430_SDL" and first.is_dir()
    assert recorder.create_run_dir(tmp_path, START) == tmp_path / "20261001_1430_SDL_2"
    (tmp_path / "20261001_1430_SDL_3").write_text("同名のファイル")
    assert recorder.create_run_dir(tmp_path, START) == tmp_path / "20261001_1430_SDL_4"


def test_remove_dir_if_empty(tmp_path):
    empty, used = tmp_path / "a", tmp_path / "b"
    empty.mkdir()
    used.mkdir()
    (used / "x.csv").write_text("x")
    recorder.remove_dir_if_empty(empty)
    recorder.remove_dir_if_empty(used)
    assert not empty.exists() and used.exists()


def test_find_partial_files_in_run_dirs(tmp_path):
    """起動時の検出は出力フォルダ（*_SDL*）の中も見る"""
    run = tmp_path / "20261001_1430_SDL_2"
    run.mkdir()
    (run / "20261001_1430_4v1_pana_partial.csv").write_text("x")
    (run / "20261001_1430_4v1_pana.csv").write_text("x")
    other = tmp_path / "other"
    other.mkdir()
    (other / "z_partial.csv").write_text("x")
    assert recorder.find_partial_files(tmp_path) == [run / "20261001_1430_4v1_pana_partial.csv"]


def test_base_name_with_number():
    """番号を入れると末尾に _no<番号>。未入力なら付けない"""
    assert recorder.make_base_name(START, "4.1V", "Panasonic", "3") == "20261001_1430_4v1_pana_no3"
    assert recorder.make_base_name(START, None, None, "a-12") == "20261001_1430_noa-12"
    assert recorder.make_base_name(START, "4.2V", None, "") == "20261001_1430_4v2"


def test_sanitize_number():
    """英字は小文字に、半角の英数字と - _ 以外は捨てる。最大 20 文字"""
    assert recorder.sanitize_number("A-12_b") == "a-12_b"
    assert recorder.sanitize_number("No.3 あ／x") == "no3x"
    assert recorder.sanitize_number("１２3") == "3"
    assert recorder.sanitize_number("x" * 25) == "x" * 20
