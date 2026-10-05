"""設定ファイル（sdl_logger_settings.json）の読み書き

保存する項目: IP、ポート、保存先フォルダ、放電電流、終止電圧、取得周期、
オプション（測定中は画面を消さない・完了を音で知らせる・Von を自動設定）。
メーカー・満充電電圧・型番・備考は保存しない（前回の値が残って誤記録になるのを防ぐ）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields
from pathlib import Path

import paths

log = logging.getLogger("sdl.settings")

DEFAULT_HOST = "192.168.10.2"
DEFAULT_PORT = 5025

CURRENT_RANGE = (0.001, 5.000)
CUTOFF_RANGE = (2.000, 4.200)
INTERVAL_RANGE = (0.2, 60.0)
PORT_RANGE = (1, 65535)


@dataclass
class Settings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    folder: str = ""
    current: float = 0.400  # 初めて起動したとき（設定ファイルが無いとき）の放電電流
    cutoff: float = 3.500   # 同じく終止電圧
    interval: float = 1.0
    keep_awake: bool = True  # 測定中は画面を消さない・ロックしない・スリープしない
    sound: bool = True       # 放電が終わったら音で知らせる
    auto_von: bool = True    # 開始時に Von を終止電圧 −0.1 V に設定する


def _in_range(value, lo, hi) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and lo <= value <= hi


def load(path: Path | None = None) -> Settings:
    """設定を読む。ファイルが無い・壊れている・範囲外の値は既定値にする"""
    path = path or paths.settings_path()
    s = Settings(folder=str(paths.app_dir()))  # 保存先の初期値は exe と同じフォルダ
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return s
    except (OSError, ValueError) as e:
        log.warning("設定ファイルを読めません（既定値を使います）%s: %s", path, e)
        return s
    if not isinstance(data, dict):
        return s
    if isinstance(data.get("host"), str) and data["host"].strip():
        s.host = data["host"].strip()
    if isinstance(data.get("port"), int) and _in_range(data["port"], *PORT_RANGE):
        s.port = data["port"]
    if isinstance(data.get("folder"), str) and data["folder"] and Path(data["folder"]).is_dir():
        s.folder = data["folder"]
    for name, rng in (("current", CURRENT_RANGE), ("cutoff", CUTOFF_RANGE), ("interval", INTERVAL_RANGE)):
        if _in_range(data.get(name), *rng):
            setattr(s, name, float(data[name]))
    for name in ("keep_awake", "sound", "auto_von"):
        if isinstance(data.get(name), bool):
            setattr(s, name, data[name])
    return s


def save(s: Settings, path: Path | None = None) -> bool:
    path = path or paths.settings_path()
    data = {f.name: getattr(s, f.name) for f in fields(Settings)}
    try:
        tmp = Path(path).with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError as e:
        log.warning("設定ファイルを保存できません %s: %s", path, e)
        return False

