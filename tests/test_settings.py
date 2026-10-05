"""設定の保持（F-09）"""

import json

import settings


def test_defaults_when_missing(app_dir):
    s = settings.load()
    assert s.host == "192.168.10.2" and s.port == 5025
    assert s.current == 0.4 and s.cutoff == 3.5 and s.interval == 1.0  # 初めて起動したときは 0.400 A / 3.500 V
    assert s.folder == str(app_dir)  # 保存先の初期値は exe と同じフォルダ


def test_roundtrip_only_allowed_keys(app_dir, tmp_path):
    s = settings.Settings(host="10.0.0.5", port=5026, folder=str(tmp_path), current=2.0, cutoff=2.75,
                          interval=0.5)
    assert settings.save(s)
    data = json.loads((app_dir / "sdl_logger_settings.json").read_text(encoding="utf-8"))
    assert set(data) == {"host", "port", "folder", "current", "cutoff", "interval", "keep_awake", "sound", "auto_von"}
    assert settings.load() == s


def test_broken_or_out_of_range_values_fall_back(app_dir):
    (app_dir / "sdl_logger_settings.json").write_text(
        json.dumps({"host": "", "port": 70000, "folder": "/存在しない", "current": 9, "cutoff": "3",
                    "interval": 0.1}), encoding="utf-8")
    s = settings.load()
    assert s.host == "192.168.10.2" and s.port == 5025
    assert s.current == 0.4 and s.cutoff == 3.5 and s.interval == 1.0
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
    """オプション（画面を消さない・音で知らせる・Von を自動設定）は初期値 ON。変えたら保存される"""
    s = settings.load()
    assert s.keep_awake and s.sound and s.auto_von
    s.keep_awake, s.sound, s.auto_von = False, False, False
    settings.save(s)
    loaded = settings.load()
    assert not loaded.keep_awake and not loaded.sound and not loaded.auto_von
    (app_dir / "sdl_logger_settings.json").write_text(json.dumps({"sound": "yes", "auto_von": 1}), encoding="utf-8")
    loaded = settings.load()
    assert loaded.sound and loaded.auto_von  # bool でない値は無視して初期値
