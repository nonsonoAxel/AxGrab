@echo off
rem Ricrea Grabby.exe dopo aver modificato app.py o ui\index.html
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  if not exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=python"
  "%PY%" -m venv .venv
)
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt pyinstaller pillow
if not exist "axgrab.ico" ".venv\Scripts\python.exe" make_icon.py
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name AxGrab ^
  --icon "%~dp0axgrab.ico" --add-data "%~dp0ui;ui" ^
  --distpath . --workpath "%TEMP%\axgrab-build" --specpath "%TEMP%\axgrab-build" app.py
echo.
echo Fatto! AxGrab.exe e' pronto in questa cartella.
pause
