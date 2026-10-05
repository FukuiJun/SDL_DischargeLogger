"""放電が終わったことを音で知らせる

完了（終止電圧到達）は上がっていく 3 音、通信断・エラーで止まったときは低い 2 音。
画面ロック中でも鳴る（スリープ中は鳴らない）。止めるまで数秒おきにくり返す（最長 10 分）。
Windows では winsound.Beep を使い、Windows のサウンド設定（通知音なし）に関係なく鳴らす。
"""

from __future__ import annotations

import logging
import sys
import threading

log = logging.getLogger("sdl.notify")

DONE = "done"
ALERT = "alert"
PATTERNS = {
    DONE: [(880, 160), (1175, 160), (1568, 320)],
    ALERT: [(440, 350), (0, 120), (440, 350)],
}
REPEAT_INTERVAL_S = 3.0
MAX_DURATION_S = 10 * 60


def _beep(freq: int, ms: int) -> None:
    if sys.platform == "win32":
        import winsound

        if freq:
            winsound.Beep(freq, ms)
        else:
            threading.Event().wait(ms / 1000)


class Alarm:
    """鳴らし始めると stop() されるまでくり返す"""

    def __init__(self, kind: str, *, interval: float = REPEAT_INTERVAL_S, max_duration: float = MAX_DURATION_S):
        self.kind = kind
        self.interval = interval
        self.max_duration = max_duration
        self.played = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="alarm", daemon=True)

    def start(self) -> "Alarm":
        log.info("音で知らせます（%s）", self.kind)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return self._thread.is_alive() and not self._stop.is_set()

    def _run(self) -> None:
        waited = 0.0
        while not self._stop.is_set() and waited <= self.max_duration:
            try:
                for freq, ms in PATTERNS[self.kind]:
                    if self._stop.is_set():
                        return
                    _beep(freq, ms)
            except Exception as e:  # noqa: BLE001 - 音が鳴らなくても動作は続ける
                log.warning("音を鳴らせません: %s", e)
                return
            self.played += 1
            if self._stop.wait(self.interval):
                return
            waited += self.interval


def start_alarm(kind: str) -> Alarm:
    return Alarm(kind).start()
