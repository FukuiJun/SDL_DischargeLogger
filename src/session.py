"""放電試験 1 回分の進行（開始 → 周期測定 → 終止判定・停止 → 保存）

GUI と CLI で共通に使う。測定は別スレッドで行い、進行は events（queue.Queue）で知らせる:
    ("sample", Sample)          測定 1 回分
    ("status", str)             状況の文言（終止電圧到達など）
    ("reconnecting", (n, 総数)) 通信が途切れ、n 回目の再接続を試みている
    ("reconnected", None)       再接続して記録を再開した
    ("finished", SessionResult) 終了（保存結果つき）
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import plotting
import recorder
from recorder import (END_REASON_COMM_LOST, END_REASON_CUTOFF, END_REASON_ERROR, END_REASON_MANUAL,
                      END_REASON_WRITE_ERROR, Integrator, PartialWriter, Sample, TestInfo)
from sdl_client import SDLClient, SDLConnectionError, SDLError, SDLResponseError

log = logging.getLogger("sdl.session")

CUTOFF_CONSECUTIVE = 3        # 終止電圧以下が連続この回数で停止
MAX_INVALID_RESPONSES = 10    # 数値でない応答が連続この回数で通信断扱い
RECONNECT_ATTEMPTS = 10       # 放電中の再接続の最大回数
RECONNECT_INTERVAL = 1.0      # 再接続の間隔 [s]


class StartError(Exception):
    """放電を開始できない（メッセージは画面にそのまま出せる文言）"""


@dataclass(frozen=True)
class Conditions:
    maker: str | None
    full_voltage: str | None
    model: str
    current: float
    cutoff: float
    interval: float
    von: float | None = None  # 開始時に SDL に設定する Von [V]（None なら設定しない）
    number: str = ""          # 番号（任意。半角の英数字と - _。ファイル名と CSV に入れる）


@dataclass
class SessionResult:
    end_reason: str
    discarded: bool = False
    pending: bool = False              # 止めたがまだ保存していない（save / discard_pending 待ち）
    csv_path: Path | None = None
    png_path: Path | None = None
    partial_path: Path | None = None   # 保存できなかったときに残した一時ファイル
    error: str | None = None
    save_ok: bool = True               # 直前の保存で CSV を作れたか
    load_off_ok: bool = True
    mah: float = 0.0
    wh: float = 0.0
    messages: list[str] = field(default_factory=list)


class DischargeSession:
    def __init__(self, client: SDLClient, folder: Path, conditions: Conditions, *, note: str = "",
                 reconnect_attempts: int = RECONNECT_ATTEMPTS, reconnect_interval: float = RECONNECT_INTERVAL):
        self.client = client
        self.folder = Path(folder)
        self.cond = conditions
        self.note = note  # 放電中も GUI から更新される。保存時点の内容を CSV に書く
        self.reconnect_attempts = reconnect_attempts
        self.reconnect_interval = reconnect_interval
        self.events: queue.Queue = queue.Queue()
        self.base_name: str | None = None
        self.run_dir: Path | None = None  # 保存先の中に作る出力フォルダ <YYYYMMDD_HHMM>_SDL
        self.paths: dict[str, Path] = {}
        self.start_time: datetime | None = None
        self.start_monotonic: float | None = None
        self.result: SessionResult | None = None
        self.latest: Sample | None = None
        self._integrator = Integrator()
        self._writer: PartialWriter | None = None
        self._measured: str | None = None  # 一時ファイルの内容。保存後も持っておき、もう一度保存するときに使う
        self._von_prev: tuple[float, bool] | None = None  # Von を設定する前の SDL の Von・Latch（終了時に戻す）
        self._stop_event = threading.Event()
        self._stop_save = True
        self._stop_keep = False
        self._end_time: datetime | None = None
        self._thread: threading.Thread | None = None
        self._finished = threading.Event()
        self._data_lock = threading.Lock()
        self._t: list[float] = []
        self._v: list[float] = []
        self._i: list[float] = []

    # ---- 状態 ----
    @property
    def running(self) -> bool:
        return self._thread is not None and not self._finished.is_set()

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    def snapshot(self) -> tuple[list[float], list[float], list[float]]:
        with self._data_lock:
            return list(self._t), list(self._v), list(self._i)

    def title_lines(self) -> list[str]:
        """PNG のタイトル（完了後は 3 行目に結果を入れる）"""
        c, r = self.cond, self.result
        if r is None or r.discarded:
            return plotting.png_title_lines(self.base_name, c.model, c.current, c.cutoff)
        return plotting.png_title_lines(self.base_name, c.model, c.current, c.cutoff, r.mah, r.wh, r.end_reason)

    # ---- 開始 ----
    def start(self) -> None:
        """前提を確かめて負荷を ON にし、最初の測定と一時ファイルの作成まで行ってから測定スレッドを始める"""
        c = self.cond
        try:
            recorder.check_writable(self.folder)
        except OSError as e:
            raise StartError(str(e)) from e
        try:
            voltage = self.client.measure_voltage()
        except SDLError as e:
            raise StartError(f"電圧を取得できません: {e}") from e
        if voltage <= c.cutoff:
            raise StartError(f"電圧が終止電圧以下です（現在 {voltage:.3f} V、終止電圧 {c.cutoff:.3f} V）")

        if c.von is not None:
            try:
                self._von_prev = self.client.von_settings()  # 先に覚えておき、設定に失敗しても戻せるようにする
                self.client.set_von(c.von, latch=False)
            except SDLError as e:
                self._restore_von()
                raise StartError(f"SDL に Von（{c.von:.3f} V）を設定できませんでした: {e}\n"
                                 "［Von を自動設定］のチェックを外すと、Von を設定せずに開始できます") from e
        try:
            self.client.setup_cc(c.current)
            self.client.set_load(True)
            if not self.client.load_state():
                raise StartError("負荷が ON になりません。SDL の状態を確認してください")
            t0 = time.monotonic()
            start_time = datetime.now()
            first = self.client.measure()
        except (SDLError, StartError) as e:
            self.client.load_off_safely()
            self._restore_von()
            if isinstance(e, StartError):
                raise
            raise StartError(f"放電を開始できません: {e}") from e

        self.start_monotonic = t0
        self.start_time = start_time
        try:
            self.run_dir = recorder.create_run_dir(self.folder, start_time)
        except OSError as e:
            self.client.load_off_safely()
            self._restore_von()
            raise StartError(f"保存先にフォルダを作れません: {self.folder}（{e}）") from e
        base = recorder.make_base_name(start_time, c.full_voltage, c.maker, c.number)
        self.base_name = recorder.unique_base_name(self.run_dir, base)
        self.paths = recorder.output_paths(self.run_dir, self.base_name)
        try:
            self._writer = PartialWriter(self.paths["partial"])
            self._record(start_time, 0.0, first)
        except OSError as e:
            self.client.load_off_safely()
            if self._writer is not None:
                self._writer.close()
                _unlink(self.paths["partial"])
            recorder.remove_dir_if_empty(self.run_dir)
            self._restore_von()
            raise StartError(f"一時ファイルを作れません: {self.paths['partial']}（{e}）") from e

        log.info("放電開始 %s 電流 %.3fA 終止 %.3fV 周期 %gs 開始電圧 %.4fV", self.base_name, c.current,
                 c.cutoff, c.interval, voltage)
        self._thread = threading.Thread(target=self._run, name="discharge", daemon=True)
        self._thread.start()

    # ---- 停止 ----
    def request_stop(self, save: bool = True, *, keep: bool = False) -> None:
        """停止を依頼する。結果は finished イベントで届く。

        save=True: 停止して保存 / save=False: 停止して破棄 /
        keep=True: 停止だけして一時ファイルを残す（後で save か discard_pending）
        """
        self._stop_save = save
        self._stop_keep = keep
        self._stop_event.set()

    @property
    def can_save(self) -> bool:
        """止めたあと［CSV保存］できるデータがある（保存待ち・保存済み・保存に失敗して一時ファイルが残っている）"""
        r = self.result
        if r is None or r.discarded:
            return False
        return r.pending or r.csv_path is not None or self._measured is not None or self.paths["partial"].exists()

    def save(self) -> SessionResult:
        """止めたデータを保存する（CSV と PNG）。備考は呼んだ時点の self.note。

        まだ保存していなければ <ベース名>.csv、保存済みなら上書きせずに <ベース名>_2.csv, _3 … として何度でも保存する。
        """
        result = self.result
        if not self.can_save:
            raise RuntimeError("保存できるデータがありません")
        result.error = None  # 前回の保存の失敗はやり直すので消す
        if result.csv_path is None:
            result.partial_path = None
            self._save(result, self._end_time)
            if result.save_ok:
                result.pending = False
        else:
            self._save(result, self._end_time, recorder.unique_base_name(self.run_dir, self.base_name))
        return result

    def discard_pending(self) -> None:
        """keep で止めたデータを破棄する（一時ファイルを消す）"""
        result = self.result
        if result is None or not result.pending:
            return
        _unlink(self.paths["partial"])
        recorder.remove_dir_if_empty(self.run_dir)
        result.pending = False
        result.discarded = True
        log.info("保存せずに破棄 %s", self.base_name)

    def wait(self, timeout: float | None = None) -> SessionResult | None:
        self._finished.wait(timeout)
        return self.result

    # ---- 測定スレッド ----
    def _record(self, timestamp: datetime, elapsed: float, m) -> Sample:
        mah, wh = self._integrator.add(elapsed, m.current, m.power)
        sample = Sample(timestamp, elapsed, m.voltage, m.current, m.power, mah, wh,
                        m.voltage_text, m.current_text, m.power_text)
        self._writer.append(sample)  # OSError は呼び出し側で処理
        with self._data_lock:
            self._t.append(elapsed)
            self._v.append(m.voltage)
            self._i.append(m.current)
        self.latest = sample
        self.events.put(("sample", sample))
        return sample

    def _status(self, text: str) -> None:
        self.events.put(("status", text))

    def _run(self) -> None:
        try:
            self._loop()
        except Exception as e:  # noqa: BLE001 - 想定外のエラーでも負荷 OFF と保存を試みる
            log.exception("測定スレッドで想定外のエラー")
            if not self._finished.is_set():
                self._finish(END_REASON_ERROR, save=True, error=f"想定外のエラーで停止しました: {e}")

    def _loop(self) -> None:
        c = self.cond
        t0 = self.start_monotonic
        below = 1 if self.latest.voltage <= c.cutoff else 0
        invalid = 0
        next_t = t0 + c.interval
        while True:
            if below >= CUTOFF_CONSECUTIVE:
                self._status("終止電圧に到達しました")
                self._finish(END_REASON_CUTOFF, save=True)
                return
            delay = next_t - time.monotonic()
            if self._stop_event.wait(max(delay, 0)):
                self._finish(END_REASON_MANUAL, save=self._stop_save, keep=self._stop_keep)
                return
            now = time.monotonic()
            timestamp = datetime.now()
            try:
                m = self.client.measure()
            except SDLResponseError as e:
                invalid += 1
                log.warning("数値でない応答（%d 回連続）: %s", invalid, e)
                if invalid >= MAX_INVALID_RESPONSES:
                    self._finish(END_REASON_COMM_LOST, save=True,
                                 error=f"数値でない応答が {MAX_INVALID_RESPONSES} 回続いたため停止しました")
                    return
                next_t = _next_tick(next_t, c.interval)
                continue
            except SDLConnectionError as e:
                log.warning("放電中に通信エラー: %s", e)
                outcome = self._reconnect()
                if outcome == "stopped":
                    self._finish(END_REASON_MANUAL, save=self._stop_save, keep=self._stop_keep)
                    return
                if outcome != "ok":
                    self._finish(END_REASON_COMM_LOST, save=True, error=outcome)
                    return
                next_t = _next_tick(next_t, c.interval)
                continue
            invalid = 0
            try:
                self._record(timestamp, now - t0, m)
            except OSError as e:
                log.error("一時ファイルに書き込めません: %s", e)
                self._finish(END_REASON_WRITE_ERROR, save=True,
                             error=f"保存先に書き込めなくなったため停止しました（{e}）")
                return
            below = below + 1 if m.voltage <= c.cutoff else 0
            next_t = _next_tick(next_t, c.interval)

    def _reconnect(self) -> str:
        """'ok'（復帰）/ 'stopped'（再接続中に停止の依頼）/ それ以外は失敗理由"""
        n = self.reconnect_attempts
        for attempt in range(1, n + 1):
            self.events.put(("reconnecting", (attempt, n)))
            if self._stop_event.wait(self.reconnect_interval):
                return "stopped"
            try:
                self.client.reconnect()
                on = self.client.load_state()
            except SDLError as e:
                log.warning("再接続失敗（%d/%d）: %s", attempt, n, e)
                continue
            if not on:
                # SDL が再起動した等。負荷 OFF のまま記録を続けると終止電圧に達しないため止める
                log.error("再接続後に負荷が OFF になっていました")
                return "再接続後、SDL の負荷が OFF になっていたため停止しました"
            log.info("再接続しました（%d/%d）", attempt, n)
            self.events.put(("reconnected", None))
            return "ok"
        return f"{n} 回再接続を試みましたが復帰しませんでした"

    def _finish(self, reason: str, save: bool, error: str | None = None, keep: bool = False) -> None:
        load_off_ok = self.client.load_off_safely()
        if load_off_ok:
            self._restore_von()
        if self._writer is not None:
            self._writer.close()
        end_time = datetime.now()
        self._end_time = end_time
        result = SessionResult(end_reason=reason, discarded=not save and not keep, pending=keep,
                               load_off_ok=load_off_ok, mah=self._integrator.mah, wh=self._integrator.wh)
        if error:
            result.messages.append(error)
        if not load_off_ok:
            result.messages.append("負荷 OFF を送信できませんでした。SDL 本体で負荷を OFF にしてください")
        log.info("放電終了 %s 理由: %s %s 容量 %.1fmAh %.3fWh 負荷OFF:%s", self.base_name, reason,
                 "保存待ち" if keep else "保存" if save else "破棄", result.mah, result.wh,
                 "成功" if load_off_ok else "失敗")
        if keep:
            pass  # 一時ファイルを残し、save / discard_pending を待つ
        elif save:
            self._save(result, end_time)
        else:
            _unlink(self.paths["partial"])
            recorder.remove_dir_if_empty(self.run_dir)
        self.result = result
        self._finished.set()
        self.events.put(("finished", result))

    def _restore_von(self) -> None:
        """開始時に変えた SDL の Von・Latch を元に戻す（負荷 OFF のあと。できなくても続ける）"""
        if self._von_prev is None:
            return
        von, latch = self._von_prev
        self._von_prev = None
        try:
            self.client.set_von(von, latch)
        except SDLError as e:
            log.warning("Von を元に戻せませんでした: %s", e)

    def _save(self, result: SessionResult, end_time: datetime, base: str | None = None) -> None:
        """CSV と PNG を保存する。base を渡すとその名前で（もう一度保存するとき）"""
        c = self.cond
        paths = self.paths if base is None else recorder.output_paths(self.run_dir, base)
        info = TestInfo(start=self.start_time, end=end_time, end_reason=result.end_reason, maker=c.maker,
                        full_voltage=c.full_voltage, model=c.model, current=c.current, cutoff=c.cutoff,
                        mah=self._integrator.mah, wh=self._integrator.wh, interval=c.interval,
                        idn=self.client.idn, note=self.note, number=c.number)
        partial = self.paths["partial"]
        try:
            if self._measured is None:
                self._measured = recorder.read_measured(partial)
            recorder.write_final_csv(paths["csv"], info, self._measured)
        except OSError as e:
            log.error("最終 CSV を作れません %s: %s", paths["csv"], e)
            result.save_ok = False
            if partial.exists():
                result.partial_path = partial
                result.error = f"保存できませんでした。一時ファイル: {partial}"
            else:
                result.error = f"保存できませんでした: {paths['csv']}（{e}）"
            return
        result.save_ok = True
        result.csv_path = paths["csv"]
        try:
            t, v, i = self.snapshot()
            lines = plotting.png_title_lines(paths["csv"].stem, c.model, c.current, c.cutoff,
                                             result.mah, result.wh, result.end_reason)
            plotting.render_png(paths["png"], t, v, i, c.cutoff, c.current, lines)
            result.png_path = paths["png"]
        except Exception as e:  # noqa: BLE001 - グラフ画像の失敗で CSV を失わない
            log.exception("グラフ画像を保存できません")
            result.error = f"グラフ画像を保存できませんでした（{e}）"
        _unlink(partial)
        log.info("保存 %s", result.csv_path)


def _next_tick(next_t: float, interval: float) -> float:
    """次の測定時刻。処理が遅れて過ぎてしまった周期は飛ばす"""
    next_t += interval
    now = time.monotonic()
    if next_t <= now:
        next_t += ((now - next_t) // interval + 1) * interval
    return next_t


def _unlink(path: Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("一時ファイルを削除できません %s: %s", path, e)
