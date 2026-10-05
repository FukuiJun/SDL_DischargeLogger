"""画面の自動テスト（AC-01, 05, 09, 10 と DESIGN.md D-AC-03 の自動化できる部分）。ディスプレイが無ければスキップ"""

import time
from pathlib import Path

import pytest

try:
    import tkinter as tk

    _r = tk.Tk()
    _r.destroy()
    HAS_DISPLAY = True
except Exception:  # noqa: BLE001
    HAS_DISPLAY = False

pytestmark = pytest.mark.skipif(not HAS_DISPLAY, reason="ディスプレイが無いためスキップ")


class Dialogs:
    def __init__(self):
        self.calls = []
        self.answer = True

    def recorder(self, name):
        def f(*args, **kwargs):
            self.calls.append((name, args[1] if len(args) > 1 else kwargs.get("message", "")))
            return self.answer if name.startswith("ask") else "ok"
        return f

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def dialogs(monkeypatch):
    import gui

    d = Dialogs()
    for name in ("showinfo", "showwarning", "showerror", "askyesno", "askyesnocancel"):
        monkeypatch.setattr(gui.messagebox, name, d.recorder(name))
    return d


def pump(root, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def make_app(dialogs, tmp_path):
    import gui

    apps = []

    def make():
        root = tk.Tk()
        app = gui.App(root)
        apps.append(app)
        return app

    yield make
    for app in apps:
        try:
            app.root.destroy()
        except tk.TclError:
            pass


def connect(app, fake, folder):
    app.host_var.set("127.0.0.1")
    app.port_var.set(str(fake.port))
    app.folder_var.set(str(folder))
    app.on_connect()
    assert pump(app.root, lambda: app.state == "idle")


def start(app, interval="0.2"):
    app.interval_var.set(interval)
    app.onoff_btn.invoke()  # ON/OFF キーで放電開始
    assert pump(app.root, lambda: app.state == "discharging")


def enabled(app):
    return {name: getattr(app, name).enabled
            for name in ("onoff_btn", "csv_btn", "graph_btn", "connect_btn")}


def values(app):
    return {k: lbl["text"] for k, lbl in app.value_labels.items()}


def test_disconnected_state(make_app):
    """D-AC-03 未接続：ピル「未接続」、LOAD OFF、数値は ---、操作ボタンはすべて無効"""
    app = make_app()
    assert app.pill_var.get() == "未接続"
    assert app.load_label["text"] == "LOAD OFF" and app.state_label["text"] == "未接続"
    assert set(values(app).values()) == {"---"}
    assert enabled(app) == {"onoff_btn": False, "csv_btn": False, "graph_btn": False, "connect_btn": True}
    assert not app.onoff_btn.lit
    assert str(app.onoff_btn.frame["bg"]) == "#c9cdd1"
    assert str(app.graph_btn.button["bg"]) == "#e9ebed"  # 押せないときは灰色
    assert app.plan_label["text"] == f"保存予定: {Path('YYYYMMDD_HHMM_SDL') / 'YYYYMMDD_HHMM.csv'}"
    app.radios[1].invoke()
    app.radios[4].invoke()
    assert app.plan_label["text"] == f"保存予定: {Path('YYYYMMDD_HHMM_SDL') / 'YYYYMMDD_HHMM_4v1_pana.csv'}"
    assert [c["text"] for c in app.chip_values] == ["0.400 A", "3.500 V", "1.0 s", "4.1 V", "Panasonic"]


def test_connect_idle_state(make_app, fake, tmp_path):
    """AC-01／F-01／D-AC-03 待機：IDN と「接続中」、電圧・電流・電力を 1 秒ごとに表示"""
    app = make_app()
    connect(app, fake, tmp_path)
    assert app.pill_var.get() == "接続中"
    assert app.model_label["text"] == "SDL1020X-E 200W DC Electronic Load"
    assert "Siglent Technologies,SDL1020X-E" in app.message_var.get()
    assert app.state_label["text"] == "待機中"
    assert app.connect_btn.button["text"] == "切断"
    assert str(app.host_entry["state"]) == "readonly"
    assert pump(app.root, lambda: values(app)["v"].endswith(" V"))
    v = values(app)
    assert v["i"] == "0.000 A" and v["p"].endswith(" W")
    assert v["mah"] == v["wh"] == v["elapsed"] == "---"
    assert enabled(app)["onoff_btn"] and not enabled(app)["graph_btn"]  # データが無いのでグラフ保存は無効
    assert not enabled(app)["csv_btn"] and not app.onoff_btn.lit


def test_connect_failure_message(make_app, dialogs, tmp_path):
    """AC-01：つながらないとき F-01 のメッセージ。放電開始不可"""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    app = make_app()
    app.host_var.set("127.0.0.1")
    app.port_var.set(str(port))
    app.on_connect()
    assert pump(app.root, lambda: "showerror" in dialogs.names())
    assert "接続できません。IP アドレスと LAN ケーブルを確認してください" in dialogs.calls[-1][1]
    assert app.state == "disconnected"
    assert not app.onoff_btn.enabled


def test_unselected_can_be_chosen_again(make_app):
    """F-02：起動時は未選択。一度選んだ後でも未選択に戻せる"""
    app = make_app()
    assert app.maker_var.get() == "" and app.full_var.get() == ""
    assert [str(rb["text"]) for rb in app.radios] == ["未選択", "Panasonic", "マクセル", "未選択", "4.1V", "4.2V"]
    app.radios[1].invoke()
    assert app.maker_var.get() == "Panasonic"
    assert str(app.radios[1]["fg"]) == "#ffe14a"  # 選択中は黄色の文字
    app.radios[0].invoke()
    assert app.maker_var.get() == ""
    assert str(app.radios[1]["fg"]) != "#ffe14a"


def test_model_max_40(make_app):
    app = make_app()
    app.model_entry.insert(0, "x" * 50)
    assert len(app.model_var.get()) == 40


def test_discharge_auto_stop_saves(make_app, fake, dialogs, tmp_path):
    """F-03〜F-05／D-AC-03 放電中→完了：放電中は備考のみ編集可。終止電圧で自動停止・保存"""
    app = make_app()
    connect(app, fake, tmp_path)
    app.radios[2].invoke()  # マクセル
    app.model_var.set("NCR18650B")
    app.note_text.insert("1.0", "最初")
    start(app)
    assert app.load_label["text"] == "LOAD ON" and app.state_label["text"] == "● 放電中"
    run_dir = app.session.run_dir  # 保存先の中の出力フォルダ <YYYYMMDD_HHMM>_SDL
    assert run_dir.parent == tmp_path and run_dir.name.endswith("_SDL") and run_dir.name[:13] == app.session.base_name[:13]
    assert app.plan_label["text"] == f"保存予定: {Path(run_dir.name) / (app.session.base_name + '.csv')}"
    assert app.message_var.get().endswith("放電開始（0.400 A / 終止 3.500 V / Von 3.400 V）")
    assert not any(c[0] == "askyesno" for c in dialogs.calls)  # 初期値（0.400 A / 3.500 V）では確認を出さない
    assert str(app.model_entry["state"]) == "disabled"
    assert all(str(rb["state"]) == "disabled" for rb in app.radios)
    assert str(app.note_text["state"]) == "normal"
    # グラフ保存は CSV保存と同じく、放電中は押せない（OFF にしてから押せる）
    assert enabled(app) == {"onoff_btn": True, "csv_btn": False, "graph_btn": False, "connect_btn": False}
    assert app.onoff_btn.lit  # 放電中は ON/OFF キーの外枠と文字が黄緑に光り、キーは緑がかった黒になる
    import theme

    lit = theme.COLORS["onoff-lit"]
    assert str(app.onoff_btn.frame["bg"]) == lit
    assert str(app.onoff_btn.button["fg"]) == lit
    assert str(app.onoff_btn.button["bg"]) == theme.COLORS["onoff-key-lit"]
    assert str(app.graph_btn.button["bg"]) == "#e9ebed"  # 押せないときは灰色
    app.note_text.insert("end", "→変更")
    assert pump(app.root, lambda: values(app)["mah"].endswith("mAh"))
    fake.voltage_override = 2.5
    assert pump(app.root, lambda: app.state == "done")
    assert "showinfo" in dialogs.names()
    base = app.session.base_name
    csv_path = app.session.run_dir / f"{base}.csv"
    assert base.endswith("_maxell")
    assert csv_path.exists() and (app.session.run_dir / f"{base}.png").exists()
    assert app.message_var.get().endswith(f"終止電圧に到達しました。保存しました: {csv_path}")
    assert app.state_label["text"] == "完了: 終止電圧到達" and app.load_label["text"] == "LOAD OFF"
    assert app.plan_label["text"] == f"保存済み: {Path(app.session.run_dir.name) / (base + '.csv')}"
    # 自動保存のあとも CSV保存を押せる（もう一度保存すると <ベース名>_2.csv）
    assert enabled(app) == {"onoff_btn": True, "csv_btn": True, "graph_btn": True, "connect_btn": True}
    assert str(app.graph_btn.button["bg"]) == "#b7282e"  # グラフ保存は押せるとき茜色
    assert not app.onoff_btn.lit
    assert values(app)["v"] == "2.500 V"  # 最終値で止める
    assert app.model_var.get() == "NCR18650B"  # 完了後も値は残す（Q-D2）
    text = csv_path.read_text(encoding="utf-8-sig")
    assert '備考,"最初→変更"' in text
    assert "メーカー,マクセル" in text


def test_reconnecting_state(make_app, fake, tmp_path):
    """D-AC-03 再接続中：ピル「再接続中 (n/10)」、通信断表示、数値は最後の値を淡色、グラフ保存は無効"""
    import gui

    app = make_app()
    connect(app, fake, tmp_path)
    start(app)
    assert pump(app.root, lambda: values(app)["mah"].endswith("mAh"))
    fake.stop()
    assert pump(app.root, lambda: app.state == "reconnecting")
    assert app.pill_var.get().startswith("再接続中 (") and app.pill_var.get().endswith("/10)")
    assert app.state_label["text"] == "通信断 — 再接続中"
    assert str(app.value_labels["v"]["fg"]) == gui.C["lcd-dim"]
    assert enabled(app) == {"onoff_btn": True, "csv_btn": False, "graph_btn": False, "connect_btn": False}
    assert app.onoff_btn.lit
    fake.restart()
    assert pump(app.root, lambda: app.state == "discharging", timeout=15)
    assert str(app.value_labels["v"]["fg"]) == gui.C["lcd-value"]
    app.onoff_btn.invoke()
    assert pump(app.root, lambda: app.state == "done")
    assert app.state_label["text"] == "完了: 手動停止"
    assert app.message_var.get().endswith("停止しました。［CSV保存］で保存できます")


def test_start_refused_below_cutoff(make_app, fake, dialogs, tmp_path):
    app = make_app()
    connect(app, fake, tmp_path)
    fake.voltage_override = 2.9
    app.on_start()
    assert pump(app.root, lambda: "showwarning" in dialogs.names())
    assert "電圧が終止電圧以下です" in dialogs.calls[-1][1]
    assert app.state == "idle"


def test_invalid_input_refused(make_app, fake, dialogs, tmp_path):
    app = make_app()
    connect(app, fake, tmp_path)
    app.current_var.set("5.5")
    assert app.chip_values[0]["text"] == "---"
    app.on_start()
    assert dialogs.calls[-1][0] == "showerror"
    assert "0.001〜5.000" in dialogs.calls[-1][1]
    assert app.state == "idle"


def test_onoff_stop_then_csv_save(make_app, fake, dialogs, tmp_path):
    """ON/OFF で停止 → 負荷 OFF・保存待ち（CSV保存が有効）→ CSV保存で CSV と PNG を保存"""
    app = make_app()
    connect(app, fake, tmp_path)
    app.note_text.insert("1.0", "メモ")
    start(app)
    assert pump(app.root, lambda: values(app)["mah"].endswith("mAh"))
    app.onoff_btn.invoke()
    assert pump(app.root, lambda: app.state == "done")
    assert not fake.load_on and not app.onoff_btn.lit
    base = app.session.base_name
    assert app.plan_label["text"] == "未保存（［CSV保存］で保存）"
    assert enabled(app)["csv_btn"]
    assert not (app.session.run_dir / f"{base}.csv").exists() and (app.session.run_dir / f"{base}_partial.csv").exists()
    app.note_text.insert("end", "→保存時点")
    app.csv_btn.invoke()
    assert pump(app.root, lambda: not app.pending and not app._saving)
    csv_path = app.session.run_dir / f"{base}.csv"
    assert csv_path.exists() and (app.session.run_dir / f"{base}.png").exists()
    assert not (app.session.run_dir / f"{base}_partial.csv").exists()
    assert '備考,"メモ→保存時点"' in csv_path.read_text(encoding="utf-8-sig")
    assert app.message_var.get().endswith(f"保存しました: {csv_path}")
    assert app.plan_label["text"] == f"保存済み: {Path(app.session.run_dir.name) / (base + '.csv')}"
    # 保存したあとも何度でも保存できる。上書きせずに _2, _3 … を付ける（備考は押した時点の内容）
    assert enabled(app)["csv_btn"]
    app.note_text.insert("end", "→2回目")
    app.csv_btn.invoke()
    assert pump(app.root, lambda: not app._saving and (app.session.run_dir / f"{base}_2.csv").exists())
    second = app.session.run_dir / f"{base}_2.csv"
    assert (app.session.run_dir / f"{base}_2.png").exists()
    assert '備考,"メモ→保存時点→2回目"' in second.read_text(encoding="utf-8-sig")
    assert '備考,"メモ→保存時点"' in csv_path.read_text(encoding="utf-8-sig")  # 1 回目のファイルはそのまま
    assert app.message_var.get().endswith(f"保存しました: {second}")
    assert app.plan_label["text"] == f"保存済み: {Path(app.session.run_dir.name) / (base + '_2.csv')}"
    assert enabled(app)["csv_btn"]


def test_unsaved_data_confirm_on_start(make_app, fake, dialogs, tmp_path):
    """保存せずに次の放電を始めるときは確認。いいえ＝何もしない、はい＝破棄して開始（停止・破棄の代わり）"""
    app = make_app()
    connect(app, fake, tmp_path)
    start(app)
    app.onoff_btn.invoke()
    assert pump(app.root, lambda: app.state == "done")
    first = app.session
    dialogs.answer = False
    app.onoff_btn.invoke()
    assert dialogs.calls[-1] == ("askyesno", "保存していないデータがあります。破棄して放電を開始しますか？")
    assert app.state == "done" and app.pending
    dialogs.answer = True
    app.onoff_btn.invoke()
    assert pump(app.root, lambda: app.state == "discharging")
    assert first.result.discarded
    # 破棄した一時ファイルは消える（同じ秒に始めると新しい放電が同じ名前を使うので、その場合は除く）
    assert app.session.base_name == first.base_name or not first.paths["partial"].exists()
    app.onoff_btn.invoke()
    assert pump(app.root, lambda: app.state == "done")


def test_unsaved_data_on_close(make_app, fake, dialogs, tmp_path):
    """保存せずに閉じるときは確認（はい＝保存して終了）"""
    app = make_app()
    connect(app, fake, tmp_path)
    start(app)
    app.onoff_btn.invoke()
    assert pump(app.root, lambda: app.state == "done")
    base = app.session.base_name
    dialogs.answer = True
    closed = []
    app.root.bind("<Destroy>", lambda e: closed.append(1) if e.widget is app.root else None)
    app.on_close()
    assert dialogs.calls[-1] == ("askyesnocancel", "保存していないデータがあります。保存して終了しますか？")
    assert closed
    assert (app.session.run_dir / f"{base}.csv").exists()


def test_save_graph_names(make_app, fake, tmp_path):
    """F-08：開始後は <ベース名>_<時刻>.png。未開始は graph_<日時>.png（ここでは関数を直接呼ぶ）"""
    app = make_app()
    app.folder_var.set(str(tmp_path))
    app.on_save_graph()
    pngs = list(tmp_path.glob("graph_*.png"))
    assert len(pngs) == 1 and len(pngs[0].stem) == len("graph_20261001_143005")
    connect(app, fake, tmp_path)
    start(app)
    app.onoff_btn.invoke()
    assert pump(app.root, lambda: app.state == "done")
    assert app.graph_btn.button["state"] == "normal"  # OFF にしたら押せる
    app.graph_btn.invoke()
    base = app.session.base_name
    named = list(app.session.run_dir.glob(f"{base}_*.png"))  # 放電の出力フォルダに保存
    assert len(named) == 1 and len(named[0].stem) == len(base) + 7
    assert app.message_var.get().endswith(f"グラフを保存しました: {named[0]}")


def test_disconnect_clears(make_app, fake, tmp_path):
    app = make_app()
    connect(app, fake, tmp_path)
    start(app)
    app.onoff_btn.invoke()
    assert pump(app.root, lambda: app.state == "done")
    app.csv_btn.invoke()
    assert pump(app.root, lambda: not app.pending and not app._saving)
    app.on_connect()  # 切断
    assert app.state == "disconnected"
    assert set(values(app).values()) == {"---"}
    assert app.model_label["text"] == ""


def test_settings_restored(make_app, fake, tmp_path, app_dir):
    """AC-10：設定は再起動後に復元。メーカー・満充電電圧は未選択、型番・備考は空"""
    app = make_app()
    app.host_var.set("192.168.10.9")
    app.port_var.set("5026")
    app.folder_var.set(str(tmp_path))
    app.current_var.set("2.5")
    app.cutoff_var.set("2.8")
    app.interval_var.set("5")
    app.maker_var.set("Panasonic")
    app.full_var.set("4.1V")
    app.model_var.set("ABC")
    app.note_text.insert("1.0", "メモ")
    app.on_close()
    assert (app_dir / "sdl_logger_settings.json").exists()
    app2 = make_app()
    assert app2.host_var.get() == "192.168.10.9"
    assert app2.port_var.get() == "5026"
    assert app2.folder_var.get() == str(tmp_path)
    assert app2.current_var.get() == "2.500"
    assert app2.cutoff_var.get() == "2.800"
    assert app2.interval_var.get() == "5.0"
    assert app2.maker_var.get() == "" and app2.full_var.get() == ""
    assert app2.model_var.get() == "" and app2.note() == ""


def test_close_dialog_choices(make_app, dialogs):
    """F-10／DESIGN 11 章：標準のメッセージボックス（はい=保存して終了、いいえ=破棄して終了）"""
    app = make_app()
    for answer, expected in ((True, "save"), (False, "discard"), (None, None)):
        dialogs.answer = answer
        assert app.ask_close_choice() == expected
    assert dialogs.calls[-1] == ("askyesnocancel", "放電中です。停止・保存して終了しますか？")


def test_close_during_discharge_saves(make_app, fake, tmp_path, monkeypatch):
    """F-10：放電中に閉じる →「保存して終了」で停止・保存してから終了"""
    app = make_app()
    connect(app, fake, tmp_path)
    start(app)
    monkeypatch.setattr(app, "ask_close_choice", lambda: None)
    app.on_close()
    assert app.state == "discharging"  # キャンセル
    monkeypatch.setattr(app, "ask_close_choice", lambda: "save")
    base = app.session.base_name
    app.on_close()
    closed = []
    app.root.bind("<Destroy>", lambda e: closed.append(1) if e.widget is app.root else None)
    pump(app.root, lambda: bool(closed), timeout=10)
    assert (app.session.run_dir / f"{base}.csv").exists()
    assert not fake.load_on


def test_close_during_discharge_discards(make_app, fake, tmp_path, monkeypatch):
    app = make_app()
    connect(app, fake, tmp_path)
    start(app)
    monkeypatch.setattr(app, "ask_close_choice", lambda: "discard")
    session = app.session
    app.on_close()
    closed = []
    app.root.bind("<Destroy>", lambda e: closed.append(1) if e.widget is app.root else None)
    pump(app.root, lambda: bool(closed), timeout=10)
    assert session.finished
    assert list(tmp_path.iterdir()) == []
    assert not fake.load_on


def test_partial_file_notice_at_startup(dialogs, tmp_path, app_dir):
    """9 章：起動時に保存先の *_partial.csv を検出したら知らせる（自動処理はしない）"""
    import json

    import gui

    (app_dir / "sdl_logger_settings.json").write_text(json.dumps({"folder": str(tmp_path)}), encoding="utf-8")
    p = tmp_path / "20261001_1430_4v1_pana_partial.csv"
    p.write_text("x")
    root = tk.Tk()
    try:
        gui.App(root)
        pump(root, lambda: bool(dialogs.calls), timeout=3)
        assert dialogs.calls[0][0] == "showwarning"
        assert f"前回の記録が途中で終了しています:\n{p}" == dialogs.calls[0][1]
        assert p.exists()
    finally:
        root.destroy()


def test_side_panel_fixed_width_when_enlarged(make_app):
    """D-AC-05：ウィンドウを広げるとグラフが伸び、試験条件パネルは幅 400 のまま"""
    app = make_app()
    app.root.update()
    side = app.note_text.master.master
    graph = app.canvas.get_tk_widget()
    w0, g0 = side.winfo_width(), graph.winfo_width()
    app.root.geometry("1600x1000")
    app.root.update()
    pump(app.root, lambda: graph.winfo_width() > g0, timeout=3)
    assert side.winfo_width() == w0 == 400
    assert graph.winfo_width() > g0


def test_window_title(make_app):
    """左上（タイトルバー）のアプリ名は SDL_DischargeLogger"""
    app = make_app()
    assert app.root.title() == "SDL_DischargeLogger"


def test_time_span_control(make_app, fake, tmp_path):
    """グラフの横軸の幅：選択肢・自由入力（90分 など）・不正入力は元に戻す。測定中も変えられる"""
    import gui

    assert gui.parse_span("自動") is None
    assert gui.parse_span("90分") == 1.5
    assert gui.parse_span("2.5時間") == 2.5
    assert gui.parse_span("4") == 4.0
    assert gui.format_span(0.5) == "30分" and gui.format_span(2.5) == "2.5時間" and gui.format_span(None) == "自動"
    app = make_app()
    connect(app, fake, tmp_path)
    start(app)
    app.span_var.set("2時間")
    app.apply_span()
    assert app.time_span == 2.0
    assert app.plot.ax_i.get_xlim() == (0.0, 2.0)
    app.span_var.set("abc")
    app.apply_span()
    assert app.time_span == 2.0 and app.span_var.get() == "2時間"
    assert "10分〜100時間" in app.message_var.get()
    app.span_var.set("自動")
    app.apply_span()
    assert app.time_span is None
    app.onoff_btn.invoke()
    assert pump(app.root, lambda: app.state == "done")


def test_scaled_layout(make_app, monkeypatch):
    """表示倍率 150% のとき：寸法・文字・グラフの解像度が 1.5 倍（ぼやけ対策）"""
    import theme

    monkeypatch.setenv("SDL_LOGGER_SCALE", "1.5")
    try:
        app = make_app()
        app.root.update()
        side = app.note_text.master.master
        assert side.winfo_reqwidth() == 600 or side.winfo_width() == 600
        assert app.figure.dpi == 150
        assert app.fonts.ui_px(20, True)[1] == -30
    finally:
        theme.set_scale(1.0)


@pytest.mark.parametrize("current, cutoff, expected", [
    ("2.000", "3.500", ["放電電流が 2.0 A 以上です（設定 2.000 A）"]),
    ("0.400", "3.000", ["終止電圧が 3.0 V 以下です（設定 3.000 V）"]),
    ("2.500", "2.800", ["放電電流が 2.0 A 以上です（設定 2.500 A）", "終止電圧が 3.0 V 以下です（設定 2.800 V）"]),
])
def test_confirm_unusual_conditions(make_app, fake, dialogs, tmp_path, current, cutoff, expected):
    """放電電流 2A 以上・終止電圧 3.0V 以下なら、開始前に設定を間違えていないか確認する。いいえなら開始しない"""
    app = make_app()
    connect(app, fake, tmp_path)
    app.current_var.set(current)
    app.cutoff_var.set(cutoff)
    dialogs.answer = False
    app.onoff_btn.invoke()
    name, text = dialogs.calls[-1][:2]
    assert name == "askyesno"
    assert text == "設定を確認してください。\n\n" + "\n".join(f"・{t}" for t in expected) + "\n\nこの設定で放電を開始しますか？"
    assert app.state == "idle" and app.session is None and not fake.load_on
    dialogs.answer = True
    start(app)
    assert fake.load_on


def test_no_confirm_just_inside_limits(make_app, fake, dialogs, tmp_path):
    """1.999 A・3.001 V は確認なしで開始する"""
    app = make_app()
    connect(app, fake, tmp_path)
    app.current_var.set("1.999")
    app.cutoff_var.set("3.001")
    start(app)
    assert not any(c[0] == "askyesno" for c in dialogs.calls)


def test_option_checkboxes(make_app, fake, tmp_path, app_dir):
    """オプションは初期値 ON。Von の表示は終止電圧 −0.1 V。放電中は Von だけ変えられない。設定は保存される"""
    app = make_app()
    assert app.keep_awake_var.get() and app.sound_var.get() and app.auto_von_var.get()
    assert app.von_text.get() == "Von を自動設定（終止電圧 − 0.1 V = 3.400 V）"
    app.cutoff_var.set("3.2")
    assert app.von_text.get() == "Von を自動設定（終止電圧 − 0.1 V = 3.100 V）"
    app.cutoff_var.set("abc")
    assert app.von_text.get() == "Von を自動設定（終止電圧 − 0.1 V）"
    app.cutoff_var.set("3.500")
    assert app.read_conditions().von == 3.4
    app.auto_von_chk.invoke()
    assert app.read_conditions().von is None
    app.auto_von_chk.invoke()
    connect(app, fake, tmp_path)
    start(app)
    assert fake.von == 3.4 and fake.von_latch is False
    assert str(app.auto_von_chk["state"]) == "disabled"
    assert str(app.sound_chk["state"]) == "normal" and str(app.keep_awake_chk["state"]) == "normal"
    app.onoff_btn.invoke()
    assert pump(app.root, lambda: app.state == "done")
    assert str(app.auto_von_chk["state"]) == "normal"
    assert fake.von == 0.0 and fake.von_latch is True  # 終わったら元に戻す
    app.sound_chk.invoke()
    app._remember_settings()
    assert app.settings.sound is False and app.settings.auto_von is True


def test_start_without_von_when_unchecked(make_app, fake, tmp_path):
    app = make_app()
    app.auto_von_var.set(False)
    connect(app, fake, tmp_path)
    start(app)
    assert fake.von == 0.0 and not any(":SOUR:VOLT:LEV:ON" in c for c in fake.commands)
    assert app.message_var.get().endswith("放電開始（0.400 A / 終止 3.500 V）")


def test_von_failure_message(make_app, fake, dialogs, tmp_path):
    """Von を設定できない SDL では開始せず、チェックを外せば開始できると知らせる"""
    fake.von_supported = False
    app = make_app()
    connect(app, fake, tmp_path)
    app.onoff_btn.invoke()
    assert pump(app.root, lambda: app.state == "idle" and dialogs.calls)
    name, text = dialogs.calls[-1]
    assert name == "showwarning" and "Von" in text and "チェックを外す" in text
    assert not fake.load_on


def test_keep_awake_while_discharging(make_app, fake, tmp_path, monkeypatch):
    """［測定中は画面を消さない・ロックしない］：放電中だけ ON。チェックを外すとすぐ元に戻す"""
    import power

    calls = []
    monkeypatch.setattr(power, "keep_awake", lambda on: calls.append(on) or True)
    app = make_app()
    connect(app, fake, tmp_path)
    assert calls and not any(calls)
    start(app)
    assert calls[-1] is True
    app.keep_awake_chk.invoke()
    assert calls[-1] is False
    app.keep_awake_chk.invoke()
    assert calls[-1] is True
    app.onoff_btn.invoke()
    assert pump(app.root, lambda: app.state == "done")
    assert calls[-1] is False


def _record_alarms(monkeypatch):
    import notify

    started = []

    class FakeAlarm:
        def __init__(self, kind):
            self.kind, self.stopped = kind, False
            started.append(self)

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(notify, "start_alarm", FakeAlarm)
    return started


def test_sound_on_cutoff_until_dialog_closed(make_app, fake, dialogs, tmp_path, monkeypatch):
    """終止電圧で完了したら完了の音を鳴らし、ダイアログを閉じたら止める"""
    started = _record_alarms(monkeypatch)
    app = make_app()
    connect(app, fake, tmp_path)
    start(app)
    fake.voltage_override = 2.5
    assert pump(app.root, lambda: app.state == "done")
    assert [a.kind for a in started] == ["done"] and started[0].stopped
    assert "showinfo" in dialogs.names()


def test_sound_alert_on_comm_lost_and_silent_on_manual_stop(make_app, fake, dialogs, tmp_path, monkeypatch):
    started = _record_alarms(monkeypatch)
    app = make_app()
    connect(app, fake, tmp_path)
    start(app)
    app.onoff_btn.invoke()  # ［ON/OFF］で止めたときは鳴らさない
    assert pump(app.root, lambda: app.state == "done")
    assert started == []
    # 通信断・エラーで止まったとき、負荷 OFF を確認できなかったときは警告の音
    # （実際に通信断にすると Windows では再接続 10 回に 30 秒以上かかるので、選ぶ処理を直接確かめる）
    import recorder
    from session import SessionResult

    app._start_alarm(SessionResult(end_reason=recorder.END_REASON_COMM_LOST), True)
    app._start_alarm(SessionResult(end_reason=recorder.END_REASON_ERROR), False)
    app._start_alarm(SessionResult(end_reason=recorder.END_REASON_MANUAL), True)
    app._start_alarm(SessionResult(end_reason=recorder.END_REASON_CUTOFF), True)
    assert [a.kind for a in started] == ["alert", "alert", "alert", "alert"]
    assert all(a.stopped for a in started[:-1])  # 新しく鳴らすときは前の音を止める
    app._stop_alarm()
    assert started[-1].stopped


def test_no_sound_when_unchecked(make_app, fake, dialogs, tmp_path, monkeypatch):
    started = _record_alarms(monkeypatch)
    app = make_app()
    app.sound_var.set(False)
    connect(app, fake, tmp_path)
    start(app)
    fake.voltage_override = 2.5
    assert pump(app.root, lambda: app.state == "done")
    assert started == []
