"""Siglent SDL1020X-E との通信・制御（LAN / TCP ソケット、SCPI テキスト）

Python 標準の socket のみを使う（NI-VISA 等は不要）。
1 コマンドあたりのタイムアウトは 3 秒。応答なし・切断のときはソケットを閉じて
SDLConnectionError を送出する（遅れて届いた応答で以降の応答がずれるのを防ぐため）。
再接続は呼び出し側が reconnect() で行う。
"""

from __future__ import annotations

import logging
import math
import socket
import threading
import weakref
from dataclasses import dataclass

log = logging.getLogger("sdl.client")

DEFAULT_PORT = 5025
DEFAULT_TIMEOUT = 3.0
# 放電電流がこの値以下なら 5A レンジ、超えるなら 30A レンジ
LOW_RANGE_MAX = 5.0

CONNECT_ERROR_MESSAGE = "接続できません。IP アドレスと LAN ケーブルを確認してください"


class SDLError(Exception):
    """SDL との通信で起きたエラーの基底クラス"""


class SDLConnectionError(SDLError):
    """応答なし（タイムアウト）・切断・未接続"""


class SDLResponseError(SDLError):
    """数値でない応答など、想定外の応答"""


@dataclass(frozen=True)
class Measurement:
    """1 回分の測定値。*_text は SDL の応答文字列そのまま（CSV にはこちらを書く）"""

    voltage: float
    current: float
    power: float
    voltage_text: str
    current_text: str
    power_text: str


def parse_float(text: str) -> float:
    """SDL の数値応答を float にする。数値でない・有限でないときは SDLResponseError"""
    try:
        value = float(text)
    except ValueError:
        raise SDLResponseError(f"数値でない応答: {text!r}") from None
    if not math.isfinite(value):
        raise SDLResponseError(f"数値でない応答: {text!r}")
    return value


def parse_state(text: str) -> bool:
    """負荷状態の応答（1/0、ON/OFF）を bool にする"""
    t = text.strip().upper()
    if t in ("1", "ON"):
        return True
    if t in ("0", "OFF"):
        return False
    raise SDLResponseError(f"負荷状態の応答が不正です: {text!r}")


def current_range_for(current: float) -> int:
    return 5 if current <= LOW_RANGE_MAX else 30


# emergency_load_off_all() で負荷 OFF を送る対象（アプリが例外で終了するときに使う）
_clients: "weakref.WeakSet[SDLClient]" = weakref.WeakSet()


class SDLClient:
    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT):
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self.idn = ""
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        self._lock = threading.RLock()
        # 負荷 ON を送った後、OFF を送るまで True（緊急停止の要否の判断に使う）
        self.load_requested = False
        _clients.add(self)

    # ---- 接続 ----
    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> str:
        """接続して *IDN? を確認する。応答に SDL を含めば IDN 文字列を返す"""
        with self._lock:
            self.close()
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            except OSError as e:
                log.warning("接続失敗 %s:%s: %s", self.host, self.port, e)
                raise SDLConnectionError(CONNECT_ERROR_MESSAGE) from e
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock = sock
            self._buf.clear()
            try:
                idn = self.query("*IDN?")
            except SDLConnectionError as e:
                raise SDLConnectionError(CONNECT_ERROR_MESSAGE) from e
            if "SDL" not in idn.upper():
                self.close()
                raise SDLResponseError(f"SDL ではない機器が応答しました: {idn}")
            self.idn = idn
            log.info("接続 %s:%s %s", self.host, self.port, idn)
            return idn

    def reconnect(self) -> str:
        return self.connect()

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            self._buf.clear()
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    # ---- 送受信 ----
    def _require_sock(self) -> socket.socket:
        if self._sock is None:
            raise SDLConnectionError("未接続です")
        return self._sock

    def _broken(self, command: str, exc: BaseException) -> SDLConnectionError:
        log.warning("通信エラー（%s）: %s", command, exc)
        self.close()
        return SDLConnectionError(f"応答がありません（{command}）")

    def _drain(self, sock: socket.socket) -> None:
        """前のコマンドの余分な応答が残っていれば捨てる"""
        self._buf.clear()
        try:
            sock.setblocking(False)
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                log.warning("余分な受信データを破棄: %r", data[:80])
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            pass
        finally:
            try:
                sock.settimeout(self.timeout)
            except OSError:
                pass

    def write(self, command: str) -> None:
        with self._lock:
            sock = self._require_sock()
            try:
                sock.settimeout(self.timeout)
                sock.sendall((command + "\n").encode("ascii"))
            except OSError as e:
                raise self._broken(command, e) from e

    def query(self, command: str) -> str:
        with self._lock:
            sock = self._require_sock()
            self._drain(sock)
            self.write(command)
            sock = self._require_sock()
            try:
                while b"\n" not in self._buf:
                    data = sock.recv(4096)
                    if not data:
                        raise ConnectionError("相手が接続を閉じました")
                    self._buf += data
            except OSError as e:
                raise self._broken(command, e) from e
            line, _, rest = bytes(self._buf).partition(b"\n")
            self._buf = bytearray(rest)
            return line.decode("ascii", errors="replace").strip()

    def query_float(self, command: str) -> tuple[float, str]:
        text = self.query(command)
        return parse_float(text), text

    # ---- 測定 ----
    def measure_voltage(self) -> float:
        return self.query_float("MEAS:VOLT?")[0]

    def measure(self) -> Measurement:
        with self._lock:
            v, vt = self.query_float("MEAS:VOLT?")
            i, it = self.query_float("MEAS:CURR?")
            p, pt = self.query_float("MEAS:POW?")
        return Measurement(v, i, p, vt, it, pt)

    # ---- 制御 ----
    def setup_cc(self, current: float) -> None:
        """CC モード・電流レンジ・電流値を設定する（負荷は ON にしない）"""
        with self._lock:
            self.write(":SOUR:FUNC CURR")
            self.write(f":SOUR:CURR:IRANG {current_range_for(current)}")
            self.write(f":SOUR:CURR:LEV:IMM {current:.3f}")

    def set_load(self, on: bool) -> None:
        with self._lock:
            if on:
                self.load_requested = True
            self.write(f":SOUR:INP:STAT {'ON' if on else 'OFF'}")
            if not on:
                self.load_requested = False
        log.info("負荷 %s を送信", "ON" if on else "OFF")

    def load_state(self) -> bool:
        return parse_state(self.query(":SOUR:INP:STAT?"))

    def von_settings(self) -> tuple[float, bool]:
        """今の Von [V] と Von Latch（ON なら True）を読む"""
        with self._lock:
            von = self.query_float(":SOUR:VOLT:LEV:ON?")[0]
            latch = parse_state(self.query(":SOUR:VOLT:LATC:STAT?"))
        return von, latch

    def set_von(self, volts: float, latch: bool = False) -> None:
        """Von（この電圧より下では電流を流さない）と Von Latch を設定し、読み戻して確かめる。

        Latch OFF にすると、PC が止まっても電池電圧が Von を下回ったところで SDL 自身が電流を止める。
        読み戻した値が違えば SDLResponseError（SPEC Q-02 と同じく実機での確認前のコマンド）
        """
        with self._lock:
            self.write(f":SOUR:VOLT:LEV:ON {volts:.3f}")
            self.write(f":SOUR:VOLT:LATC:STAT {'ON' if latch else 'OFF'}")
            von, now_latch = self.von_settings()
        if abs(von - volts) > 0.005 or now_latch != latch:
            raise SDLResponseError(f"Von が設定どおりになりません（Von {von:.3f} V、Latch {'ON' if now_latch else 'OFF'}）")
        log.info("Von %.3f V / Latch %s を設定", volts, "ON" if latch else "OFF")

    def load_off_safely(self) -> bool:
        """負荷 OFF を送り、負荷状態を読んで OFF になったことを確かめる。

        切断されていれば再接続して送る（最大 2 回）。OFF を確認できたら True
        """
        for attempt in range(2):
            try:
                with self._lock:
                    if not self.connected:
                        self.connect()
                    self.set_load(False)
                    if not self.load_state():
                        return True
                    log.warning("負荷 OFF を送信しましたが、負荷状態が ON のままです")
            except SDLError as e:
                log.warning("負荷 OFF の送信に失敗（%d 回目）: %s", attempt + 1, e)
        return False

    def emergency_load_off(self) -> bool:
        """アプリが異常終了するときの負荷 OFF。ロックが取れなければ新しい接続で送る"""
        if self._lock.acquire(timeout=2.0):
            try:
                return self.load_off_safely()
            finally:
                self._lock.release()
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
                sock.sendall(b":SOUR:INP:STAT OFF\n")
            self.load_requested = False
            log.info("負荷 OFF を新しい接続で送信")
            return True
        except OSError as e:
            log.error("緊急の負荷 OFF に失敗: %s", e)
            return False


def emergency_load_off_all() -> None:
    """負荷 ON のままのクライアントすべてに負荷 OFF を送る"""
    for client in list(_clients):
        if client.load_requested:
            client.emergency_load_off()
