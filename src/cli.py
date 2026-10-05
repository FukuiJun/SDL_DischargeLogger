"""GUI なしで放電 → 自動停止 → 保存を行う（動作確認用）

    python src/cli.py --host 127.0.0.1 --current 1.0 --cutoff 3.0 --folder out \
        --maker Panasonic --full 4.1V --model NCR18650B --note "25℃"

Ctrl+C で停止・保存。
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
from pathlib import Path

import applog
import recorder
from sdl_client import DEFAULT_PORT, SDLClient, SDLError
from session import Conditions, DischargeSession, StartError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SDL 放電ロガー（CLI）")
    ap.add_argument("--host", default="192.168.10.2")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--current", type=float, required=True, help="放電電流 [A]")
    ap.add_argument("--cutoff", type=float, required=True, help="終止電圧 [V]")
    ap.add_argument("--interval", type=float, default=1.0, help="取得周期 [s]（既定 1.0）")
    ap.add_argument("--folder", type=Path, default=Path.cwd(), help="保存先フォルダ")
    ap.add_argument("--maker", choices=recorder.MAKERS)
    ap.add_argument("--full", choices=recorder.FULL_VOLTAGES, help="満充電電圧")
    ap.add_argument("--model", default="", help="型番")
    ap.add_argument("--number", default="", help="番号（半角の英数字と - _。ファイル名の末尾に _no<番号>）")
    ap.add_argument("--note", default="", help="備考")
    args = ap.parse_args(argv)

    applog.setup()
    logging.getLogger().addHandler(logging.StreamHandler(sys.stderr))

    client = SDLClient(args.host, args.port)
    try:
        print("接続:", client.connect())
        session = DischargeSession(client, args.folder,
                                   Conditions(args.maker, args.full, args.model, args.current, args.cutoff,
                                              args.interval, number=recorder.sanitize_number(args.number)),
                                   note=args.note)
        session.start()
    except (SDLError, StartError) as e:
        print("エラー:", e, file=sys.stderr)
        client.close()
        return 1

    print(f"放電開始: {session.base_name}（Ctrl+C で停止・保存）")
    try:
        while True:
            try:
                kind, payload = session.events.get(timeout=0.5)
            except queue.Empty:
                continue
            if kind == "sample":
                s = payload
                print(f"{s.elapsed:9.1f}s  {s.voltage:.4f}V  {s.current:.4f}A  {s.power:.3f}W  "
                      f"{s.mah:8.1f}mAh  {s.wh:.3f}Wh")
            elif kind == "status":
                print(payload)
            elif kind == "reconnecting":
                print(f"通信が途切れました。再接続しています（{payload[0]}/{payload[1]}）")
            elif kind == "reconnected":
                print("再接続しました。記録を続けます")
            elif kind == "finished":
                break
    except KeyboardInterrupt:
        print("停止・保存します")
        session.request_stop(save=True)
        session.wait()
    result = session.result
    client.close()
    print("終了理由:", result.end_reason)
    for m in result.messages:
        print(m)
    if result.csv_path:
        print("CSV:", result.csv_path)
    if result.png_path:
        print("PNG:", result.png_path)
    if result.error:
        print(result.error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
