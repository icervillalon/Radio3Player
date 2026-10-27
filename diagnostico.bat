@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Comprobando la conexion con RTVE (programa: %1 o turbo-3 por defecto)...
echo.
if "%1"=="" (python rtve.py turbo-3) else (python rtve.py %1)
echo.
python -c "import player; p=player.Player(); print('VLC: OK')" 2>&1
python -c "import wintegration as w; m=w.MediaControls(lambda c,a: None); print('Teclas multimedia:', m.mode, m.error); m.close()" 2>&1
echo Registro de errores: %APPDATA%\Radio3Player\app.log
echo.
pause
