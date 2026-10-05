"""画面（tkinter + matplotlib）。デザインは docs/design/DESIGN.md（案A「本体カラー」）

通信はすべて別スレッドで行い、結果は ui_queue / session.events 経由で画面スレッドが受け取る
（応答待ちで画面が固まらないようにするため）。色・フォントは theme.py、数値の書式は display.py。
"""

from __future__ import annotations

import logging
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

import display
import notify
import paths
import plotting
import power
import recorder
import settings as settings_mod
from display import DASH
from sdl_client import CONNECT_ERROR_MESSAGE, SDLClient, SDLError
from session import Conditions, DischargeSession, StartError
import theme
from theme import C, Fonts, px

log = logging.getLogger("sdl.gui")

APP_TITLE = "SDL_DischargeLogger"
MODEL_MAX_LEN = 40
POLL_MS = 100
GRAPH_MS = 500
IDLE_VOLTAGE_INTERVAL = 1.0
WINDOW_W, WINDOW_H = 1280, 800
SIDE_W = 400
# 接続バーに出す機種の説明（IDN の機種名から）
MODEL_DESCRIPTIONS = {"SDL1020X-E": "SDL1020X-E 200W DC Electronic Load"}

# 画面の状態（DESIGN.md 7 章の 5 状態＋処理中の一時的な状態）
DISCONNECTED = "disconnected"   # 未接続
CONNECTING = "connecting"
IDLE = "idle"                   # 待機（接続済み）
STARTING = "starting"
DISCHARGING = "discharging"     # 放電中
RECONNECTING = "reconnecting"   # 再接続中
STOPPING = "stopping"
DONE = "done"                   # 完了

# グラフの横軸の幅の選択肢（自由入力もできる：「90分」「2.5時間」「4」(時間) など）
SPAN_AUTO = "自動"
SPAN_PRESETS = [SPAN_AUTO, "30分", "1時間", "2時間", "3時間", "6時間", "12時間", "24時間"]
SPAN_RANGE_H = (10 / 60, 100.0)

UNSAVED_START = "保存していないデータがあります。破棄して放電を開始しますか？"
UNSAVED_DISCONNECT = "保存していないデータがあります。破棄して切断しますか？"
UNSAVED_CLOSE = "保存していないデータがあります。保存して終了しますか？"
UNSAVED_CLOSE_DETAIL = "はい: 保存して終了\nいいえ: 破棄して終了\nキャンセル: 戻る"
CLOSE_QUESTION = "放電中です。停止・保存して終了しますか？"
# 設定の間違いを防ぐため、放電電流がこの値以上・終止電圧がこの値以下なら開始前に確認する
CONFIRM_CURRENT_A = 2.0
CONFIRM_CUTOFF_V = 3.0
VON_OFFSET_V = 0.1  # ［Von を自動設定］のとき Von = 終止電圧 − 0.1 V
CLOSE_DETAIL = "はい: 保存して終了\nいいえ: 破棄して終了（データは残りません）\nキャンセル: 放電を続ける"


class InputError(Exception):
    pass


def parse_number(text: str, name: str, lo: float, hi: float, unit: str, digits: int) -> float:
    try:
        value = float(text.strip())
    except ValueError:
        value = None
    if value is None or not (lo <= value <= hi):
        raise InputError(f"{name}は {lo:.{digits}f}〜{hi:.{digits}f} {unit} の範囲で入力してください")
    return value


def parse_span(text: str) -> float | None:
    """横軸の幅の入力 → 時間（「自動」は None）。「90分」「2.5時間」「2h」、単位なしは時間"""
    t = text.strip().replace("　", "").lower()
    if t in ("", SPAN_AUTO, "auto"):
        return None
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(分|min|m|時間|h|hr)?", t)
    if not m:
        raise InputError("横軸の幅は「自動」、または 10分〜100時間 で入力してください（例: 90分、2.5時間）")
    value = float(m.group(1))
    hours = value / 60 if m.group(2) in ("分", "min", "m") else value
    if not SPAN_RANGE_H[0] - 1e-9 <= hours <= SPAN_RANGE_H[1]:
        raise InputError("横軸の幅は「自動」、または 10分〜100時間 で入力してください（例: 90分、2.5時間）")
    return hours


def condition_warnings(cond) -> list[str]:
    """開始前に確認したい設定（放電電流 2A 以上・終止電圧 3.0V 以下）"""
    items = []
    if cond.current >= CONFIRM_CURRENT_A - 1e-9:
        items.append(f"放電電流が {CONFIRM_CURRENT_A:.1f} A 以上です（設定 {cond.current:.3f} A）")
    if cond.cutoff <= CONFIRM_CUTOFF_V + 1e-9:
        items.append(f"終止電圧が {CONFIRM_CUTOFF_V:.1f} V 以下です（設定 {cond.cutoff:.3f} V）")
    return items


def format_span(hours: float | None) -> str:
    if hours is None:
        return SPAN_AUTO
    minutes = round(hours * 60)
    if hours < 1 and abs(hours * 60 - minutes) < 1e-6:
        return f"{minutes}分"
    return f"{hours:g}時間"


def try_number(text: str, rng: tuple[float, float]) -> float | None:
    try:
        return parse_number(text, "", *rng, "", 3)
    except InputError:
        return None


# ---------------------------------------------------------------------- 部品
class LcdCanvas(FigureCanvasTkAgg):
    """グラフの描画領域。matplotlib が自分で行う表示倍率の補正を止め、画面全体と同じ倍率（theme.SCALE）に
    そろえる（figure の dpi は 100 × theme.SCALE で作る。補正が重なると Windows で 2 重に拡大される）"""

    def _update_device_pixel_ratio(self, event=None):
        return None


class FlatButton:
    """色を指定できるボタン（Windows の ttk はボタンの背景色を無視するため tk で作る）"""

    STYLES = {
        "key": (C["key"], C["text"], C["key-border"], False),
        "primary": (C["accent-blue"], "#ffffff", C["accent-blue"], True),
        "stop": (C["button-stop"], "#ffffff", "#000000", True),
        "danger": (C["danger"], "#ffffff", "#8e1c16", True),
        "akane": (C["akane"], "#ffffff", C["akane-border"], True),
    }

    def __init__(self, parent, fonts: Fonts, text: str, command, style: str = "key", *, height: int = 30,
                 size: int = 13, padx: int = 14, bg_parent: str | None = None):
        self.style = style
        bg, fg, border, bold = self.STYLES[style]
        font = fonts.ui_px(size, bold)
        width = tkfont.Font(font=font).measure(text) + px(padx) * 2 + 2
        self.frame = tk.Frame(parent, width=width, height=px(height), bg=border)
        self.frame.pack_propagate(False)
        self.button = tk.Button(self.frame, text=text, command=command, font=font, relief="flat", bd=0,
                                highlightthickness=0, cursor="hand2")
        self.button.place(x=1, y=1, relwidth=1, width=-2, relheight=1, height=-2)
        self.set_enabled(True)

    def set_enabled(self, on: bool) -> None:
        bg, fg, border, _ = self.STYLES[self.style]
        if not on:
            bg, fg, border = C["button-disabled"], C["text-disabled"], C["key-border"]
        self.button.configure(state="normal" if on else "disabled", bg=bg, fg=fg, activebackground=bg,
                              activeforeground=fg, disabledforeground=C["text-disabled"],
                              cursor="hand2" if on else "arrow")
        self.frame.configure(bg=border)

    @property
    def enabled(self) -> bool:
        return str(self.button["state"]) == "normal"

    def invoke(self):
        return self.button.invoke()


class LoadKey:
    """SDL1020X-E 本体の ON/OFF キーを模したボタン（明るい灰色の縁の中の黒いキー）。負荷 ON の間は外枠と文字が黄緑に光り、キーは緑がかった黒になる"""

    def __init__(self, parent, fonts: Fonts, command, *, width: int = 132, height: int = 44):
        self.frame = tk.Frame(parent, width=px(width), height=px(height), bg=C["onoff-frame"])
        self.frame.pack_propagate(False)
        self.button = tk.Button(self.frame, text="ON/OFF", command=command, font=fonts.ui_px(15, True),
                                relief="flat", bd=0, highlightthickness=0, cursor="hand2")
        self.button.place(x=px(4), y=px(4), relwidth=1, width=-px(8), relheight=1, height=-px(8))
        self._enabled = True
        self._lit = False
        self._apply()

    def set(self, *, enabled: bool, lit: bool) -> None:
        if (enabled, lit) != (self._enabled, self._lit):
            self._enabled, self._lit = enabled, lit
            self._apply()

    def _apply(self) -> None:
        if self._lit:
            bg = C["onoff-key-lit"]
            fg = frame = C["onoff-lit"]
        else:
            bg = C["onoff-key"]
            fg = C["onoff-text"] if self._enabled else C["onoff-off-text"]
            frame = C["onoff-frame"]
        self.button.configure(state="normal" if self._enabled else "disabled", bg=bg, fg=fg, activebackground=bg,
                              activeforeground=fg, disabledforeground=fg, cursor="hand2" if self._enabled else "arrow")
        self.frame.configure(bg=frame)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def lit(self) -> bool:
        return self._lit

    def invoke(self):
        return self.button.invoke()


class BoxEntry:
    """枠つきの入力欄（高さ固定）"""

    def __init__(self, parent, var: tk.StringVar, font, *, width: int = 1, height: int = 30, justify="left"):
        self.frame = tk.Frame(parent, width=px(width), height=px(height), bg=C["input-border"])
        self.frame.pack_propagate(False)
        self.frame.grid_propagate(False)
        inner = tk.Frame(self.frame, bg=C["input-bg"])
        inner.place(x=1, y=1, relwidth=1, width=-2, relheight=1, height=-2)
        self.entry = tk.Entry(inner, textvariable=var, font=font, relief="flat", bd=0, highlightthickness=0,
                              bg=C["input-bg"], fg=C["text"], disabledbackground=C["input-bg"],
                              readonlybackground=C["input-bg"], disabledforeground=C["text"],
                              insertbackground=C["text"], justify=justify)
        self.entry.place(x=px(7), rely=0.5, relwidth=1, width=-px(14), anchor="w")

    def set_state(self, state: str) -> None:
        self.entry.configure(state=state)


class Segmented:
    """セグメント型のラジオボタン（選択中は黒地に黄色文字）"""

    def __init__(self, parent, fonts: Fonts, var: tk.StringVar, options: list[tuple[str, str]]):
        self.var = var
        self.frame = tk.Frame(parent, bg=C["panel"])
        self.items = []
        for k, (label, value) in enumerate(options):
            holder = tk.Frame(self.frame, height=px(34), bg=C["key-border"])
            holder.grid(row=0, column=k, sticky="ew", padx=(0 if k == 0 else px(3), 0 if k == len(options) - 1 else px(3)))
            holder.pack_propagate(False)
            self.frame.columnconfigure(k, weight=1, uniform="seg")
            rb = tk.Radiobutton(holder, text=label, value=value, variable=var, indicatoron=0, relief="flat",
                                offrelief="flat", bd=0, highlightthickness=0, font=fonts.ui_px(13),
                                bg=C["key"], selectcolor=C["bezel"], cursor="hand2")
            rb.place(x=1, y=1, relwidth=1, width=-2, relheight=1, height=-2)
            self.items.append((holder, rb, value))
        var.trace_add("write", lambda *_: self.refresh())
        self.refresh()

    @property
    def radios(self) -> list[tk.Radiobutton]:
        return [rb for _, rb, _ in self.items]

    def refresh(self) -> None:
        current = self.var.get()
        for holder, rb, value in self.items:
            if value == current:
                rb.configure(fg=C["lcd-value"], activeforeground=C["lcd-value"], activebackground=C["bezel"],
                             disabledforeground=C["lcd-value"])
                holder.configure(bg=C["bezel"])
            else:
                rb.configure(fg=C["text"], activeforeground=C["text"], activebackground=C["key"],
                             disabledforeground=C["text-disabled"])
                holder.configure(bg=C["key-border"])

    def set_enabled(self, on: bool) -> None:
        for _, rb, _ in self.items:
            rb.configure(state="normal" if on else "disabled", cursor="hand2" if on else "arrow")


def hline(parent, color: str) -> tk.Frame:
    return tk.Frame(parent, height=px(1), bg=color)


# ---------------------------------------------------------------------- 画面
def screen_scale(root: tk.Tk) -> float:
    """画面の拡大率。Windows の表示倍率（96 dpi = 100%）に合わせ、ウィンドウが画面に収まる範囲で決める。

    環境変数 SDL_LOGGER_SCALE で指定もできる（確認用）。
    """
    override = os.environ.get("SDL_LOGGER_SCALE")
    if override:
        try:
            return max(0.5, min(float(override), 4.0))
        except ValueError:
            pass
    dpi_scale = root.winfo_fpixels("1i") / 96.0
    if dpi_scale < 1.1:  # 100%（Windows の倍率は 125% から。Linux の 100 dpi 前後の画面も 100% 扱い）
        return 1.0
    fit = min(root.winfo_screenwidth() / WINDOW_W, (root.winfo_screenheight() - 80) / WINDOW_H)
    return round(max(1.0, min(dpi_scale, fit)), 2)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        theme.set_scale(screen_scale(root))
        self.fonts = Fonts(root)
        self.settings = settings_mod.load()
        self.state = DISCONNECTED
        self.client: SDLClient | None = None
        self.session: DischargeSession | None = None
        self.ui_queue: queue.Queue = queue.Queue()
        self._monitor_stop: threading.Event | None = None
        self._closing_after_stop = False
        self._graph_key = None
        self.idle_ok = True
        self.reconnect_count = (0, 0)
        self.result = None
        self.values: dict[str, float | None] = dict.fromkeys(("v", "i", "p", "mah", "wh", "elapsed"))
        self.time_span: float | None = None  # グラフの横軸の幅［時間］。None は自動
        self._saving = False                 # ［CSV保存］の保存処理中
        self._alarm: notify.Alarm | None = None  # 放電終了を知らせる音

        root.title(APP_TITLE)
        root.configure(bg=C["chassis"])
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(px(WINDOW_W), sw), min(px(WINDOW_H), max(sh - px(60), 400))
        root.minsize(w, h)
        root.geometry(f"{w}x{h}")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self._on_tk_error
        self._set_window_icon()

        self._build()
        for var in (self.maker_var, self.full_var, self.current_var, self.cutoff_var, self.interval_var):
            var.trace_add("write", lambda *_: self._refresh())
        self._refresh()
        root.after(POLL_MS, self._poll)
        root.after(GRAPH_MS, self._graph_tick)
        root.after_idle(self._after_shown)

    def _set_window_icon(self) -> None:
        """ウィンドウ左上・タスクバーのアイコン（無くても動作は続ける）。

        Windows では .ico をこのウィンドウに直接設定する方法だけを使う。iconphoto と iconbitmap(default=) を
        続けて呼ぶと、後の設定が前のアイコンを捨ててタイトルバーのアイコンが空（白い四角）になるため。
        """
        self._icon_images = []
        try:
            if sys.platform == "win32":
                if paths.icon_ico().exists():
                    self.root.iconbitmap(str(paths.icon_ico()))
                return
            self._icon_images = [tk.PhotoImage(master=self.root, file=str(p)) for p in paths.icon_pngs() if p.exists()]
            if self._icon_images:
                self.root.iconphoto(True, *self._icon_images)
        except tk.TclError as e:
            log.warning("ウィンドウのアイコンを設定できません: %s", e)

    # ================================================================== 組み立て
    def _build(self) -> None:
        outer = tk.Frame(self.root, bg=C["chassis"])
        outer.pack(fill="both", expand=True, padx=px(20), pady=px(20))
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        self._build_connection_bar(outer)
        middle = tk.Frame(outer, bg=C["chassis"])
        middle.grid(row=1, column=0, sticky="nsew", pady=px(16))
        middle.columnconfigure(0, weight=1)
        middle.rowconfigure(0, weight=1)
        self._build_lcd(middle)
        self._build_side(middle)
        self._build_operation_bar(outer)

    def _bar(self, parent, height: int) -> tk.Frame:
        bar = tk.Frame(parent, height=px(height), bg=C["panel"], highlightthickness=1,
                       highlightbackground=C["panel-border"])
        bar.pack_propagate(False)
        return bar

    # ① 接続バー
    def _build_connection_bar(self, parent) -> None:
        f = self.fonts
        bar = self._bar(parent, 48)
        bar.grid(row=0, column=0, sticky="ew")
        tk.Label(bar, text=APP_TITLE, font=f.ui_px(20, True), bg=C["panel"], fg=C["text"]).pack(
            side="left", padx=(px(16), px(14)))
        self.model_label = tk.Label(bar, text="", font=f.ui_px(12), bg=C["panel"], fg=C["text-sub"])
        self.model_label.pack(side="left", pady=(px(6), px(0)))

        # 右側（右から順に置く）
        pill = tk.Frame(bar, bg=C["bezel"], padx=px(10), pady=px(4))
        pill.pack(side="right", padx=(px(10), px(16)))
        self.pill_dot = tk.Canvas(pill, width=px(8), height=px(8), bg=C["bezel"], highlightthickness=0)
        self.pill_dot_item = self.pill_dot.create_oval(0, 0, px(8), px(8), fill=C["idle"], outline="")
        self.pill_dot.pack(side="left", padx=(px(0), px(6)))
        self.pill_var = tk.StringVar(value="未接続")
        tk.Label(pill, textvariable=self.pill_var, font=f.ui_px(12), bg=C["bezel"], fg="#f2f2f2").pack(side="left")
        self.connect_btn = FlatButton(bar, f, "接続", self.on_connect, height=30, size=13, padx=14)
        self.connect_btn.frame.pack(side="right", padx=(px(10), px(0)))
        self.port_var = tk.StringVar(value=str(self.settings.port))
        self.port_box = BoxEntry(bar, self.port_var, f.num_px(14), width=60)
        self.port_box.frame.pack(side="right")
        tk.Label(bar, text="Port", font=f.ui_px(13), bg=C["panel"], fg=C["text-sub"]).pack(side="right", padx=(px(10), px(8)))
        self.host_var = tk.StringVar(value=self.settings.host)
        self.host_box = BoxEntry(bar, self.host_var, f.num_px(14), width=120)
        self.host_box.frame.pack(side="right")
        tk.Label(bar, text="IP", font=f.ui_px(13), bg=C["panel"], fg=C["text-sub"]).pack(side="right", padx=(px(0), px(8)))
        # テスト・旧コードとの互換用
        self.host_entry, self.port_entry = self.host_box.entry, self.port_box.entry

    # ② 液晶パネル
    def _build_lcd(self, parent) -> None:
        f = self.fonts
        bezel = tk.Frame(parent, bg=C["bezel"], padx=px(12), pady=px(12))
        bezel.grid(row=0, column=0, sticky="nsew", padx=(px(0), px(16)))
        lcd = tk.Frame(bezel, bg=C["lcd-bg"], padx=px(16))
        lcd.pack(fill="both", expand=True)
        lcd.columnconfigure(0, weight=1)
        lcd.rowconfigure(4, weight=1)
        bg = C["lcd-bg"]

        # ②-1 ステータス行
        row = tk.Frame(lcd, bg=bg)
        row.grid(row=0, column=0, sticky="ew", pady=(px(10), px(6)))
        tk.Label(row, text="CC", font=f.ui_px(13, True), bg=C["mode-chip"], fg=C["lcd-white"], padx=px(10)).pack(side="left")
        self.load_label = tk.Label(row, text="LOAD OFF", font=f.ui_px(13, True), bg=bg, fg=C["lcd-white"])
        self.load_label.pack(side="left", padx=(px(10), px(0)))
        self.state_label = tk.Label(row, text="未接続", font=f.ui_px(13), bg=bg, fg=C["lcd-label"])
        self.state_label.pack(side="left", padx=(px(10), px(0)))
        self.plan_label = tk.Label(row, text="", font=f.ui_px(13), bg=bg, fg="#c9c9c9", anchor="e")
        self.plan_label.pack(side="right")
        hline(lcd, C["lcd-rule"]).grid(row=1, column=0, sticky="ew")

        # ②-2 数値表示（3 列×2 段、右揃え）
        grid = tk.Frame(lcd, bg=bg)
        grid.grid(row=2, column=0, sticky="ew", pady=px(8), padx=px(4))
        self.value_labels: dict[str, tk.Label] = {}
        for k, key in enumerate(("v", "i", "p", "mah", "wh", "elapsed")):
            grid.columnconfigure(k % 3, weight=1, uniform="val")
            lbl = tk.Label(grid, text=DASH, font=f.num_px(40 if k < 3 else 30), bg=bg, fg=C["lcd-dim"], anchor="e")
            lbl.grid(row=k // 3, column=k % 3, sticky="ew", padx=(px(12) if k % 3 else 0, 0))
            self.value_labels[key] = lbl
        hline(lcd, C["lcd-rule"]).grid(row=3, column=0, sticky="ew")

        # ②-3〜②-5 グラフ
        # 拡大率に合わせて dpi を上げる（線・文字が倍率どおりの解像度で描かれる）
        self.figure = Figure(figsize=(7.7, 3.6), dpi=100 * theme.SCALE, facecolor=bg)
        self.canvas = LcdCanvas(self.figure, master=lcd)
        self.plot = plotting.LcdPlot(self.figure)
        widget = self.canvas.get_tk_widget()
        widget.configure(bg=bg, highlightthickness=0, height=px(300))
        widget.grid(row=4, column=0, sticky="nsew", pady=(px(8), px(0)))
        self.canvas.mpl_connect("resize_event", lambda _e: (self.plot.layout(), self.canvas.draw_idle()))

        # ②-6 条件チップ
        chips = tk.Frame(lcd, bg=bg)
        chips.grid(row=5, column=0, sticky="ew", pady=(px(8), px(12)))
        self.chip_values: list[tk.Label] = []
        for k, name in enumerate(("電流", "終止電圧", "取得周期", "満充電", "メーカー")):
            chips.columnconfigure(k, weight=1, uniform="chip")
            border = C["lcd-accent"] if name == "メーカー" else C["chip-border"]
            cell = tk.Frame(chips, bg=bg, highlightthickness=1, highlightbackground=border, pady=px(4))
            cell.grid(row=0, column=k, sticky="ew", padx=(0 if k == 0 else px(3), 0 if k == 4 else px(3)))
            tk.Label(cell, text=name, font=f.ui_px(12), bg=bg, fg=C["lcd-label"]).pack()
            value = tk.Label(cell, text=DASH, font=f.ui_px(14) if name == "メーカー" else f.num_px(14),
                             bg=bg, fg=C["lcd-white"])
            value.pack()
            self.chip_values.append(value)

    # ③ 試験条件パネル
    def _build_side(self, parent) -> None:
        f = self.fonts
        side = tk.Frame(parent, width=px(SIDE_W), bg=C["panel"], highlightthickness=1,
                        highlightbackground=C["panel-border"])
        side.grid(row=0, column=1, sticky="ns")
        side.pack_propagate(False)
        body = tk.Frame(side, bg=C["panel"])
        body.pack(fill="both", expand=True, padx=px(18), pady=px(16))
        lab = {"bg": C["panel"], "fg": C["text-sub"], "font": f.ui_px(13)}

        tk.Label(body, text="試験条件", font=f.ui_px(15, True), bg=C["panel"], fg=C["text"]).pack(anchor="w")
        self.maker_var = tk.StringVar(value="")
        self.full_var = tk.StringVar(value="")
        self.segments = []
        for label, var, options in (("メーカー", self.maker_var, recorder.MAKERS),
                                    ("満充電電圧", self.full_var, recorder.FULL_VOLTAGES)):
            tk.Label(body, text=label, **lab).pack(anchor="w", pady=(px(12), px(6)))
            seg = Segmented(body, f, var, [("未選択", "")] + [(o, o) for o in options])
            seg.frame.pack(fill="x")
            self.segments.append(seg)
        self.radios = [rb for seg in self.segments for rb in seg.radios]

        form = tk.Frame(body, bg=C["panel"])
        form.pack(fill="x", pady=(px(12), px(0)))
        form.columnconfigure(1, weight=1)
        form.columnconfigure(0, minsize=px(96))
        self.model_var = tk.StringVar(value="")
        # 最大 40 文字（貼り付けで超えたときは 40 文字で切る）
        self.model_var.trace_add("write", lambda *_: len(self.model_var.get()) > MODEL_MAX_LEN
                                 and self.model_var.set(self.model_var.get()[:MODEL_MAX_LEN]))
        self.current_var = tk.StringVar(value=f"{self.settings.current:.3f}")
        self.cutoff_var = tk.StringVar(value=f"{self.settings.cutoff:.3f}")
        self.interval_var = tk.StringVar(value=recorder.format_interval(self.settings.interval))
        self.folder_var = tk.StringVar(value=self.settings.folder)
        rows = [("型番", self.model_var, f.ui_px(13)), ("放電電流 [A]", self.current_var, f.num_px(15)),
                ("終止電圧 [V]", self.cutoff_var, f.num_px(15)), ("取得周期 [s]", self.interval_var, f.num_px(15))]
        self.condition_boxes: list[BoxEntry] = []
        for r, (label, var, font) in enumerate(rows):
            tk.Label(form, text=label, **lab).grid(row=r, column=0, sticky="w", pady=px(4))
            box = BoxEntry(form, var, font)
            box.frame.grid(row=r, column=1, sticky="ew", pady=px(4))
            self.condition_boxes.append(box)
        self.model_entry = self.condition_boxes[0].entry
        r = len(rows)
        tk.Label(form, text="保存先", **lab).grid(row=r, column=0, sticky="w", pady=px(4))
        ff = tk.Frame(form, bg=C["panel"])
        ff.grid(row=r, column=1, sticky="ew", pady=px(4))
        ff.columnconfigure(0, weight=1)
        self.folder_box = BoxEntry(ff, self.folder_var, f.ui_px(12))
        self.folder_box.set_state("readonly")
        self.folder_box.frame.grid(row=0, column=0, sticky="ew")
        self.folder_btn = FlatButton(ff, f, "参照", self.on_browse, height=30, size=12, padx=10)
        self.folder_btn.frame.grid(row=0, column=1, padx=(px(6), px(0)))

        # オプション（チェックで任意に選ぶ。状態は設定ファイルに残す）
        self.keep_awake_var = tk.BooleanVar(value=self.settings.keep_awake)
        self.sound_var = tk.BooleanVar(value=self.settings.sound)
        self.auto_von_var = tk.BooleanVar(value=self.settings.auto_von)
        self.von_text = tk.StringVar()
        opts = tk.Frame(body, bg=C["panel"])
        opts.pack(fill="x", pady=(px(8), px(0)))
        chk = {"bg": C["panel"], "fg": C["text"], "activebackground": C["panel"], "activeforeground": C["text"],
               "selectcolor": C["input-bg"], "font": f.ui_px(12), "highlightthickness": 0, "bd": 0,
               "anchor": "w", "padx": 0, "pady": px(1), "command": self._on_option_changed}
        self.keep_awake_chk = tk.Checkbutton(opts, text="測定中は画面を消さない・ロックしない", variable=self.keep_awake_var, **chk)
        self.sound_chk = tk.Checkbutton(opts, text="放電が終わったら音で知らせる", variable=self.sound_var, **chk)
        self.auto_von_chk = tk.Checkbutton(opts, textvariable=self.von_text, variable=self.auto_von_var, **chk)
        for c in (self.keep_awake_chk, self.sound_chk, self.auto_von_chk):
            c.pack(fill="x")
        self.cutoff_var.trace_add("write", lambda *_: self._update_von_text())
        self._update_von_text()

        memo_head = tk.Frame(body, bg=C["panel"])
        memo_head.pack(fill="x", pady=(px(12), px(6)))
        tk.Label(memo_head, text="備考", **lab).pack(side="left")
        tk.Label(memo_head, text="（放電中も編集可）", font=f.ui_px(11), bg=C["panel"], fg="#4a5056").pack(
            side="left", padx=(px(4), px(0)))
        self.note_text = tk.Text(body, font=f.ui_px(13), wrap="char", undo=True, relief="flat", bd=0, padx=px(8), pady=px(8),
                                 bg=C["memo-bg"], fg=C["text"], insertbackground=C["text"], spacing1=2, spacing3=2,
                                 highlightthickness=1, highlightbackground=C["accent-blue"],
                                 highlightcolor=C["accent-blue"], width=10, height=4)
        self.note_text.pack(fill="both", expand=True)
        self.note_text.bind("<<Modified>>", self._on_note_modified)

    # ④ 操作バー
    def _build_operation_bar(self, parent) -> None:
        f = self.fonts
        bar = self._bar(parent, 64)
        bar.grid(row=2, column=0, sticky="ew")
        opts = {"height": 44, "size": 15, "padx": 22}
        # ON/OFF：待機中・完了後に押すと放電開始（負荷 ON、黄緑に光る）、放電中に押すと停止（保存は［CSV保存］）
        self.onoff_btn = LoadKey(bar, f, self.on_onoff)
        self.csv_btn = FlatButton(bar, f, "CSV保存", self.on_save_csv, "primary", **opts)
        self.graph_btn = FlatButton(bar, f, "グラフ保存", self.on_save_graph, "akane", **opts)
        for k, b in enumerate((self.onoff_btn, self.csv_btn, self.graph_btn)):
            b.frame.pack(side="left", padx=(px(16) if k == 0 else px(12), 0))
        # グラフの横軸（時間軸）の幅。測定中も変えられる
        tk.Label(bar, text="横軸", font=f.ui_px(13), bg=C["panel"], fg=C["text-sub"]).pack(
            side="left", padx=(px(20), px(6)))
        self.root.option_add("*TCombobox*Listbox.font", f.ui_px(14))
        self.span_var = tk.StringVar(value=SPAN_AUTO)
        self.span_box = ttk.Combobox(bar, textvariable=self.span_var, values=SPAN_PRESETS, width=7,
                                     font=f.ui_px(14), state="normal")
        self.span_box.pack(side="left")
        for seq in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
            self.span_box.bind(seq, lambda _e: self.apply_span())
        self.message_var = tk.StringVar(value="")
        self.message_label = tk.Label(bar, textvariable=self.message_var, font=f.ui_px(13), bg=C["panel"],
                                      fg=C["text-sub"], anchor="e", justify="right")
        self.message_label.pack(side="left", fill="both", expand=True, padx=(px(16), px(16)))
        self.message_label.bind("<Configure>", lambda e: self.message_label.configure(wraplength=max(e.width, 100)))

    # ================================================================== 表示の更新
    def message(self, text: str, level: str = "info") -> None:
        """メッセージ欄（最新の 1 件）。level: info / warn / error"""
        color = {"info": C["text-sub"], "warn": C["message-warn"], "error": C["danger"]}[level]
        self.message_var.set(display.fmt_message(text))
        self.message_label.configure(fg=color)

    def _set_state(self, state: str) -> None:
        self.state = state
        self._refresh()

    def _input_conditions(self) -> tuple:
        """入力欄の値（不正なら None）。条件チップ・グラフの表示用"""
        return (try_number(self.current_var.get(), settings_mod.CURRENT_RANGE),
                try_number(self.cutoff_var.get(), settings_mod.CUTOFF_RANGE),
                try_number(self.interval_var.get(), settings_mod.INTERVAL_RANGE),
                self.full_var.get() or None, self.maker_var.get() or None)

    def _shown_conditions(self) -> tuple:
        s = self.session
        if s is not None and self.state in (STARTING, DISCHARGING, RECONNECTING, STOPPING):
            c = s.cond
            return c.current, c.cutoff, c.interval, c.full_voltage, c.maker
        return self._input_conditions()

    def _refresh(self) -> None:
        """状態に合わせて、接続ピル・状態表示・ボタン・入力欄・チップを更新する（DESIGN.md 7 章）"""
        s = self.state
        running = s in (STARTING, DISCHARGING, RECONNECTING, STOPPING)

        # 接続ピル
        if s in (DISCONNECTED, CONNECTING):
            dot, text = C["idle"], "未接続" if s == DISCONNECTED else "接続しています…"
        elif s == RECONNECTING:
            dot, text = C["warn"], f"再接続中 ({self.reconnect_count[0]}/{self.reconnect_count[1]})"
        elif s in (IDLE, DONE) and not self.idle_ok:
            dot, text = C["warn"], "応答なし"
        else:
            dot, text = C["ok"], "接続中"
        self.pill_dot.itemconfigure(self.pill_dot_item, fill=dot)
        self.pill_var.set(text)

        # 接続ボタン・IP・Port
        self.connect_btn.button.configure(text="接続" if s in (DISCONNECTED, CONNECTING) else "切断")
        self.connect_btn.set_enabled(s in (DISCONNECTED, IDLE, DONE))
        for box in (self.host_box, self.port_box):
            box.set_state("normal" if s == DISCONNECTED else "readonly")

        # ②-1 ステータス行
        load_on = s in (DISCHARGING, RECONNECTING, STOPPING)
        self.load_label.configure(text="LOAD ON" if load_on else "LOAD OFF")
        state_text, state_color = {
            DISCONNECTED: ("未接続", C["lcd-label"]),
            CONNECTING: ("未接続", C["lcd-label"]),
            IDLE: ("待機中", C["lcd-white"]) if self.idle_ok else ("応答なし", C["lcd-warn"]),
            STARTING: ("開始しています…", C["lcd-white"]),
            DISCHARGING: ("● 放電中", C["lcd-value"]),
            RECONNECTING: ("通信断 — 再接続中", C["lcd-warn"]),
            STOPPING: ("停止しています…", C["lcd-white"]),
            DONE: (f"完了: {self.result.end_reason}" if self.result else "完了", C["lcd-white"]),
        }[s]
        self.state_label.configure(text=state_text, fg=state_color)
        self.plan_label.configure(text=self._plan_text())

        # ③ 入力（放電中は備考のみ編集可）
        editable = not running
        for seg in self.segments:
            seg.set_enabled(editable)
        for box in self.condition_boxes:
            box.set_state("normal" if editable else "disabled")
        self.folder_btn.set_enabled(editable)
        self.auto_von_chk.configure(state="normal" if editable else "disabled")  # Von は開始時に設定する
        # 測定中は画面オフ・ロック・スリープを止める（チェックしたとき）
        power.keep_awake(self.keep_awake_var.get() and running)

        # ④ ボタン
        has_data = self.session is not None and bool(self.session.snapshot()[0])
        self.onoff_btn.set(enabled=s in (IDLE, DONE, DISCHARGING, RECONNECTING) and not self._saving,
                           lit=s in (DISCHARGING, RECONNECTING, STOPPING))
        # CSV保存は止めたあと何度でも押せる（自動保存のあとも。2 回目からは <ベース名>_2.csv …）
        self.csv_btn.set_enabled(s == DONE and self.session is not None and self.session.can_save
                                 and not self._saving)
        # グラフ保存も CSV保存と同じく、負荷を OFF にしてから押せる（放電中・再接続中は押せない）
        self.graph_btn.set_enabled((s == DONE or (s == IDLE and has_data)) and not self._saving)

        # ②-6 条件チップ
        for label, value in zip(self.chip_values, display.chip_values(*self._shown_conditions())):
            label.configure(text=value, fg=C["lcd-dim"] if value == DASH else C["lcd-white"])
        self._render_values()

    def _plan_text(self) -> str:
        s = self.session
        if self.state in (DISCHARGING, RECONNECTING, STOPPING) and s is not None and s.base_name:
            return f"保存予定: {Path(s.run_dir.name) / (s.base_name + '.csv')}"
        if self.state == DONE and self.result is not None:
            r = self.result
            if r.pending:
                return "未保存（［CSV保存］で保存）"
            if r.discarded:
                return "破棄しました"
            if r.csv_path:
                return f"保存済み: {Path(r.csv_path.parent.name) / r.csv_path.name}"
            return "保存できませんでした（一時ファイルを残しました）"
        pattern = display.planned_name_pattern(self.full_var.get() or None, self.maker_var.get() or None)
        return f"保存予定: {pattern}"  # 日時は開始時に決まる

    def _render_values(self) -> None:
        """②-2 数値表示。未取得は ---（lcd-dim）、再接続中は最後の値を lcd-dim で"""
        fmt = {"v": display.fmt_voltage, "i": display.fmt_current, "p": display.fmt_power,
               "mah": display.fmt_mah, "wh": display.fmt_wh, "elapsed": display.fmt_elapsed}
        dim = self.state == RECONNECTING
        for key, label in self.value_labels.items():
            value = self.values[key]
            if value is None:
                label.configure(text=DASH, fg=C["lcd-dim"])
            else:
                normal = C["lcd-white"] if key == "elapsed" else C["lcd-value"]
                label.configure(text=fmt[key](value), fg=C["lcd-dim"] if dim else normal)

    def _set_values(self, **values) -> None:
        self.values.update(values)
        self._render_values()

    # ================================================================== 接続
    def on_connect(self) -> None:
        if self.state in (IDLE, DONE):
            self.disconnect()
            return
        if self.state != DISCONNECTED:
            return
        host = self.host_var.get().strip()
        try:
            port = int(self.port_var.get().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_TITLE, "ポートは 1〜65535 の整数で入力してください")
            return
        if not host:
            messagebox.showerror(APP_TITLE, "IP アドレスを入力してください")
            return
        client = SDLClient(host, port)
        self._set_state(CONNECTING)
        self._run_bg(client.connect, lambda idn: self._on_connected(client, idn), self._on_connect_failed)

    def _on_connected(self, client: SDLClient, idn: str) -> None:
        self.client = client
        self.idle_ok = True
        self.settings.host, self.settings.port = client.host, client.port
        parts = [p.strip() for p in idn.split(",")]
        model = parts[1] if len(parts) > 1 else idn
        self.model_label.configure(text=MODEL_DESCRIPTIONS.get(model, model))
        self.message(f"接続しました: {idn}")
        self._set_state(IDLE)
        self._start_monitor()

    def _on_connect_failed(self, exc: BaseException) -> None:
        self._set_state(DISCONNECTED)
        text = str(exc) if isinstance(exc, SDLError) else CONNECT_ERROR_MESSAGE
        self.message(text, "error")
        messagebox.showerror(APP_TITLE, text)

    @property
    def pending(self) -> bool:
        """ON/OFF で止めたまま、まだ保存していないデータがある"""
        return self.result is not None and self.result.pending

    def _confirm_conditions(self, cond: Conditions) -> bool:
        """放電電流が大きい・終止電圧が低いときは、設定を間違えていないか確認する。続けてよければ True"""
        items = condition_warnings(cond)
        if not items:
            return True
        text = "設定を確認してください。\n\n" + "\n".join(f"・{t}" for t in items) + "\n\nこの設定で放電を開始しますか？"
        if messagebox.askyesno(APP_TITLE, text, icon="warning", default="no"):
            log.info("確認のうえ開始: %s", " / ".join(items))
            return True
        self.message("放電を開始しませんでした（設定を確認してください）", "warn")
        return False

    def _confirm_discard(self, question: str) -> bool:
        """保存していないデータがあれば破棄してよいか聞き、よければ破棄する。続けてよければ True"""
        if not self.pending or self.session is None:
            return True
        if not messagebox.askyesno(APP_TITLE, question, icon="warning", default="no"):
            return False
        self.session.discard_pending()
        self.message("保存していないデータを破棄しました")
        return True

    def disconnect(self) -> None:
        if self._saving or not self._confirm_discard(UNSAVED_DISCONNECT):
            return
        self._stop_monitor()
        client, self.client = self.client, None
        if client is not None:
            threading.Thread(target=client.close, daemon=True).start()
        self.session = None
        self.result = None
        self._set_values(**dict.fromkeys(self.values))
        self.model_label.configure(text="")
        self._set_state(DISCONNECTED)
        self._draw_graph(force=True)
        self.message("切断しました")

    # 待機中の V/I/P 表示（1 秒周期）
    def _start_monitor(self) -> None:
        self._stop_monitor()
        stop = threading.Event()
        self._monitor_stop = stop
        threading.Thread(target=self._monitor_loop, args=(self.client, stop), name="idle-monitor",
                         daemon=True).start()

    def _stop_monitor(self) -> None:
        if self._monitor_stop is not None:
            self._monitor_stop.set()
            self._monitor_stop = None

    def _monitor_loop(self, client: SDLClient, stop: threading.Event) -> None:
        while not stop.is_set():
            with client._lock:  # 停止の判定と通信を一体にする（放電開始と入れ違いにならないように）
                if stop.is_set():
                    break
                try:
                    if not client.connected:
                        client.reconnect()
                    m = client.measure()
                    self.ui_queue.put((self._on_idle_measure, (stop, m)))
                except SDLError as e:
                    self.ui_queue.put((self._on_idle_error, (stop, str(e))))
            stop.wait(IDLE_VOLTAGE_INTERVAL)

    def _on_idle_measure(self, stop, m) -> None:
        if stop is not self._monitor_stop or self.state not in (IDLE, DONE):
            return
        if not self.idle_ok:
            self.idle_ok = True
            self._refresh()
        if self.state == IDLE:  # 完了の状態では最終値のまま止めておく
            self._set_values(v=m.voltage, i=m.current, p=m.power)

    def _on_idle_error(self, stop, text: str) -> None:
        if stop is not self._monitor_stop or self.state not in (IDLE, DONE):
            return
        if self.idle_ok:
            log.warning("待機中に応答なし: %s", text)
        self.idle_ok = False
        if self.state == IDLE:
            self._set_values(v=None, i=None, p=None)
        self._refresh()

    # ================================================================== 放電
    def read_conditions(self) -> Conditions:
        current = parse_number(self.current_var.get(), "放電電流", *settings_mod.CURRENT_RANGE, "A", 3)
        cutoff = parse_number(self.cutoff_var.get(), "終止電圧", *settings_mod.CUTOFF_RANGE, "V", 3)
        interval = parse_number(self.interval_var.get(), "取得周期", *settings_mod.INTERVAL_RANGE, "秒", 1)
        von = round(cutoff - VON_OFFSET_V, 3) if self.auto_von_var.get() else None
        return Conditions(maker=self.maker_var.get() or None, full_voltage=self.full_var.get() or None,
                          model=self.model_var.get().strip(), current=current, cutoff=cutoff, interval=interval,
                          von=von)

    def _update_von_text(self) -> None:
        cutoff = try_number(self.cutoff_var.get(), settings_mod.CUTOFF_RANGE)
        detail = f" = {cutoff - VON_OFFSET_V:.3f} V" if cutoff is not None else ""
        self.von_text.set(f"Von を自動設定（終止電圧 − {VON_OFFSET_V:.1f} V{detail}）")

    def _on_option_changed(self) -> None:
        self._refresh()

    def note(self) -> str:
        return self.note_text.get("1.0", "end-1c")

    def on_onoff(self) -> None:
        """ON/OFF キー：放電中なら停止（保存は［CSV保存］）、待機中・完了後なら放電開始"""
        if self.state in (DISCHARGING, RECONNECTING):
            self._request_stop(save=False, keep=True)
        else:
            self.on_start()

    def on_start(self) -> None:
        if self.state not in (IDLE, DONE) or self.client is None or self._saving:
            return
        self._stop_alarm()
        try:
            cond = self.read_conditions()
        except InputError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        if not self._confirm_conditions(cond):
            return
        if not self._confirm_discard(UNSAVED_START):
            return
        self.current_var.set(f"{cond.current:.3f}")
        self.cutoff_var.set(f"{cond.cutoff:.3f}")
        self.interval_var.set(recorder.format_interval(cond.interval))
        self._remember_settings()
        settings_mod.save(self.settings)

        self._stop_monitor()
        # 「放電開始」で前回のグラフと数値をクリアする
        session = DischargeSession(self.client, Path(self.folder_var.get()), cond, note=self.note())
        self.session = session
        self.result = None
        self._set_values(**dict.fromkeys(self.values))
        self._set_state(STARTING)
        self._draw_graph(force=True)
        self._run_bg(session.start, lambda _r: self._on_started(session), self._on_start_failed)

    def _on_started(self, session: DischargeSession) -> None:
        session.note = self.note()
        self._graph_key = None
        self._set_state(DISCHARGING)
        c = session.cond
        von = f" / Von {c.von:.3f} V" if c.von is not None else ""
        self.message(f"放電開始（{c.current:.3f} A / 終止 {c.cutoff:.3f} V{von}）")

    def _on_start_failed(self, exc: BaseException) -> None:
        self.session = None
        self._set_state(IDLE)
        self._draw_graph(force=True)
        self._start_monitor()
        if not isinstance(exc, (StartError, SDLError)):
            log.error("放電開始で想定外のエラー", exc_info=exc)
        text = str(exc) if isinstance(exc, (StartError, SDLError)) else f"放電を開始できません: {exc}"
        self.message(text, "error")
        messagebox.showwarning(APP_TITLE, text)

    def _on_note_modified(self, _event=None) -> None:
        if self.session is not None and self.session.running:
            self.session.note = self.note()
        self.note_text.edit_modified(False)

    def _request_stop(self, save: bool, keep: bool = False) -> None:
        if self.state not in (DISCHARGING, RECONNECTING) or self.session is None:
            return
        self.session.note = self.note()
        self.session.request_stop(save=save, keep=keep)
        self._set_state(STOPPING)

    def on_save_csv(self) -> None:
        """止めたデータを保存する（<ベース名>.csv と .png）。備考は押した時点の内容。

        保存済み（自動保存を含む）でも押せて、そのときは上書きせずに <ベース名>_2.csv, _3 … として保存する
        """
        if self.state != DONE or self._saving or self.session is None or not self.session.can_save:
            return
        session = self.session
        session.note = self.note()
        self._saving = True
        self._refresh()
        self._run_bg(session.save, self._on_csv_saved, self._on_csv_save_failed)

    def _on_csv_saved(self, result) -> None:
        self._saving = False
        self.result = result
        self._refresh()
        if not result.save_ok:  # CSV が作れなかった（データは残っているので、もう一度押せる）
            self.message(result.error or "保存できませんでした", "error")
            messagebox.showerror(APP_TITLE, result.error or "保存できませんでした")
            return
        if result.error:  # CSV は保存できたがグラフ画像が保存できなかった
            self.message(f"保存しました: {result.csv_path} / {result.error}", "error")
            messagebox.showerror(APP_TITLE, result.error)
            return
        self.message(f"保存しました: {result.csv_path}")

    def _on_csv_save_failed(self, exc: BaseException) -> None:
        self._saving = False
        self._refresh()
        log.error("CSV 保存で想定外のエラー", exc_info=exc)
        self.message(f"保存できませんでした: {exc}", "error")
        messagebox.showerror(APP_TITLE, f"保存できませんでした: {exc}")

    def _on_sample(self, s: recorder.Sample) -> None:
        if self.state == RECONNECTING:
            self._set_state(DISCHARGING)
        self._set_values(v=s.voltage, i=s.current, p=s.power, mah=s.mah, wh=s.wh, elapsed=s.elapsed)

    def _on_reconnecting(self, attempt: int, total: int) -> None:
        self.reconnect_count = (attempt, total)
        if self.state in (DISCHARGING, RECONNECTING):
            if self.state == DISCHARGING:
                self.message("通信が途切れました。再接続しています", "warn")
            self._set_state(RECONNECTING)

    def _on_reconnected(self) -> None:
        if self.state == RECONNECTING:
            self._set_state(DISCHARGING)
            self.message("再接続しました。記録を続けます")

    def _on_finished(self, result) -> None:
        session = self.session
        self.result = result
        if session is not None and session.latest is not None:
            s = session.latest
            self._set_values(v=s.voltage, i=s.current, p=s.power, mah=s.mah, wh=s.wh, elapsed=s.elapsed)
        self.idle_ok = True
        self._set_state(DONE)
        self._draw_graph(force=True)
        # 通信断で終わったときも接続中の扱いのまま、待機中の監視で再接続を試みる
        if self.client is not None:
            self._start_monitor()

        problems = [m for m in ([result.error] if result.error else []) + result.messages if m]
        if result.pending:
            text, level = "停止しました。［CSV保存］で保存できます", "info"
        elif result.discarded:
            text, level = "停止しました。データは破棄しました", "info"
        elif result.csv_path is None:
            text, level = result.error or "保存できませんでした", "error"
        else:
            head = {recorder.END_REASON_CUTOFF: "終止電圧に到達しました。",
                    recorder.END_REASON_MANUAL: "停止しました。"}.get(result.end_reason, f"{result.end_reason}で停止しました。")
            text, level = f"{head}保存しました: {result.csv_path}", "info"
        if result.messages and level == "info":
            level = "error"
        full = "\n".join([text] + [m for m in problems if m != text])
        self.message(full.replace("\n", " / "), level)

        if self._closing_after_stop:
            self._quit()
            return
        self._start_alarm(result, bool(problems))
        try:
            if problems:
                messagebox.showerror(APP_TITLE, full)
            elif result.end_reason == recorder.END_REASON_CUTOFF:
                messagebox.showinfo(APP_TITLE, full)
        finally:
            self._stop_alarm()  # ダイアログを閉じたら止める

    def _start_alarm(self, result, problems: bool) -> None:
        """［放電が終わったら音で知らせる］のとき、完了は完了の音、通信断・エラーは警告の音をくり返す"""
        if not self.sound_var.get():
            return
        if result.end_reason == recorder.END_REASON_CUTOFF and not problems:
            kind = notify.DONE
        elif problems or result.end_reason != recorder.END_REASON_MANUAL:
            kind = notify.ALERT
        else:
            return  # ［ON/OFF］で止めたときは鳴らさない
        self._stop_alarm()
        self._alarm = notify.start_alarm(kind)

    def _stop_alarm(self) -> None:
        if self._alarm is not None:
            self._alarm.stop()
            self._alarm = None

    # ================================================================== グラフ
    def _graph_tick(self) -> None:
        try:
            self._draw_graph()
            s = self.session
            if self.state == DISCHARGING and s is not None and s.start_monotonic is not None:
                self._set_values(elapsed=time.monotonic() - s.start_monotonic)
        finally:
            try:
                self.root.after(GRAPH_MS, self._graph_tick)
            except tk.TclError:
                pass  # ウィンドウを閉じた後

    def _graph_source(self):
        """(経過時間, 電圧, 電流, 終止電圧, 放電電流設定)。放電前は入力中の終止電圧の線だけ"""
        s = self.session
        if s is not None and self.state != DISCONNECTED:
            t, v, i = s.snapshot()
            return t, v, i, s.cond.cutoff, s.cond.current
        current, cutoff, *_ = self._input_conditions()
        return [], [], [], cutoff, current

    def _draw_graph(self, force: bool = False) -> None:
        t, v, i, cutoff, current = self._graph_source()
        key = (id(self.session), len(t), cutoff, current, self.time_span)
        if not force and key == self._graph_key:
            return
        self._graph_key = key
        self.plot.update(t, v, i, cutoff, current, self.time_span)
        self.canvas.draw_idle()

    def apply_span(self) -> None:
        """横軸の幅の入力を反映する（不正な入力なら前の値に戻す）"""
        try:
            span = parse_span(self.span_var.get())
        except InputError as e:
            self.span_var.set(format_span(self.time_span))
            self.message(str(e), "warn")
            return
        self.span_var.set(format_span(span))
        if span != self.time_span:
            self.time_span = span
            self._draw_graph(force=True)

    def on_save_graph(self) -> None:
        t, v, i, cutoff, current = self._graph_source()
        folder = Path(self.folder_var.get())
        now = datetime.now()
        s = self.session
        if s is not None and s.base_name:
            path = s.run_dir / f"{s.base_name}_{now:%H%M%S}.png"  # 放電の出力フォルダにまとめる
            lines = s.title_lines()
        else:
            path = folder / f"graph_{now:%Y%m%d_%H%M%S}.png"
            lines = plotting.png_title_lines(None, self.model_var.get().strip(), current, cutoff)
        try:
            plotting.render_png(path, t, v, i, cutoff, current, lines)
        except Exception as e:  # noqa: BLE001
            log.exception("グラフを保存できません")
            self.message(f"グラフを保存できませんでした: {path}", "error")
            messagebox.showerror(APP_TITLE, f"グラフを保存できませんでした: {path}\n{e}")
            return
        self.message(f"グラフを保存しました: {path}")
        log.info("グラフ保存 %s", path)

    # ================================================================== 保存先
    def on_browse(self) -> None:
        folder = filedialog.askdirectory(parent=self.root, title="保存先フォルダ",
                                         initialdir=self.folder_var.get() or None, mustexist=True)
        if folder:
            self.folder_var.set(os.path.normpath(folder))
            self.settings.folder = self.folder_var.get()
            self._check_partials()

    def _check_partials(self) -> None:
        """保存先に前回の一時ファイルが残っていれば知らせる（自動処理はしない）"""
        found = recorder.find_partial_files(Path(self.folder_var.get()))
        if not found:
            return
        log.warning("前回の一時ファイルが残っています: %s", ", ".join(str(p) for p in found))
        more = f" ほか {len(found) - 1} 件" if len(found) > 1 else ""
        self.message(f"前回の記録が途中で終了しています: {found[0]}{more}", "warn")
        messagebox.showwarning(APP_TITLE, "前回の記録が途中で終了しています:\n" + "\n".join(str(p) for p in found))

    # ================================================================== 終了・その他
    def _remember_settings(self) -> None:
        s = self.settings
        host = self.host_var.get().strip()
        if host:
            s.host = host
        try:
            port = int(self.port_var.get())
            if 1 <= port <= 65535:
                s.port = port
        except ValueError:
            pass
        if self.folder_var.get():
            s.folder = self.folder_var.get()
        for name, var, rng in (("current", self.current_var, settings_mod.CURRENT_RANGE),
                               ("cutoff", self.cutoff_var, settings_mod.CUTOFF_RANGE),
                               ("interval", self.interval_var, settings_mod.INTERVAL_RANGE)):
            value = try_number(var.get(), rng)
            if value is not None:
                setattr(s, name, value)
        s.keep_awake = self.keep_awake_var.get()
        s.sound = self.sound_var.get()
        s.auto_von = self.auto_von_var.get()

    def ask_close_choice(self) -> str | None:
        """放電中に閉じようとしたとき。'save'（保存して終了）/ 'discard'（破棄して終了）/ None（キャンセル）"""
        answer = messagebox.askyesnocancel(APP_TITLE, CLOSE_QUESTION, detail=CLOSE_DETAIL, icon="warning")
        return None if answer is None else ("save" if answer else "discard")

    def on_close(self) -> None:
        if self.state in (DISCHARGING, RECONNECTING):
            choice = self.ask_close_choice()
            if choice is None or self.state not in (DISCHARGING, RECONNECTING):
                return
            self._closing_after_stop = True
            self._request_stop(save=(choice == "save"))
            return
        if self.state in (STARTING, STOPPING) or self._saving:
            if self.state == STOPPING:
                self._closing_after_stop = True
            self.message("処理中です。終わるまでお待ちください", "warn")
            return
        if self.pending and self.session is not None:
            answer = messagebox.askyesnocancel(APP_TITLE, UNSAVED_CLOSE, detail=UNSAVED_CLOSE_DETAIL, icon="warning")
            if answer is None:
                return
            if answer:
                self.session.note = self.note()
                result = self.session.save()
                if not result.save_ok:
                    messagebox.showerror(APP_TITLE, result.error or "保存できませんでした")
                    return
            else:
                self.session.discard_pending()
        self._quit()

    def _quit(self) -> None:
        self._stop_alarm()
        power.keep_awake(False)
        self._remember_settings()
        settings_mod.save(self.settings)
        self._stop_monitor()
        if self.client is not None:
            self.client.close()
        log.info("終了")
        self.root.destroy()

    def _run_bg(self, func, on_ok, on_err) -> None:
        def worker():
            try:
                result = func()
            except BaseException as e:  # noqa: BLE001
                self.ui_queue.put((on_err, (e,)))
            else:
                self.ui_queue.put((on_ok, (result,)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                try:
                    func, args = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                func(*args)
            s = self.session
            while s is not None and s is self.session:
                try:
                    kind, payload = s.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "sample":
                    self._on_sample(payload)
                elif kind == "status":
                    pass  # 終止電圧到達などは finished でまとめて表示する
                elif kind == "reconnecting":
                    self._on_reconnecting(*payload)
                elif kind == "reconnected":
                    self._on_reconnected()
                elif kind == "finished":
                    self._on_finished(payload)
        finally:
            try:
                self.root.after(POLL_MS, self._poll)
            except tk.TclError:
                pass  # ウィンドウを閉じた後

    def _after_shown(self) -> None:
        ready = os.environ.get("SDL_LOGGER_READY_FILE")
        if ready:
            # ビルドの確認（起動時間の測定）用：ウィンドウが表示されたことを知らせる
            self.root.update_idletasks()
            try:
                Path(ready).write_text("ready", encoding="utf-8")
            except OSError:
                pass
        self._check_partials()

    def _on_tk_error(self, exc_type, exc, tb) -> None:
        log.error("画面の処理で想定外のエラー", exc_info=(exc_type, exc, tb))
        try:
            messagebox.showerror(APP_TITLE, f"想定外のエラーが発生しました:\n{exc}\n\n詳細は sdl_logger.log を参照してください")
        except tk.TclError:
            pass
