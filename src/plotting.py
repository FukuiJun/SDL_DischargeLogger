"""グラフ（上段: 電圧、下段: 電流、横軸: 経過時間）の描画と PNG 保存

画面は液晶スタイル（黒背景、docs/design/DESIGN.md 10.1）、PNG は報告書向けの白背景（10.2）。
pyplot は使わず matplotlib.figure.Figure を直接使う（PNG は画面とは別の Figure で描くので、
測定スレッドから保存しても画面の描画とぶつからない）。横軸のデータの単位は「時間」。
"""

from __future__ import annotations

import bisect
import math
from pathlib import Path
from typing import Sequence

import matplotlib
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FixedLocator, FuncFormatter

from theme import C, MPL_NUM_FAMILIES, MPL_UI_FAMILIES

MAX_PLOT_POINTS = 5000
PNG_SIZE_INCH = (16, 10)  # 100 dpi で 1600×1000 px
PNG_DPI = 100
CUTOFF_DASH = (0, (6, 5))  # 線 6・間隔 5
MARKER_DASH = (0, (2, 3))  # 負荷を ON・OFF にした時刻の縦線（オフセットを入れたとき）

# 画面のグラフの余白 [px]
LCD_LEFT = 40
LCD_RIGHT = 2
LCD_TOP = 22          # 電圧グラフの上（見出し「電圧 [V]」の分）
LCD_BOTTOM = 20       # 時間軸の目盛り数字の分
LCD_GAP = 28          # 電圧グラフと電流グラフの間（見出し「電流 [A]」の分）
LCD_CURRENT_H = 92    # 電流グラフの高さ

_fonts: dict[str, list[str]] = {}


def setup_fonts() -> None:
    """日本語（BIZ UDPGothic）と数値（Consolas）のフォントを入っているものから選ぶ"""
    if _fonts:
        return
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    ui = [n for n in MPL_UI_FAMILIES if n in available] + ["DejaVu Sans"]
    num = [n for n in MPL_NUM_FAMILIES if n in available] + ui
    _fonts["ui"] = ui
    _fonts["num"] = num
    matplotlib.rcParams["font.family"] = "sans-serif"
    matplotlib.rcParams["font.sans-serif"] = ui
    matplotlib.rcParams["axes.unicode_minus"] = False


def px_to_pt(px: float, dpi: float) -> float:
    return px * 72.0 / dpi


# ---- 間引き・軸の範囲 ----
def decimate(xs: Sequence[float], ys: Sequence[float], max_points: int = MAX_PLOT_POINTS):
    """表示点数を max_points 以下に間引く（等間隔に抜き出し、最後の点は必ず残す）"""
    n = len(xs)
    if n <= max_points:
        return list(xs), list(ys)
    step = math.ceil(n / (max_points - 1))
    out_x, out_y = list(xs[::step]), list(ys[::step])
    if (n - 1) % step:
        out_x.append(xs[n - 1])
        out_y.append(ys[n - 1])
    return out_x, out_y


def time_axis(max_elapsed_s: float) -> tuple[float, float]:
    """横軸の (上限, 目盛り間隔)［時間］。

    上限は max(1 時間, 経過時間 × 1.2) を 30 分単位で切り上げ。目盛りは 30 分ごと、
    6 時間超は 1 時間、12 時間超は 2 時間、24 時間超は 4 時間ごと。
    """
    hours = max(1.0, max_elapsed_s / 3600 * 1.2)
    xmax = math.ceil(hours * 2 - 1e-9) / 2
    if xmax > 24:
        step = 4.0
    elif xmax > 12:
        step = 2.0
    elif xmax > 6:
        step = 1.0
    else:
        step = 0.5
    if step > 0.5:
        xmax = math.ceil(xmax / step - 1e-9) * step  # 最後の目盛りが右端に来るようにする
    return xmax, step


# 測定時間に合わせた横軸（fit_axis）で使う目盛り間隔の候補［秒］
FIT_STEPS_S = (10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 21600, 43200)
FIT_MAX_TICKS = 8


def fit_axis(duration_s: float) -> tuple[float, float]:
    """測定時間にちょうど合う横軸の (上限, 目盛り間隔)［時間］。放電が終わったあとの画面と画像に使う。

    少し余白（5%、最低 1 分）を足し、目盛りの区切りが 8 以下になる最小の間隔を選んで、上限をその倍数にする。
    例: 2 分 → 0〜2:30（30 秒刻み）、25 分 → 0〜30 分（5 分刻み）
    """
    raw = max(duration_s * 1.05, 60.0)
    step = next((st for st in FIT_STEPS_S if raw / st <= FIT_MAX_TICKS + 1e-9), FIT_STEPS_S[-1])
    xmax = math.ceil(raw / step - 1e-9) * step
    return xmax / 3600, step / 3600


def tick_step(span_hours: float) -> float:
    """横軸の幅［時間］に対する目盛りの間隔［時間］"""
    if span_hours > 24:
        return 4.0
    if span_hours > 12:
        return 2.0
    if span_hours > 6:
        return 1.0
    if span_hours > 1:
        return 0.5
    if span_hours > 0.5:
        return 0.25
    if span_hours > 10 / 60:
        return 5 / 60   # 5 分
    if span_hours > 5 / 60:
        return 1 / 60   # 1 分
    return 30 / 3600    # 30 秒


def time_window(max_elapsed_s: float, span_hours: float | None = None, *,
                fit: bool = False) -> tuple[float, float, float]:
    """横軸の (左端, 右端, 目盛り間隔)［時間］。

    span_hours が None なら自動。fit=True なら測定時間に合わせ（fit_axis：終わったあとの画面・画像）、
    そうでなければ伸びていく分の余裕を持たせる（time_axis：測定中）。
    span_hours を指定したときはその幅で固定し、経過時間が幅を超えたら最新の点が右端に来るように送る。
    """
    if span_hours is None:
        xmax, step = fit_axis(max_elapsed_s) if fit else time_axis(max_elapsed_s)
        return 0.0, xmax, step
    t = max_elapsed_s / 3600
    step = tick_step(span_hours)
    if t <= span_hours:
        return 0.0, span_hours, step
    return t - span_hours, t, step


def format_hmm(hours: float) -> str:
    total = int(round(hours * 60))
    return f"{total // 60}:{total % 60:02d}"


def uses_seconds(step_hours: float) -> bool:
    """目盛り間隔が 1 分未満なら、目盛りを m:ss（分:秒）で書く"""
    return step_hours < 1 / 60 - 1e-9


def format_time(hours: float, step_hours: float) -> str:
    """横軸の目盛り。間隔が 1 分以上なら h:mm、1 分未満なら m:ss"""
    if not uses_seconds(step_hours):
        return format_hmm(hours)
    total = int(round(hours * 3600))
    return f"{total // 60}:{total % 60:02d}"


def voltage_ylim(cutoff: float | None, voltages: Sequence[float]) -> tuple[float, float]:
    """下限 = min(終止電圧 − 0.2, データ最小 − 0.05)、上限 = max(4.3, データ最大 + 0.05)"""
    c = 3.0 if cutoff is None else cutoff
    lo, hi = c - 0.2, 4.3
    if len(voltages):
        lo = min(lo, min(voltages) - 0.05)
        hi = max(hi, max(voltages) + 0.05)
    return lo, hi


def voltage_axis(cutoff: float | None, voltages: Sequence[float]) -> tuple[float, float, list[float]]:
    """電圧の縦軸の (下限, 上限, 目盛り)。目盛りは両端を含めて等間隔に 6 本。

    voltage_ylim の範囲を 0.1 V 単位で外側に広げ、間隔が 0.1 V の倍数になるようにする
    （既定の 終止 3.000 V なら 2.8〜4.3、目盛り 0.3 V ごと）。
    """
    lo, hi = voltage_ylim(cutoff, voltages)
    lo = math.floor(round(lo * 10, 6)) / 10
    step = max(math.ceil(round((hi - lo) / 5 * 10, 6)) / 10, 0.1)
    return lo, lo + step * 5, [round(lo + step * k, 3) for k in range(6)]


def current_ylim(set_current: float | None, currents: Sequence[float] = ()) -> tuple[float, float]:
    """0 〜 放電電流設定値 × 1.5（設定が無いときはデータの最大から決める）"""
    if set_current:
        return 0.0, set_current * 1.5
    top = max(currents) if len(currents) else 1.0
    return 0.0, max(top, 0.001) * 1.5


def _set_markers(axes, markers: Sequence[float], color: str, linewidth: float) -> None:
    """負荷を ON・OFF にした経過時間 [s] に縦線を引き直す"""
    for ax in axes:
        for line in getattr(ax, "_sdl_marks", []):
            line.remove()
        ax._sdl_marks = [ax.axvline(t / 3600, color=color, linewidth=linewidth, linestyle=MARKER_DASH, zorder=1)
                         for t in markers]


def _apply_common(ax_v, ax_i, elapsed, voltage, current, cutoff, set_current, *, suffix_h: bool,
                  span_hours: float | None = None, fit: bool = False) -> tuple[float, float, list[float]]:
    max_t = elapsed[-1] if len(elapsed) else 0.0
    xmin, xmax, step = time_window(max_t, span_hours, fit=fit)
    # 横軸の範囲に入る点だけを描く（左端の外の 1 点は線をつなぐために残す）
    start = max(bisect.bisect_left(elapsed, xmin * 3600) - 1, 0) if xmin > 0 else 0
    elapsed, voltage, current = elapsed[start:], voltage[start:], current[start:]
    xs_v, ys_v = decimate(elapsed, voltage)
    xs_i, ys_i = decimate(elapsed, current)
    ax_v._sdl_line.set_data([x / 3600 for x in xs_v], ys_v)
    ax_i._sdl_line.set_data([x / 3600 for x in xs_i], ys_i)

    first = math.ceil(xmin / step - 1e-9)
    ticks = [k * step for k in range(first, int(math.floor(xmax / step + 1e-9)) + 1)]
    ax_i.set_xlim(xmin, xmax)
    for ax in (ax_v, ax_i):
        ax.xaxis.set_major_locator(FixedLocator(ticks))
    last = ticks[-1] if ticks else xmax
    unit = " 分" if uses_seconds(step) else " h"  # 画面の最後の目盛りに付ける単位
    ax_i.xaxis.set_major_formatter(FuncFormatter(
        lambda x, _pos: format_time(x, step) + (unit if suffix_h and abs(x - last) < 1e-9 else "")))
    ax_i._sdl_step = step

    v_lo, v_hi, v_ticks = voltage_axis(cutoff, voltage)
    ax_v.set_ylim(v_lo, v_hi)
    ax_v.yaxis.set_major_locator(FixedLocator(v_ticks))
    lo, hi = current_ylim(set_current, current)
    ax_i.set_ylim(lo, hi)
    ax_i.yaxis.set_major_locator(FixedLocator([lo + (hi - lo) * k / 3 for k in range(4)]))
    ax_v._sdl_cutoff.set_ydata([cutoff, cutoff] if cutoff is not None else [float("nan")] * 2)
    return xmin, xmax, ticks


# ---- 画面（液晶スタイル） ----
class LcdPlot:
    """画面の 2 段グラフ。FigureCanvasTkAgg に載せて使う（電流グラフは高さ 92px 固定）"""

    def __init__(self, figure: Figure):
        setup_fonts()
        self.figure = figure
        figure.set_facecolor(C["lcd-bg"])
        self.ax_v = figure.add_axes((0.1, 0.4, 0.85, 0.5))
        self.ax_i = figure.add_axes((0.1, 0.1, 0.85, 0.2), sharex=self.ax_v)
        # 文字の大きさは DESIGN.md の px（100% のとき）で決める。拡大率は figure の dpi に入っている
        self._tick_font = {"family": _fonts["num"], "size": px_to_pt(11, 100)}
        head = px_to_pt(12, 100)
        for ax in (self.ax_v, self.ax_i):
            ax.set_facecolor(C["lcd-bg"])
            for spine in ax.spines.values():
                spine.set_color(C["lcd-frame"])
                spine.set_linewidth(1.0)
            ax.grid(True, color=C["lcd-grid"], linewidth=0.8)
            ax.set_axisbelow(True)
            ax.tick_params(colors=C["lcd-tick"], length=0, pad=4)
        self.ax_v.tick_params(labelbottom=False)
        self.ax_v.yaxis.set_major_formatter(FuncFormatter(lambda y, _p: f"{y:.1f}"))
        self.ax_i.yaxis.set_major_formatter(FuncFormatter(lambda y, _p: f"{y:.1f}"))
        (self.ax_v._sdl_line,) = self.ax_v.plot([], [], color=C["lcd-value"], linewidth=1.8)
        (self.ax_i._sdl_line,) = self.ax_i.plot([], [], color=C["lcd-accent"], linewidth=1.8)
        self.ax_v._sdl_cutoff = self.ax_v.axhline(float("nan"), color=C["lcd-cutoff"], linewidth=1.2,
                                                  linestyle=CUTOFF_DASH)
        self.ax_v.set_title("電圧 [V]", loc="left", pad=6, color=C["lcd-label"], fontsize=head)
        self.legend = self.ax_v.set_title("", loc="right", pad=6, color=C["lcd-cutoff"], fontsize=head)
        self.ax_i.set_title("電流 [A]", loc="left", pad=6, color=C["lcd-label"], fontsize=head)
        self.layout()
        self.update([], [], [], None, None)

    def layout(self) -> None:
        """余白・電流グラフの高さを px で固定し、電圧グラフを残りに伸ばす"""
        # 余白は DESIGN.md の px。figure の dpi が 100 を超える（画面の拡大率）ときはその分広げる
        k = self.figure.dpi / 100
        w, h = self.figure.get_size_inches() * self.figure.dpi / k
        w, h = max(w, 200), max(h, 200)
        left, right = LCD_LEFT / w, 1 - LCD_RIGHT / w
        v_bottom = (LCD_BOTTOM + LCD_CURRENT_H + LCD_GAP) / h
        self.ax_i.set_position((left, LCD_BOTTOM / h, right - left, LCD_CURRENT_H / h))
        self.ax_v.set_position((left, v_bottom, right - left, max(1 - LCD_TOP / h - v_bottom, 0.05)))

    def update(self, elapsed: Sequence[float], voltage: Sequence[float], current: Sequence[float],
               cutoff: float | None, set_current: float | None, span_hours: float | None = None,
               fit: bool = False, markers: Sequence[float] = ()) -> None:
        """span_hours: 横軸の幅［時間］。None なら自動。fit: 自動のとき測定時間に合わせる（放電が終わったあと）。
        markers: 縦線を引く経過時間 [s]（オフセットを入れたとき、負荷を ON・OFF にした時刻）"""
        xmin, xmax, values = _apply_common(self.ax_v, self.ax_i, elapsed, voltage, current, cutoff, set_current,
                                           suffix_h=True, span_hours=span_hours, fit=fit)
        _set_markers((self.ax_v, self.ax_i), markers, C["lcd-label"], 1.0)
        self.legend.set_text(f"- - 終止電圧 {cutoff:.3f} V" if cutoff is not None else "")
        # 目盛り数字は Consolas。横軸の最初は左揃え・最後は右揃え（液晶の枠からはみ出さないように）
        for ax in (self.ax_v, self.ax_i):
            for tick in ax.xaxis.get_major_ticks() + ax.yaxis.get_major_ticks():
                tick.label1.set_fontproperties(self._tick_font)
        ticks = self.ax_i.xaxis.get_major_ticks()[:len(values)]
        for tick, x in zip(ticks, values):
            edge = "left" if abs(x - xmin) < 1e-9 else "right" if abs(x - xmax) < 1e-9 else "center"
            tick.label1.set_horizontalalignment(edge)


# ---- PNG（白背景スタイル） ----
def png_title_lines(base: str | None, model: str, current: float | None, cutoff: float | None,
                    mah: float | None = None, wh: float | None = None, reason: str | None = None, *,
                    pre_voltage: float | None = None, post_voltage: float | None = None) -> list[str]:
    """1 行目: ベース名、2 行目: 型番・放電電流・終止電圧（未入力は省く）、次の行: 完了後の結果、
    最後の行: オフセットを入れたときの放電前電圧・放電後電圧（測れたものだけ）"""
    lines = [base or "（未開始）"]
    second = []
    if model:
        second.append(f"型番: {model}")
    if current is not None:
        second.append(f"放電電流: {current:.3f} A")
    if cutoff is not None:
        second.append(f"終止電圧: {cutoff:.3f} V")
    if second:
        lines.append(" / ".join(second))
    if reason is not None and mah is not None and wh is not None:
        lines.append(f"放電容量: {mah:.1f} mAh / 電力量: {wh:.3f} Wh / 終了理由: {reason}")
    rest = []
    if pre_voltage is not None:
        rest.append(f"放電前電圧: {pre_voltage:.3f} V")
    if post_voltage is not None:
        rest.append(f"放電後電圧: {post_voltage:.3f} V")
    if rest:
        lines.append(" / ".join(rest))
    return lines


def render_png(path: Path, elapsed: Sequence[float], voltage: Sequence[float], current: Sequence[float],
               cutoff: float | None, set_current: float | None, title_lines: list[str],
               markers: Sequence[float] = ()) -> Figure:
    """1600×1000 px の白背景 PNG を保存する（画面のグラフとは別の Figure で描く）。

    横軸はいつも測定全体に合わせる（画面の横軸の設定には関係なく、最初から最後までが収まる）。
    markers: 縦線を引く経過時間 [s]（オフセットを入れたとき、負荷を ON・OFF にした時刻）
    """
    setup_fonts()
    fig = Figure(figsize=PNG_SIZE_INCH, dpi=PNG_DPI, facecolor=C["png-bg"])
    FigureCanvasAgg(fig)
    gs = fig.add_gridspec(2, 1, height_ratios=[2, 1], hspace=0.08)
    ax_v = fig.add_subplot(gs[0])
    ax_i = fig.add_subplot(gs[1], sharex=ax_v)
    for ax in (ax_v, ax_i):
        ax.set_facecolor(C["png-bg"])
        ax.grid(True, color=C["png-grid"], linewidth=1.0)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(C["png-text"])
        ax.tick_params(colors=C["png-text"], labelsize=11)
    ax_v.tick_params(labelbottom=False)
    (ax_v._sdl_line,) = ax_v.plot([], [], color=C["png-voltage"], linewidth=2.0)
    (ax_i._sdl_line,) = ax_i.plot([], [], color=C["png-current"], linewidth=2.0)
    ax_v._sdl_cutoff = ax_v.axhline(float("nan"), color=C["png-cutoff"], linewidth=1.5, linestyle=CUTOFF_DASH)
    ax_i.yaxis.set_major_formatter(FuncFormatter(lambda y, _p: f"{y:.2f}"))
    _apply_common(ax_v, ax_i, elapsed, voltage, current, cutoff, set_current, suffix_h=False, fit=True)
    _set_markers((ax_v, ax_i), markers, C["png-text"], 1.2)
    if cutoff is not None:
        ax_v.text(1.0, cutoff, f"終止電圧 {cutoff:.3f} V", transform=ax_v.get_yaxis_transform(),
                  ha="right", va="bottom", color=C["png-cutoff"], fontsize=11,
                  bbox={"facecolor": C["png-bg"], "edgecolor": "none", "pad": 2})
    ax_v.set_ylabel("電圧 [V]", color=C["png-text"], fontsize=13)
    ax_i.set_ylabel("電流 [A]", color=C["png-text"], fontsize=13)
    xlabel = "経過時間 [分:秒]" if uses_seconds(ax_i._sdl_step) else "経過時間 [h:mm]"
    ax_i.set_xlabel(xlabel, color=C["png-text"], fontsize=13)
    n = len(title_lines)
    fig.suptitle("\n".join(title_lines), color=C["png-text"], fontsize=15, linespacing=1.5, y=0.985, va="top")
    fig.subplots_adjust(left=0.07, right=0.97, bottom=0.08, top=0.92 - 0.035 * (n - 1))
    fig.savefig(str(path), dpi=PNG_DPI, format="png", facecolor=C["png-bg"])
    return fig  # 確認用（呼び出し側は使わなくてよい）
