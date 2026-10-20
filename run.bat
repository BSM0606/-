@echo off
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py
%PY% -c "import pynput" >nul 2>nul || %PY% -m pip install -r requirements.txt
%PY% click_macro.py
if errorlevel 1 pause
