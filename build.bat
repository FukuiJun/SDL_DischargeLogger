@echo off
rem Build SDL discharge logger (Windows 11 / Python 3.12)
rem Usage: double-click build.bat, or run it from a command prompt.
rem Output: dist\SDL_DischargeLogger\SDL_DischargeLogger.exe (a folder; distribute the whole folder)
rem This file is ASCII only on purpose (non-ASCII text breaks cmd parsing).
setlocal
cd /d "%~dp0" || goto :error
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set PY=py -3.12
%PY% --version >NUL 2>&1 || set PY=python

echo [1/3] Installing packages
%PY% -m pip install --upgrade -r requirements.txt || goto :error

echo [2/3] Running tests
%PY% -m pytest -q || goto :error

echo [3/3] Building exe
%PY% build_exe.py || goto :error

echo.
echo Done: see the dist folder
endlocal
exit /b 0

:error
echo.
echo BUILD FAILED
endlocal
exit /b 1
