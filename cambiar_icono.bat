@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo.
echo  === Cambiar el icono de Radio 3 Player ===
echo.
echo  Uso: arrastra una imagen ^(SVG, PNG, JPG...^) encima de este archivo, o guardala como logo.png en
echo  esta carpeta ^(o logo.svg^) y haz doble clic. Para volver al icono de la app:
echo  cambiar_icono.bat original
echo.

python -c "import PIL, resvg_py" >nul 2>nul || python -m pip install --quiet pillow resvg-py

python icono.py %*
if errorlevel 1 (
  pause
  exit /b 1
)

rem Vuelve a crear el acceso directo para que coja el icono nuevo
set "PYW="
for /f "delims=" %%i in ('where pythonw 2^>nul') do if not defined PYW set "PYW=%%i"
if defined PYW (
  powershell -NoProfile -Command ^
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Radio 3.lnk');" ^
    "$s.TargetPath='%PYW%';$s.Arguments='\"%~dp0radio3.py\"';$s.WorkingDirectory='%~dp0';" ^
    "$s.IconLocation='%~dp0icono.ico,0';$s.Save()"
)

rem Refresca la cache de iconos de Windows
ie4uinit.exe -show >nul 2>nul

echo.
echo  Listo. Cierra y vuelve a abrir Radio 3 para ver el icono nuevo.
echo  ^(Si lo tenias anclado a la barra de tareas, desanclalo y vuelve a anclarlo.^)
echo.
pause
