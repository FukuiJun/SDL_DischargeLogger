"""グラフ（F-07、F-08、DESIGN.md 10 章、AC-11）"""

import struct
import time

import pytest

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

import plotting
from theme import C


def test_decimate_keeps_last_point_and_limit():
    xs = list(range(86400))
    dx, dy = plotting.decimate(xs, xs)
    assert len(dx) <= plotting.MAX_PLOT_POINTS
    assert dx[0] == 0 and dx[-1] == 86399
    small = list(range(100))
    assert plotting.decimate(small, small) == (small, small)


def test_time_axis():
    """横軸は max(1 時間, 経過×1.2) を 30 分単位で切り上げ。目盛りは 30 分／6h超 1h／12h超 2h"""
    assert plotting.time_axis(0) == (1.0, 0.5)
    assert plotting.time_axis(2 * 3600 + 12 * 60 + 35) == (3.0, 0.5)   # 見本: 0:00〜3:00 h
    assert plotting.time_axis(5 * 3600) == (6.0, 0.5)
    assert plotting.time_axis(6 * 3600) == (8.0, 1.0)                  # 7.2h → 7.5 → 1h 刻みで 8
    assert plotting.time_axis(11 * 3600) == (14.0, 2.0)
    assert plotting.format_hmm(2.5) == "2:30"
    assert plotting.format_hmm(0) == "0:00"


def test_voltage_axis():
    """下限 = min(終止−0.2, 最小−0.05)、上限 = max(4.3, 最大+0.05)。目盛りは両端を含む 6 本"""
    lo, hi, ticks = plotting.voltage_axis(3.0, [4.1, 3.5])
    assert (lo, hi) == (2.8, 4.3)
    assert ticks == [2.8, 3.1, 3.4, 3.7, 4.0, 4.3]
    lo, hi, ticks = plotting.voltage_axis(3.0, [4.25, 2.45])
    assert lo <= 2.40 and hi >= 4.3 and len(ticks) == 6
    assert plotting.current_ylim(1.0) == (0.0, 1.5)


def _lcd():
    fig = Figure(figsize=(7.7, 3.6), dpi=100)
    canvas = FigureCanvasAgg(fig)
    return fig, canvas, plotting.LcdPlot(fig)


def test_lcd_style():
    """10.1：黒背景、黄色の電圧線、青の電流線、オレンジ破線の終止電圧"""
    fig, canvas, plot = _lcd()
    plot.update([0, 3600], [4.0, 3.6], [1.0, 1.0], 3.0, 1.0)
    canvas.draw()
    assert plot.ax_v.get_facecolor()[:3] == tuple(int(C["lcd-bg"][k:k + 2], 16) / 255 for k in (1, 3, 5))
    assert plot.ax_v._sdl_line.get_color() == C["lcd-value"]
    assert plot.ax_i._sdl_line.get_color() == C["lcd-accent"]
    assert plot.ax_v._sdl_cutoff.get_color() == C["lcd-cutoff"]
    assert list(plot.ax_v._sdl_cutoff.get_ydata()) == [3.0, 3.0]
    assert plot.legend.get_text() == "- - 終止電圧 3.000 V"
    labels = [t.get_text() for t in plot.ax_i.get_xticklabels()]
    assert labels == ["0:00", "0:30", "1:00", "1:30 h"]  # 1h×1.2 を 30 分単位で切り上げ
    assert plot.ax_i.get_ylim() == (0.0, 1.5)
    # 電流グラフは高さ 92px
    assert abs(plot.ax_i.get_position().height * 360 - 92) < 1


def test_graph_update_86400_points_within_half_second():
    """AC-11：86,400 点でもグラフ更新（描画込み）が 0.5 秒以内"""
    fig, canvas, plot = _lcd()
    n = 86400
    t = [float(k) for k in range(n)]
    v = [4.2 - 1.2 * k / n for k in range(n)]
    i = [1.0] * n
    plot.update(t[:10], v[:10], i[:10], 3.0, 1.0)
    canvas.draw()  # 初回の描画（フォント読み込み等）は除く
    worst = 0.0
    for _ in range(3):
        start = time.perf_counter()
        plot.update(t, v, i, 3.0, 1.0)
        canvas.draw()
        worst = max(worst, time.perf_counter() - start)
    assert worst < 0.5, f"{worst:.3f} 秒"
    assert len(plot.ax_v._sdl_line.get_xdata()) <= plotting.MAX_PLOT_POINTS


def test_png_title_lines_omit_missing():
    """10.2：2 行目は未入力の項目を省く。3 行目は完了後のみ"""
    assert plotting.png_title_lines("20261001_1430_4v1_pana", "NCR18650B", 1.0, 3.0) == [
        "20261001_1430_4v1_pana", "型番: NCR18650B / 放電電流: 1.000 A / 終止電圧: 3.000 V"]
    assert plotting.png_title_lines("b", "", 1.0, 3.0)[1] == "放電電流: 1.000 A / 終止電圧: 3.000 V"
    lines = plotting.png_title_lines("b", "X", 1.0, 3.0, 2702.5, 9.874, "終止電圧到達")
    assert lines[2] == "放電容量: 2702.5 mAh / 電力量: 9.874 Wh / 終了理由: 終止電圧到達"


def test_render_png_size(tmp_path):
    """5.2：PNG は 1600×1000 px（白背景）"""
    path = tmp_path / "g.png"
    lines = plotting.png_title_lines("20261001_1430_4v1_pana", "NCR18650B", 1.0, 3.0)
    plotting.render_png(path, [0, 60, 120], [4.1, 4.0, 3.9], [1, 1, 1], 3.0, 1.0, lines)
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", head[16:24]) == (1600, 1000)


def test_time_window_manual_span():
    """横軸の手動設定：幅を固定し、経過時間が幅を超えたら最新の点が右端に来るように送る"""
    assert plotting.time_window(1800, None) == (0.0, 1.0, 0.5)          # 自動
    assert plotting.time_window(1800, 3.0) == (0.0, 3.0, 0.5)           # 幅より短い：0 から
    xmin, xmax, step = plotting.time_window(5 * 3600, 2.0)              # 幅より長い：最新 2 時間
    assert (xmin, xmax, step) == (3.0, 5.0, 0.5)
    assert plotting.tick_step(24) == 2.0 and plotting.tick_step(48) == 4.0
    assert plotting.tick_step(0.5) == 5 / 60          # 30 分 → 5 分刻み
    assert plotting.tick_step(0.05) == 30 / 3600      # 3 分 → 30 秒刻み
    assert plotting.time_window(150, 0.05) == (0.0, 0.05, 30 / 3600)
    assert plotting.time_window(240, 0.05) == pytest.approx((1 / 60, 4 / 60, 30 / 3600))  # 3 分を超えたら送る


def test_lcd_manual_span_scrolls():
    fig, canvas, plot = _lcd()
    t = [float(k) for k in range(0, 5 * 3600, 10)]
    v = [4.2 - 0.0001 * k for k in range(len(t))]
    plot.update(t, v, [1.0] * len(t), 3.0, 1.0, span_hours=1.0)
    canvas.draw()
    lo, hi = plot.ax_i.get_xlim()
    assert abs(hi - t[-1] / 3600) < 1e-9 and abs(hi - lo - 1.0) < 1e-9
    xs = plot.ax_v._sdl_line.get_xdata()
    assert min(xs) >= lo - 0.01  # 範囲外の古い点は描かない（左端の外の 1 点を除く）
    labels = [lb.get_text() for lb in plot.ax_i.get_xticklabels() if lb.get_text()]
    assert labels[-1].endswith(" h") or labels[-1].count(":") == 1



def test_fit_axis():
    """放電が終わったあと・画像：測定時間に合う横軸（余白 5%、目盛りの区切り 8 以下）"""
    assert plotting.fit_axis(120) == pytest.approx((150 / 3600, 30 / 3600))       # 2 分 → 0〜2:30、30 秒刻み
    assert plotting.fit_axis(25 * 60) == pytest.approx((0.5, 5 / 60))             # 25 分 → 0〜30 分、5 分刻み
    assert plotting.fit_axis(2 * 3600 + 12 * 60) == pytest.approx((2.5, 0.5))     # 2:12 → 0〜2:30、30 分刻み
    assert plotting.fit_axis(0) == pytest.approx((60 / 3600, 10 / 3600))          # 最低 1 分
    assert plotting.time_window(120, None, fit=True) == pytest.approx((0.0, 150 / 3600, 30 / 3600))
    assert plotting.time_window(120, None) == (0.0, 1.0, 0.5)                     # 測定中の自動は今までどおり


def test_format_time_switches_to_seconds():
    assert plotting.format_time(150 / 3600, 30 / 3600) == "2:30"   # 2 分 30 秒（m:ss）
    assert plotting.format_time(2.5, 0.5) == "2:30"                # 2 時間 30 分（h:mm）
    assert plotting.format_time(5 / 60, 1 / 60) == "0:05"          # 1 分刻みは h:mm
    assert plotting.uses_seconds(30 / 3600) and not plotting.uses_seconds(1 / 60)


def test_png_axis_fits_short_discharge(tmp_path):
    """2 分で終わった放電の画像は、横軸が 0〜2:30（分:秒）で全体が収まる"""
    t = [float(k) for k in range(0, 121)]
    fig = plotting.render_png(tmp_path / "s.png", t, [4.1 - k * 0.004 for k in range(121)], [1.0] * 121,
                              3.5, 1.0, ["x"])
    ax_i = fig.axes[1]
    assert ax_i.get_xlim() == pytest.approx((0.0, 150 / 3600))
    assert ax_i.get_xlabel() == "経過時間 [分:秒]"
    labels = [ax_i.xaxis.get_major_formatter()(x, None) for x in ax_i.get_xticks()]
    assert labels[0] == "0:00" and labels[-1] == "2:30" and "1:00" in labels


def test_png_axis_long_discharge_uses_hmm(tmp_path):
    t = [float(k) for k in range(0, 3 * 3600, 60)]
    fig = plotting.render_png(tmp_path / "l.png", t, [4.0] * len(t), [1.0] * len(t), 3.0, 1.0, ["x"])
    assert fig.axes[1].get_xlim() == pytest.approx((0.0, 3.5))
    assert fig.axes[1].get_xlabel() == "経過時間 [h:mm]"


def test_png_title_lines_with_rest_voltages():
    lines = plotting.png_title_lines("b", "M", 1.0, 3.0, 100.0, 0.4, "終止電圧到達", pre_voltage=4.1504,
                                     post_voltage=3.41)
    assert lines[-1] == "放電前電圧: 4.150 V / 放電後電圧: 3.410 V"
    only_pre = plotting.png_title_lines("b", "M", 1.0, 3.0, pre_voltage=4.15)
    assert only_pre[-1] == "放電前電圧: 4.150 V"
    assert plotting.png_title_lines("b", "M", 1.0, 3.0)[-1].startswith("型番")  # オフセットなしなら出さない


def test_png_markers(tmp_path):
    """オフセットを入れたとき、負荷を ON・OFF にした時刻に縦線を引く"""
    t = [float(k) for k in range(0, 200, 2)]
    fig = plotting.render_png(tmp_path / "m.png", t, [4.0] * len(t), [0.0] * len(t), 3.0, 1.0, ["x"],
                              markers=[10.0, 150.0])
    ax_v, ax_i = fig.axes[:2]
    for ax in (ax_v, ax_i):
        assert [line.get_xdata()[0] * 3600 for line in ax._sdl_marks] == pytest.approx([10.0, 150.0])
    fig2 = plotting.render_png(tmp_path / "n.png", t, [4.0] * len(t), [0.0] * len(t), 3.0, 1.0, ["x"])
    assert fig2.axes[0]._sdl_marks == []
