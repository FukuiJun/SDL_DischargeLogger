"""SDL1020X-E シミュレータ（TCP サーバ、SCPI 応答と放電カーブを模擬）

実機なしでアプリの動作確認・自動テストをするためのもの。

    python tools/fake_sdl.py                  # 127.0.0.1:5025、等倍速
    python tools/fake_sdl.py --speed 120      # 時間を 120 倍速で進める（3000mAh/1A が約 1.5 分）
    python tools/fake_sdl.py --host 0.0.0.0 --capacity 2000 --soc 0.9

アプリの IP を 127.0.0.1 にして接続する。Ctrl+C で終了。

テストからは FakeSDL クラスを直接使う（port=0 で空きポート）。
受信したコマンドは commands に記録され、voltage_override・garbage・silent で異常を模擬できる。
"""

from __future__ import annotations

import argparse
import random
import re
import socket
import threading
import time

IDN = "Siglent Technologies,SDL1020X-E,SDL00000000001,1.1.1.21R2"

# 充電状態（SOC）と開放電圧（OCV）の対応（一般的なリチウムイオン電池の形）
OCV_TABLE = [
    (0.00, 2.50), (0.02, 3.20), (0.05, 3.45), (0.10, 3.60), (0.20, 3.70), (0.30, 3.75),
    (0.40, 3.79), (0.50, 3.82), (0.60, 3.87), (0.70, 3.92), (0.80, 3.98), (0.90, 4.06), (1.00, 4.20),
]

# SCPI のキーワード（長い形）。短い形・途中までの形を長い形に揃えるのに使う
_KEYWORDS = ["MEASURE", "VOLTAGE", "CURRENT", "POWER", "SOURCE", "FUNCTION", "INPUT", "STATE",
             "LEVEL", "IMMEDIATE", "IRANGE", "DC", "LATCH"]
_SHORT = {"MEASURE": "MEAS", "VOLTAGE": "VOLT", "CURRENT": "CURR", "POWER": "POW", "SOURCE": "SOUR",
          "FUNCTION": "FUNC", "INPUT": "INP", "STATE": "STAT", "LEVEL": "LEV", "IMMEDIATE": "IMM",
          "IRANGE": "IRANG", "DC": "DC", "LATCH": "LATC"}


def ocv(soc: float) -> float:
    soc = min(max(soc, 0.0), 1.0)
    for (s0, v0), (s1, v1) in zip(OCV_TABLE, OCV_TABLE[1:]):
        if soc <= s1:
            return v0 + (v1 - v0) * (soc - s0) / (s1 - s0)
    return OCV_TABLE[-1][1]


def normalize(header: str) -> str:
    """':SOURce:CURRent:LEVel:IMMediate' → 'CURR:LEV:IMM' のように短い形に揃え、SOUR: を外す"""
    parts = []
    for token in header.strip().lstrip(":").upper().split(":"):
        query = token.endswith("?")
        word = token.rstrip("?")
        for long in _KEYWORDS:
            short = _SHORT[long]
            if word.startswith(short) and long.startswith(word):
                word = short
                break
        parts.append(word + ("?" if query else ""))
    if parts and parts[0] == "SOUR":
        parts = parts[1:]
    return ":".join(parts)


class FakeSDL:
    def __init__(self, host: str = "127.0.0.1", port: int = 5025, *, speed: float = 1.0,
                 capacity_mah: float = 3000.0, soc: float = 1.0, resistance: float = 0.05,
                 noise: float = 0.0, idn: str = IDN):
        self.host = host
        self.port = port
        self.speed = speed
        self.capacity_mah = capacity_mah
        self.remaining_mah = capacity_mah * soc
        self.resistance = resistance
        self.noise = noise
        self.idn = idn
        # 状態
        self.function = "CURR"
        self.irange = 5.0
        self.set_current = 0.0
        self.load_on = False
        self.von = 0.0          # Von（この電圧より下では電流を流さない）
        self.von_latch = True   # Von Latch。OFF なら電圧が Von を下回ると電流を止める
        self.von_supported = True  # False にすると Von のコマンドに応じない（実機で使えない場合の模擬）
        # 異常の模擬
        self.voltage_override: float | None = None  # 電圧の応答を固定する
        self.garbage = False  # MEAS に数値でない応答を返す
        self.silent = False   # 何も応答しない（タイムアウトを起こす）
        self.commands: list[str] = []
        self._lock = threading.Lock()
        self._last = time.monotonic()
        self._server: socket.socket | None = None
        self._clients: list[socket.socket] = []
        self._thread: threading.Thread | None = None
        self._running = False

    # ---- サーバ ----
    def start(self) -> "FakeSDL":
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(4)
        self.port = srv.getsockname()[1]
        self._server = srv
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, name="fake-sdl", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """サーバを止め、接続中のクライアントも切断する（電源断・ケーブル抜けの模擬）"""
        self._running = False
        if self._server is not None:
            try:
                # Linux では accept() 中のソケットを close しても待ち受けが続くため、先に shutdown する
                self._server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        self.disconnect_clients()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def restart(self) -> None:
        """同じポートでサーバを再開する（状態はそのまま）"""
        self.start()

    def disconnect_clients(self) -> None:
        with self._lock:
            clients, self._clients = self._clients, []
        for c in clients:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _ = self._server.accept()
            except OSError:
                break
            with self._lock:
                self._clients.append(conn)
            threading.Thread(target=self._client_loop, args=(conn,), daemon=True).start()

    def _client_loop(self, conn: socket.socket) -> None:
        buf = b""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    for cmd in line.decode("ascii", errors="replace").split(";"):
                        cmd = cmd.strip()
                        if not cmd:
                            continue
                        reply = self.handle(cmd)
                        if reply is not None and not self.silent:
                            conn.sendall((reply + "\n").encode("ascii"))
        except OSError:
            pass
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    # ---- 電池の模擬 ----
    def _advance(self) -> None:
        now = time.monotonic()
        dt = (now - self._last) * self.speed
        self._last = now
        if self.load_on and self.function == "CURR":
            self.remaining_mah = max(0.0, self.remaining_mah - self._actual_current() * dt / 3.6)

    def _actual_current(self) -> float:
        v = ocv(self.remaining_mah / self.capacity_mah)
        if not self.load_on or v < 1.0:
            return 0.0
        if not self.von_latch and v - self.set_current * self.resistance < self.von:
            return 0.0  # Von Latch OFF：電圧が Von を下回ったら流さない
        return self.set_current

    def _values(self) -> tuple[float, float, float]:
        current = self._actual_current()
        voltage = ocv(self.remaining_mah / self.capacity_mah) - current * self.resistance
        if self.noise:
            voltage += random.gauss(0, self.noise)
            if current:
                current += random.gauss(0, self.noise / 10)
        if self.voltage_override is not None:
            voltage = self.voltage_override
        voltage = max(voltage, 0.0)
        return voltage, current, voltage * current

    # ---- コマンド処理 ----
    def handle(self, raw: str) -> str | None:
        with self._lock:
            self.commands.append(raw)
            self._advance()
            header, _, arg = raw.partition(" ")
            cmd = normalize(header)
            arg = arg.strip().upper()
            if cmd == "*IDN?":
                return self.idn
            if re.fullmatch(r"MEAS:(VOLT|CURR|POW)(:DC)?\?", cmd):
                if self.garbage:
                    return "**ERROR**"
                v, i, p = self._values()
                if cmd.startswith("MEAS:VOLT"):
                    return f"{v:.5f}"
                if cmd.startswith("MEAS:CURR"):
                    return f"{i:.4f}"
                return f"{p:.4f}"
            if cmd == "FUNC":
                self.function = arg[:4]
                return None
            if cmd == "FUNC?":
                return self.function
            if cmd == "CURR:IRANG":
                self.irange = float(arg)
                return None
            if cmd in ("CURR:LEV:IMM", "CURR:LEV", "CURR"):
                self.set_current = float(arg)
                return None
            if cmd in ("CURR:LEV:IMM?", "CURR:LEV?", "CURR?"):
                return f"{self.set_current:.3f}"
            if cmd in ("INP:STAT", "INP"):
                self.load_on = arg in ("ON", "1")
                return None
            if cmd in ("INP:STAT?", "INP?"):
                return "1" if self.load_on else "0"
            if cmd.startswith("VOLT:") and not self.von_supported:
                return "**ERROR**" if cmd.endswith("?") else None
            if cmd in ("VOLT:LEV:ON", "VOLT:ON"):
                self.von = float(arg)
                return None
            if cmd in ("VOLT:LEV:ON?", "VOLT:ON?"):
                return f"{self.von:.3f}"
            if cmd in ("VOLT:LATC:STAT", "VOLT:LATC"):
                self.von_latch = arg in ("ON", "1")
                return None
            if cmd in ("VOLT:LATC:STAT?", "VOLT:LATC?"):
                return "1" if self.von_latch else "0"
            return None  # 未対応のコマンドは無視（実機ではエラーキューに入る）

    def count(self, command: str) -> int:
        with self._lock:
            return sum(1 for c in self.commands if c == command)


def main() -> None:
    ap = argparse.ArgumentParser(description="SDL1020X-E シミュレータ")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5025)
    ap.add_argument("--speed", type=float, default=1.0, help="時間の倍速（既定 1）")
    ap.add_argument("--capacity", type=float, default=3000.0, help="電池容量 mAh（既定 3000）")
    ap.add_argument("--soc", type=float, default=1.0, help="開始時の充電状態 0〜1（既定 1.0 = 4.20V）")
    ap.add_argument("--resistance", type=float, default=0.05, help="内部抵抗 Ω（既定 0.05）")
    ap.add_argument("--noise", type=float, default=0.001, help="電圧ノイズの標準偏差 V（既定 0.001）")
    args = ap.parse_args()
    fake = FakeSDL(args.host, args.port, speed=args.speed, capacity_mah=args.capacity, soc=args.soc,
                   resistance=args.resistance, noise=args.noise).start()
    print(f"SDL シミュレータ起動: {args.host}:{fake.port}（{args.speed:g} 倍速、{args.capacity:g}mAh）")
    print("Ctrl+C で終了")
    try:
        last_state = None
        while True:
            time.sleep(1)
            with fake._lock:
                fake._advance()
                v, i, _ = fake._values()
                state = fake.load_on
            if state or state != last_state:
                print(f"負荷 {'ON ' if state else 'OFF'}  {v:.4f} V  {i:.3f} A  残 {fake.remaining_mah:.1f} mAh")
            last_state = state
    except KeyboardInterrupt:
        pass
    finally:
        fake.stop()


if __name__ == "__main__":
    main()
