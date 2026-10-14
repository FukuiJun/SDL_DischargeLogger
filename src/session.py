"""放電試験 1 回分の進行（開始 → 周期測定 → 終止判定・停止 → 保存）

GUI と CLI で共通に使う。測定は別スレッドで行い、進行は events（queue.Queue）で知らせる:
    ("sample", Sample)          測定 1 回分
    ("status", str)             状況の文言（終止電圧到達など）
    ("reconnecting", (n, 総数)) 通信が途切れ、n 回目の再接続を試みている
    ("reconnected", None)       再接続して記録を再開した
    ("phase", 区間)             区間が変わった（前休止 → 放電 → 後休止）
    ("finished", SessionResult) 終了（保存結果つき）

オフセット（Conditions.pre_rest / post_rest）を入れると、負荷 OFF のまま電圧だけを記録する区間を
放電の前（前休止）と、終止電圧で負荷を OFF にしたあと（後休止）に置く。経過時間の 0 は測定開始。
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
                      END_REASON_WRITE_ERROR, PHASE_DISCHARGE, PHASE_POST, PHASE_PRE, Integrator, PartialWriter,
                      Sample, TestInfo)
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
    pre_rest: float = 0.0     # オフセット：放電の前に負荷 OFF のまま電圧を記録する時間 [s]（0 なら無し）
    post_rest: float = 0.0    # オフセット：終止電圧で負荷を OFF にしたあと電圧を記録する時間 [s]（0 なら無し）


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
    stopped_by_user: bool = False      # 後休止を［ON/OFF］で途中で止めた（終了理由は終止電圧到達のまま）
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
        self.phase: str | None = None             # いまの区間（PHASE_PRE / PHASE_DISCHARGE / PHASE_POST）
        self.phase_deadline: float | None = None  # 前休止・後休止が終わる時刻（time.monotonic）
        self.load_on_elapsed: float | None = None   # 前休止のあと負荷を ON にした経過時間 [s]
        self.load_off_elapsed: float | None = None  # 後休止の前に負荷を OFF にした経過時間 [s]
        self.pre_voltage: float | None = None   # 放電前電圧（前休止の最後の電圧）
        self.post_voltage: float | None = None  # 放電後電圧（後休止の最後の電圧）
        self._invalid = 0
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
        """PNG のタイトル（完了後は結果を入れる。オフセットを入れたときは放電前・放電後の電圧も）"""
        c, r = self.cond, self.result
        rest = {"pre_voltage": self.pre_voltage, "post_voltage": self.post_voltage}
        if r is None or r.discarded:
            return plotting.png_title_lines(self.base_name, c.model, c.current, c.cutoff, **rest)
        return plotting.png_title_lines(self.base_name, c.model, c.current, c.cutoff, r.mah, r.wh, r.end_reason,
                                        **rest)

    def markers(self) -> list[float]:
        """グラフに縦線を引く経過時間 [s]（オフセットを入れたとき、負荷を ON・OFF にした時刻）"""
        return [t for t in (self.load_on_elapsed, self.load_off_elapsed) if t is not None]

    def phase_remaining(self) -> float | None:
        """前休止・後休止の残り時間 [s]（放電中・終了後は None）"""
        if self.phase_deadline is None or self.finished:
            return None
        return max(self.phase_deadline - time.monotonic(), 0.0)

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
        pre_rest = c.pre_rest > 0
        try:
            self.client.setup_cc(c.current)
            if not pre_rest:
                self.client.set_load(True)
                if not self.client.load_state():
                    raise StartError("負荷が ON になりません。SDL の状態を確認してください")
            # 前休止があるときは負荷 OFF のまま測り始める（負荷は前休止が終わってから ON にする）
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
        if pre_rest:
            self.phase, self.phase_deadline = PHASE_PRE, t0 + c.pre_rest
        else:
            self.phase = PHASE_DISCHARGE
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

        log.info("%s %s 電流 %.3fA 終止 %.3fV 周期 %gs 開始電圧 %.4fV オフセット 前 %gs 後 %gs",
                 "前休止開始" if pre_rest else "放電開始", self.base_name, c.current, c.cutoff, c.interval, voltage,
                 c.pre_rest, c.post_rest)
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
        phase = self.phase
        if phase == PHASE_DISCHARGE:
            mah, wh = self._integrator.add(elapsed, m.current, m.power)
        else:  # 放電容量・電力量は放電の区間だけで数える
            mah, wh = self._integrator.mah, self._integrator.wh
        sample = Sample(timestamp, elapsed, m.voltage, m.current, m.power, mah, wh,
                        m.voltage_text, m.current_text, m.power_text, phase)
        self._writer.append(sample)  # OSError は呼び出し側で処理
        if phase == PHASE_PRE:
            self.pre_voltage = m.voltage
        elif phase == PHASE_POST:
            self.post_voltage = m.voltage
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
        below = 1 if self.phase == PHASE_DISCHARGE and self.latest.voltage <= c.cutoff else 0
        next_t = self.start_monotonic + c.interval
        while True:
            if self.phase == PHASE_DISCHARGE and below >= CUTOFF_CONSECUTIVE:
                if c.post_rest <= 0:
                    self._status("終止電圧に到達しました")
                    self._finish(END_REASON_CUTOFF, save=True)
                    return
                if not self._begin_post_rest():
                    return
                below = 0
                next_t = time.monotonic() + c.interval
                continue
            wake = next_t if self.phase_deadline is None else min(next_t, self.phase_deadline)
            if self._stop_event.wait(max(wake - time.monotonic(), 0)):
                self._finish_by_request()
                return
            if self.phase_deadline is not None and time.monotonic() >= self.phase_deadline:
                if self.phase == PHASE_PRE:
                    if not self._begin_discharge():
                        return
                    last = self.latest
                    below = 1 if last.phase == PHASE_DISCHARGE and last.voltage <= c.cutoff else 0
                    next_t = time.monotonic() + c.interval
                    continue
                # 後休止の終わり：最後の電圧を記録して終了
                if self._sample() == "done":
                    return
                self._finish(END_REASON_CUTOFF, save=True)
                return
            outcome = self._sample()
            if outcome == "done":
                return
            if outcome == "ok" and self.phase == PHASE_DISCHARGE:
                below = below + 1 if self.latest.voltage <= c.cutoff else 0
            next_t = _next_tick(next_t, c.interval)

    def _sample(self) -> str:
        """1 回測って記録する。'ok' / 'skip'（今回は記録なし。続ける）/ 'done'（終了した）"""
        now = time.monotonic()
        timestamp = datetime.now()
        try:
            m = self.client.measure()
        except SDLResponseError as e:
            self._invalid += 1
            log.warning("数値でない応答（%d 回連続）: %s", self._invalid, e)
            if self._invalid >= MAX_INVALID_RESPONSES:
                self._finish(END_REASON_COMM_LOST, save=True,
                             error=f"数値でない応答が {MAX_INVALID_RESPONSES} 回続いたため停止しました")
                return "done"
            return "skip"
        except SDLConnectionError as e:
            log.warning("測定中に通信エラー: %s", e)
            outcome = self._reconnect()
            if outcome == "stopped":
                self._finish_by_request()
                return "done"
            if outcome != "ok":
                self._finish(END_REASON_COMM_LOST, save=True, error=outcome)
                return "done"
            return "skip"
        self._invalid = 0
        try:
            self._record(timestamp, now - self.start_monotonic, m)
        except OSError as e:
            log.error("一時ファイルに書き込めません: %s", e)
            self._finish(END_REASON_WRITE_ERROR, save=True,
                         error=f"保存先に書き込めなくなったため停止しました（{e}）")
            return "done"
        return "ok"

    def _set_phase(self, phase: str, deadline: float | None) -> None:
        self.phase, self.phase_deadline = phase, deadline
        self.events.put(("phase", phase))

    def _begin_discharge(self) -> bool:
        """前休止の終わり：負荷 ON の直前の電圧を記録してから負荷を ON にし、直後の電圧も記録する。
        終了したら False"""
        if self._sample() == "done":
            return False
        try:
            self.client.set_load(True)
            on = self.client.load_state()
        except SDLError as e:
            self._finish(END_REASON_ERROR, save=True, error=f"負荷を ON にできませんでした: {e}")
            return False
        if not on:
            self._finish(END_REASON_ERROR, save=True, error="負荷が ON になりません。SDL の状態を確認してください")
            return False
        self.load_on_elapsed = time.monotonic() - self.start_monotonic
        self._set_phase(PHASE_DISCHARGE, None)
        log.info("前休止を終えて放電開始（経過 %.1fs、放電前電圧 %.4fV）", self.load_on_elapsed, self.pre_voltage)
        return self._sample() != "done"

    def _begin_post_rest(self) -> bool:
        """終止電圧に到達：負荷を OFF にして後休止に入り、OFF 直後の電圧を記録する。終了したら False"""
        if not self.client.load_off_safely():
            self._status("終止電圧に到達しました")
            self._finish(END_REASON_CUTOFF, save=True)  # 負荷 OFF の失敗は _finish が知らせる
            return False
        self.load_off_elapsed = time.monotonic() - self.start_monotonic
        self._restore_von()
        self._set_phase(PHASE_POST, time.monotonic() + self.cond.post_rest)
        self._status("終止電圧に到達しました。後休止を記録しています")
        log.info("終止電圧に到達。後休止 %gs（経過 %.1fs）", self.cond.post_rest, self.load_off_elapsed)
        return self._sample() != "done"

    def _finish_by_request(self) -> None:
        """停止の依頼で終わる。後休止中なら放電は終止電圧で終わっているので、終止電圧到達として保存する"""
        if self.phase == PHASE_POST and (self._stop_save or self._stop_keep):
            self._finish(END_REASON_CUTOFF, save=True, by_user=True)
        else:
            self._finish(END_REASON_MANUAL, save=self._stop_save, keep=self._stop_keep)

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
            if self.phase == PHASE_DISCHARGE and not on:
                # SDL が再起動した等。負荷 OFF のまま記録を続けると終止電圧に達しないため止める
                log.error("再接続後に負荷が OFF になっていました")
                return "再接続後、SDL の負荷が OFF になっていたため停止しました"
            if self.phase != PHASE_DISCHARGE and on:
                log.warning("休止中なのに負荷が ON になっていたため OFF にします")
                self.client.load_off_safely()
            log.info("再接続しました（%d/%d）", attempt, n)
            self.events.put(("reconnected", None))
            return "ok"
        return f"{n} 回再接続を試みましたが復帰しませんでした"

    def _finish(self, reason: str, save: bool, error: str | None = None, keep: bool = False,
                by_user: bool = False) -> None:
        load_off_ok = self.client.load_off_safely()
        if load_off_ok:
            self._restore_von()
        if self._writer is not None:
            self._writer.close()
        end_time = datetime.now()
        self._end_time = end_time
        self.phase_deadline = None
        result = SessionResult(end_reason=reason, discarded=not save and not keep, pending=keep,
                               load_off_ok=load_off_ok, stopped_by_user=by_user,
                               mah=self._integrator.mah, wh=self._integrator.wh)
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
                        idn=self.client.idn, note=self.note, number=c.number, pre_rest=c.pre_rest,
                        post_rest=c.post_rest, pre_voltage=self.pre_voltage, post_voltage=self.post_voltage)
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
            rest = {"pre_voltage": self.pre_voltage, "post_voltage": self.post_voltage}
            lines = plotting.png_title_lines(paths["csv"].stem, c.model, c.current, c.cutoff,
                                             result.mah, result.wh, result.end_reason, **rest)
            plotting.render_png(paths["png"], t, v, i, c.cutoff, c.current, lines, markers=self.markers())
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
