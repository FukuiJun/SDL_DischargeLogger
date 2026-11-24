"""積算（放電容量・電力量）、ファイル名、一時ファイル・最終 CSV の作成"""

from __future__ import annotations

import csv
import io
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

# 画面の選択肢 → ファイル名に使う略称
MAKERS = ["Panasonic", "マクセル", "Spard"]
MAKER_ABBR = {"Panasonic": "pa", "マクセル": "ma", "Spard": "sp"}
FULL_VOLTAGES = ["4.1V", "4.2V"]

# 放電モード。CC: 電流を一定に（定電流）、CP: 電力を一定に（定電力）、
# 連続放電: CC で終止電圧まで放電 → 負荷 OFF で待機 → 電流に倍率をかけて放電 … を設定した回数くり返す
MODE_CC = "CC"
MODE_CP = "CP"
MODE_SERIES = "SERIES"
MODES = [MODE_CC, MODE_CP, MODE_SERIES]
SERIES_NAME = "連続放電"
MODE_NAMES = {MODE_CC: "CC", MODE_CP: "CP", MODE_SERIES: SERIES_NAME}  # CSV の「放電モード」

CSV_ENCODING = "utf-8-sig"  # UTF-8 BOM 付き（Excel で文字化けしない）
CSV_NEWLINE = "\r\n"
COLUMNS = ["日時", "経過時間[s]", "電圧[V]", "電流[A]", "電力[W]", "放電容量[mAh]", "電力量[Wh]", "区間"]
# ファイル名にはドットを使わない（拡張子の . を除く）。大文字は SDL・CP と単位（mA・mV・mW）だけ
PARTIAL_SUFFIX = "_partial.csv"
LEGACY_PARTIAL_SUFFIX = ".partial.csv"  # v1.0.5 までの一時ファイル（起動時の検出だけに使う）
RUN_DIR_SUFFIX = "_SDL"  # 放電 1 回分の出力をまとめるフォルダ <YYYYMMDD_HHMM>_SDL_…（make_base_name）
# 放電開始前に確保されている必要がある空き容量（24 時間分の CSV が約 7MB）
MIN_FREE_BYTES = 20 * 1024 * 1024

END_REASON_CUTOFF = "終止電圧到達"
END_REASON_MANUAL = "手動停止"
END_REASON_COMM_LOST = "通信断"
END_REASON_WRITE_ERROR = "保存先書き込みエラー"
END_REASON_ERROR = "異常終了"

# 測定の区間（CSV の「区間」列）。前休止・後休止は負荷 OFF のまま電圧だけを記録する（オフセット）
PHASE_PRE = "前休止"
PHASE_DISCHARGE = "放電"
PHASE_POST = "後休止"
PHASE_WAIT = "待機"   # 連続放電で、終止電圧に達してから次の回の放電までの負荷 OFF


# ---- ファイル名 ----
NUMBER_MAX_LEN = 20
NUMBER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")


def sanitize_number(text: str) -> str:
    """番号の入力を、ファイル名に使える形にする（英字は小文字に、半角の英数字と - _ 以外は捨てる、最大 20 文字）"""
    return "".join(ch for ch in text.lower() if ch in NUMBER_CHARS)[:NUMBER_MAX_LEN]


def make_base_name(start: datetime, mode: str, setpoint: float | None, cutoff: float | None,
                   maker: str | None = None) -> str:
    """出力フォルダとファイルの名前（同じ名前にする）。
    CC: <YYYYMMDD_HHMM>_SDL_<放電電流 mA>mA_<終止電圧 mV>mV[_<メーカー略称>]
    CP: <YYYYMMDD_HHMM>_SDL_CP_<放電電力 mW>mW_<終止電圧 mV>mV[_<メーカー略称>]
    連続放電: CC と同じ（電流は初期電流）で、一番後ろに _連続放電
    メーカーが未選択なら _ ごと省く。setpoint は CC・連続放電なら放電電流 [A]、CP なら放電電力 [W]"""
    return name_from(start.strftime("%Y%m%d_%H%M"), mode, setpoint, cutoff, maker)


def name_from(stamp: str, mode: str, setpoint: float | None, cutoff: float | None, maker: str | None) -> str:
    """make_base_name の日時の部分を文字列で渡す形（待機中の表示では YYYYMMDD_HHMM）。値が未入力なら ?"""
    parts = [stamp + RUN_DIR_SUFFIX]
    if mode == MODE_CP:
        parts += ["CP", _milli(setpoint, "mW")]
    else:
        parts.append(_milli(setpoint, "mA"))
    parts.append(_milli(cutoff, "mV"))
    if maker:
        parts.append(MAKER_ABBR[maker])
    if mode == MODE_SERIES:
        parts.append(SERIES_NAME)
    return "_".join(parts)


def file_base_name(run_dir_name: str, number: str | None = None) -> str:
    """出力フォルダの中の CSV・PNG・一時ファイルの名前。フォルダ名に、番号を入れたときだけ _no<番号> を付ける
    （番号はファイル名だけに入れ、フォルダ名には入れない）"""
    return f"{run_dir_name}_no{number}" if number else run_dir_name


def _milli(value: float | None, unit: str) -> str:
    """0.400 → '400mA' のように 1000 倍した整数に単位を付ける（入力は小数 3 桁まで）"""
    return f"{round(value * 1000)}{unit}" if value is not None else f"?{unit}"


# ---- 連続放電 ----
SERIES_MIN_CURRENT = 0.001   # 最後の回の電流の下限 [A]
CURRENT_STEP = 0.0001      # 倍率をかけた電流を丸める単位 [A]（0.1 mA）
DEFAULT_RATIO = 0.5        # 倍率の初期値（半分ずつ）


def series_currents(initial: float, ratio: float, count: int) -> list[float]:
    """各回の電流 [A]。初期電流に回ごとに倍率をかける（0.1 mA 単位に四捨五入）。
    例: 0.4 A・倍率 0.5・4 回 → [0.4, 0.2, 0.1, 0.05]、倍率 1 なら毎回同じ電流"""
    step, base, r = Decimal(str(CURRENT_STEP)), Decimal(str(initial)), Decimal(str(ratio))
    return [float((base * r ** k).quantize(step, rounding=ROUND_HALF_UP)) for k in range(count)]


def format_ratio(value: float) -> str:
    """倍率の表示。0.5 → '0.5'、0.75 → '0.75'、1.0 → '1'"""
    return f"{value:g}"


def format_current(value: float) -> str:
    """電流の表示。小数 3 桁、0.1 mA の桁があるときだけ 4 桁（0.050 / 0.0125）"""
    text = f"{value:.4f}"
    return text[:-1] if text.endswith("0") else text


def output_paths(folder: Path, base: str) -> dict[str, Path]:
    return {"csv": folder / f"{base}.csv", "png": folder / f"{base}.png",
            "partial": folder / f"{base}{PARTIAL_SUFFIX}"}


def unique_base_name(folder: Path, base: str) -> str:
    """CSV・PNG・一時ファイルのどれかが既にあれば末尾に _2, _3 … を付ける"""
    candidate, n = base, 1
    while any(p.exists() for p in output_paths(folder, candidate).values()):
        n += 1
        candidate = f"{base}_{n}"
    return candidate


def create_run_dir(folder: Path, name: str) -> Path:
    """保存先の中に出力フォルダ（名前は make_base_name）を作る。同名のフォルダ（またはファイル）があれば _2, _3 … を付ける"""
    folder = Path(folder)
    candidate, n = name, 1
    while True:
        path = folder / candidate
        try:
            path.mkdir()
            return path
        except FileExistsError:
            n += 1
            candidate = f"{name}_{n}"


def remove_dir_if_empty(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.rmdir()  # 空でなければ OSError で何もしない
    except OSError:
        pass


def find_partial_files(folder: Path) -> list[Path]:
    """保存先と、その中の出力フォルダ（*_SDL*）にある一時ファイル"""
    folder = Path(folder)
    found: set[Path] = set()
    try:
        for pattern in (f"*{PARTIAL_SUFFIX}", f"*{LEGACY_PARTIAL_SUFFIX}", f"*{RUN_DIR_SUFFIX}*/*{PARTIAL_SUFFIX}"):
            found.update(p for p in folder.glob(pattern) if p.is_file())
    except OSError:
        return []
    return sorted(found)


def check_writable(folder: Path) -> None:
    """保存先に書き込めるか・空き容量があるかを確かめる。だめなら OSError（メッセージ付き）"""
    folder = Path(folder)
    if not folder.is_dir():
        raise OSError(f"保存先フォルダがありません: {folder}")
    test = folder / f".sdl_logger_write_test_{os.getpid()}.tmp"
    try:
        with open(test, "wb") as fp:
            fp.write(b"test")
    except OSError as e:
        raise OSError(f"保存先に書き込めません: {folder}（{e.strerror or e}）") from e
    finally:
        try:
            test.unlink()
        except OSError:
            pass
    if shutil.disk_usage(folder).free < MIN_FREE_BYTES:
        raise OSError(f"保存先の空き容量が不足しています: {folder}")


# ---- 積算 ----
class Integrator:
    """電流・電力の台形積分（mAh・Wh）"""

    def __init__(self) -> None:
        self.mah = 0.0
        self.wh = 0.0
        self._last: tuple[float, float, float] | None = None

    def add(self, elapsed: float, current: float, power: float) -> tuple[float, float]:
        if self._last is not None:
            t0, i0, p0 = self._last
            dt = elapsed - t0
            if dt > 0:
                self.mah += (i0 + current) / 2 * dt / 3.6
                self.wh += (p0 + power) / 2 * dt / 3600
        self._last = (elapsed, current, power)
        return self.mah, self.wh

    def restart(self) -> None:
        """次の add を区切りにする（連続放電の待機をはさんだ放電どうしを、つないで積算しない）"""
        self._last = None


# ---- 測定行 ----
@dataclass(frozen=True)
class Sample:
    timestamp: datetime
    elapsed: float
    voltage: float
    current: float
    power: float
    mah: float
    wh: float
    voltage_text: str = ""
    current_text: str = ""
    power_text: str = ""
    phase: str = ""  # 区間（PHASE_PRE / PHASE_DISCHARGE / PHASE_WAIT / PHASE_POST）
    step: int = 0    # 連続放電の何回目か（放電1・待機1 … と書く）。0 なら区間だけ

    def row(self) -> list[str]:
        return [
            format_datetime_ms(self.timestamp),
            f"{self.elapsed:.3f}",
            self.voltage_text or f"{self.voltage:.5f}",
            self.current_text or f"{self.current:.4f}",
            self.power_text or f"{self.power:.4f}",
            f"{self.mah:.3f}",
            f"{self.wh:.4f}",
            f"{self.phase}{self.step}" if self.step else self.phase,
        ]


def format_datetime(dt: datetime) -> str:
    """試験情報の開始・終了日時（分まで）"""
    return dt.strftime("%Y/%m/%d %H:%M")


def format_datetime_ms(dt: datetime) -> str:
    return dt.strftime("%Y/%m/%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def _csv_line(values: list[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator=CSV_NEWLINE).writerow(values)
    return buf.getvalue()


class PartialWriter:
    """一時ファイル（<ベース名>_partial.csv）に測定行を 1 行ずつ追記する（毎行 flush）"""

    def __init__(self, path: Path):
        self.path = Path(path)
        # 新規作成のみ（既存ファイルは上書きしない）
        self._fp = open(self.path, "x", encoding=CSV_ENCODING, newline="")
        self._write(_csv_line(COLUMNS))

    def _write(self, text: str) -> None:
        self._fp.write(text)
        self._fp.flush()
        try:
            os.fsync(self._fp.fileno())
        except OSError:
            pass

    def append(self, sample: Sample) -> None:
        self._write(_csv_line(sample.row()))

    def close(self) -> None:
        if not self._fp.closed:
            try:
                self._fp.close()
            except OSError:
                pass

    @property
    def closed(self) -> bool:
        return self._fp.closed


# ---- 最終 CSV ----
@dataclass
class TestInfo:
    __test__ = False  # pytest にテストクラスと見なされないように

    start: datetime
    end: datetime
    end_reason: str
    maker: str | None
    full_voltage: str | None  # "4.1V" など
    model: str
    current: float | None     # 放電電流 [A]（CP モードのときは None）
    cutoff: float
    mah: float
    wh: float
    interval: float
    idn: str
    note: str
    number: str = ""
    pre_rest: float = 0.0               # オフセット（前休止・後休止）[s]
    post_rest: float = 0.0
    pre_voltage: float | None = None    # 放電前電圧（前休止の最後）[V]
    post_voltage: float | None = None   # 放電後電圧（後休止の最後）[V]
    mode: str = MODE_CC                 # 放電モード（CC / CP / 連続放電）
    power: float | None = None          # 放電電力 [W]（CP モードのとき）
    ratio: float | None = None          # 連続放電で、次の回の電流にかける倍率
    wait: float | None = None           # 連続放電の待機 [s]
    count: int | None = None            # 連続放電の放電回数
    step_currents: tuple[float, ...] = ()  # 連続放電で放電した各回の電流 [A]
    step_mah: tuple[float, ...] = ()       # 同じく各回の放電容量 [mAh]


def format_interval(value: float) -> str:
    """1.0 → '1.0'、0.25 → '0.25'（小数 1 桁以上・余分な 0 なし）"""
    text = f"{value:.3f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def format_seconds(value: float) -> str:
    """オフセットの秒数。10.0 → '10'、0.5 → '0.5'"""
    return f"{value:g}"


def info_lines(info: TestInfo) -> str:
    full = info.full_voltage[:-1] if info.full_voltage and info.full_voltage.endswith("V") else (info.full_voltage or "")
    rows = [
        ["試験情報"],
        ["開始日時", format_datetime(info.start)],
        ["終了日時", format_datetime(info.end)],
        ["終了理由", info.end_reason],
        ["メーカー", info.maker or ""],
        ["満充電電圧[V]", full],
        ["型番", info.model or ""],
        ["番号", info.number or ""],
        ["放電モード", MODE_NAMES.get(info.mode, info.mode)],
        # 使わない設定値は空欄にする（行の並びはモードによらず同じ）。連続放電の放電電流は初期電流
        ["放電電流[A]", f"{info.current:.3f}" if info.current is not None else ""],
        ["放電電力[W]", f"{info.power:.3f}" if info.power is not None else ""],
        ["電流の倍率", format_ratio(info.ratio) if info.ratio is not None else ""],
        ["待機[s]", format_seconds(info.wait) if info.wait is not None else ""],
        ["放電回数", str(info.count) if info.count is not None else ""],
        ["各回の電流[A]"] + [format_current(i) for i in info.step_currents],
        ["各回の放電容量[mAh]"] + [f"{m:.1f}" for m in info.step_mah],
        ["終止電圧[V]", f"{info.cutoff:.3f}"],
        ["放電容量[mAh]", f"{info.mah:.1f}"],
        ["電力量[Wh]", f"{info.wh:.3f}"],
        ["取得周期[s]", format_interval(info.interval)],
        ["オフセット 放電前[s]", format_seconds(info.pre_rest)],
        ["オフセット 放電後[s]", format_seconds(info.post_rest)],
        ["放電前電圧[V]", f"{info.pre_voltage:.3f}" if info.pre_voltage is not None else ""],
        ["放電後電圧[V]", f"{info.post_voltage:.3f}" if info.post_voltage is not None else ""],
        ["機器", info.idn],
    ]
    text = "".join(_csv_line(r) for r in rows)
    # 備考は改行・カンマを含んでよい。常にダブルクォートで囲む（中の " は "" にする）
    text += '備考,"' + (info.note or "").replace('"', '""') + '"' + CSV_NEWLINE
    return text


def read_measured(partial_path: Path) -> str:
    """一時ファイルの内容（列見出し＋測定行）"""
    with open(partial_path, "r", encoding=CSV_ENCODING, newline="") as fp:
        return fp.read()


def write_final_csv(path: Path, info: TestInfo, measured: str) -> None:
    """試験情報ブロック＋空行＋測定データ（read_measured の内容）で最終 CSV を作る。

    同名のファイルがあれば上書きせずに FileExistsError。途中で失敗したら作りかけの CSV は消す。
    """
    path = Path(path)
    try:
        with open(path, "x", encoding=CSV_ENCODING, newline="") as fp:
            fp.write(info_lines(info))
            fp.write(CSV_NEWLINE)
            fp.write(measured)
            fp.flush()
            os.fsync(fp.fileno())
    except FileExistsError:
        raise
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
        raise
