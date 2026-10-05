"""放電終了を音で知らせる"""

import time

import notify


def test_alarm_repeats_until_stopped(monkeypatch):
    played = []
    monkeypatch.setattr(notify, "_beep", lambda f, ms: played.append(f))
    alarm = notify.Alarm(notify.DONE, interval=0.05).start()
    deadline = time.monotonic() + 3
    while alarm.played < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert alarm.played >= 3
    alarm.stop()
    time.sleep(0.15)
    count = alarm.played
    time.sleep(0.15)
    assert alarm.played == count and not alarm.running
    assert played[:3] == [880, 1175, 1568]


def test_alarm_gives_up_after_max_duration(monkeypatch):
    monkeypatch.setattr(notify, "_beep", lambda f, ms: None)
    alarm = notify.Alarm(notify.ALERT, interval=0.02, max_duration=0.1).start()
    time.sleep(0.5)
    assert not alarm._thread.is_alive()
