@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw "%~dp0radio3.py" & exit /b)
where pyw >nul 2>nul && (start "" pyw "%~dp0radio3.py" & exit /b)
python "%~dp0radio3.py"
