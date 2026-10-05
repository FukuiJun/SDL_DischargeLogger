"""PyInstaller で exe を作る（build.bat から呼ぶ）

フォルダ形式（--onedir）で dist/SDL_DischargeLogger/ を作る。中身は SDL_DischargeLogger.exe と _internal フォルダ。
exe 1 つの形（--onefile）は起動のたびに中身を一時フォルダへ展開し、ウイルス対策ソフトの検査も毎回かかって
起動が遅くなるため使わない。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
EXE_NAME = "SDL_DischargeLogger"
EXCLUDES = ["PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "gi", "IPython", "jupyter", "notebook",
            "pytest", "scipy", "pandas", "sphinx"]


def main() -> int:
    ext = ".exe" if sys.platform == "win32" else ""
    dist = ROOT / "dist"
    args = [
        str(ROOT / "src" / "main.py"),
        "--onedir", "--windowed", "--noconfirm", "--clean",
        "--name", EXE_NAME,
        "--icon", str(ROOT / "assets" / "app.ico"),
        "--paths", str(ROOT / "src"),
        "--distpath", str(dist),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    # ウィンドウのアイコン用（exe の中の assets フォルダに入れる）
    for name in ["app.ico"] + [f"icon_{n}.png" for n in (16, 32, 48, 256)]:
        args += ["--add-data", f"{ROOT / 'assets' / name}{os.pathsep}assets"]
    for mod in EXCLUDES:
        args += ["--exclude-module", mod]
    PyInstaller.__main__.run(args)

    folder = dist / EXE_NAME
    target = folder / f"{EXE_NAME}{ext}"
    size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    print(f"作成しました: {target}（フォルダ全体 {size / 1024 / 1024:.1f} MB。フォルダごと配布する）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
