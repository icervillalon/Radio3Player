@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo.
echo  === Radio 3 Player: instalacion ===
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo  No encuentro Python. Instalalo desde https://www.python.org/downloads/
  echo  ^(marca "Add python.exe to PATH" durante la instalacion^) y vuelve a ejecutar este archivo.
  pause
  exit /b 1
)

echo  1/4  Instalando python-vlc...
python -m pip install --upgrade --quiet python-vlc
if errorlevel 1 (
  echo  Fallo al instalar python-vlc.
  pause
  exit /b 1
)

echo  2/4  Instalando los controles multimedia de Windows...
python -m pip install --upgrade --quiet winrt-runtime winrt-Windows.Foundation winrt-Windows.Media winrt-Windows.Media.Playback winrt-Windows.Storage.Streams
if errorlevel 1 (
  echo       No se pudieron instalar ^(la app funcionara igual, con las teclas multimedia en modo basico^).
)

echo  3/4  Comprobando VLC...
if exist "%ProgramFiles%\VideoLAN\VLC\libvlc.dll" (
  echo       VLC encontrado.
) else (
  echo       VLC ^(64 bits^) no esta instalado. Intentando instalarlo con winget...
  winget install --id VideoLAN.VLC -e --accept-source-agreements --accept-package-agreements
  if errorlevel 1 (
    echo       No se pudo instalar automaticamente. Descargalo de https://www.videolan.org/vlc/
  )
)

echo  4/4  Creando acceso directo en el escritorio...
set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if not defined PYW (
  echo       No encuentro pythonw.exe; usa Radio3.bat para abrir la app.
  goto fin
)
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Radio 3.lnk');" ^
  "$s.TargetPath='%PYW%';$s.Arguments='\"%~dp0radio3.py\"';$s.WorkingDirectory='%~dp0';" ^
  "$s.IconLocation='%~dp0icono.ico,0';$s.Save()"

:fin
echo.
echo  Listo. Abre "Radio 3" desde el escritorio o con Radio3.bat
echo.
pause
