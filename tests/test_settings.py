"""設定の保持（F-09）"""

import json

import settings


def test_defaults_when_missing(app_dir):
    s = settings.load()
    assert s.host == "192.168.10.2" and s.port == 5025
    assert s.current == 0.4 and s.cutoff == 3.5 and s.interval == 1.0  # 初めて起動したときは 0.400 A / 3.500 V
    assert s.mode == "CC" and s.power == 1.5  # 放電モードは CC、CP の放電電力は 1.500 W
    assert s.folder == str(app_dir)  # 保存先の初期値は exe と同じフォルダ


def test_roundtrip_only_allowed_keys(app_dir, tmp_path):
    s = settings.Settings(host="10.0.0.5", port=5026, folder=str(tmp_path), current=2.0, cutoff=2.75,
                          interval=0.5, mode="CP", power=3.25)
    assert settings.save(s)
    data = json.loads((app_dir / "sdl_logger_settings.json").read_text(encoding="utf-8"))
    assert set(data) == {"host", "port", "folder", "mode", "current", "power", "cutoff", "interval", "keep_awake",
                         "sound", "rest", "pre_rest", "post_rest"}
    assert settings.load() == s


def test_broken_or_out_of_range_values_fall_back(app_dir):
    (app_dir / "sdl_logger_settings.json").write_text(
        json.dumps({"host": "", "port": 70000, "folder": "/存在しない", "current": 9, "cutoff": "3",
                    "interval": 0.1, "mode": "CV", "power": 25}), encoding="utf-8")
    s = settings.load()
    assert s.host == "192.168.10.2" and s.port == 5025
    assert s.current == 0.4 and s.cutoff == 3.5 and s.interval == 1.0
    assert s.mode == "CC" and s.power == 1.5
    (app_dir / "sdl_logger_settings.json").write_text("{壊れた", encoding="utf-8")
    assert settings.load().host == "192.168.10.2"


def test_icon_files():
    """アイコン（デザイン案B）：exe 用の ico に 16〜256 px、ウィンドウ用の PNG がある"""
    from PIL import Image

    import paths

    ico = paths.icon_ico()
    assert ico.exists()
    sizes = Image.open(ico).info["sizes"]
    assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(sizes)
    for p in paths.icon_pngs():
        assert p.exists(), p


def test_options_default_on_and_saved(app_dir):
    """オプション（測定中継続表示・測定終了音）は初期値 ON。変えたら保存される"""
    s = settings.load()
    assert s.keep_awake and s.sound
    s.keep_awake, s.sound = False, False
    settings.save(s)
    loaded = settings.load()
    assert not loaded.keep_awake and not loaded.sound
    (app_dir / "sdl_logger_settings.json").write_text(json.dumps({"sound": "yes", "auto_von": False}),
                                                      encoding="utf-8")
    loaded = settings.load()
    assert loaded.sound  # bool でない値は無視して初期値
    assert not hasattr(loaded, "auto_von")  # v1.1.0 までの［負荷側終止電圧］は読まない（Von はいつも設定する）
