@echo off
rem Ricrea AxGrab.exe dopo aver modificato app.py o ui\index.html
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  if not exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=python"
  "%PY%" -m venv .venv
)
rem versioni esatte (requirements-build.txt), non "l'ultima disponibile"
".venv\Scripts\python.exe" -m pip install --quiet -r requirements-build.txt
if errorlevel 1 (
  echo Installazione delle dipendenze non riuscita: EXE non creato.
  pause
  exit /b 1
)
if not exist "axgrab.ico" ".venv\Scripts\python.exe" make_icon.py
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name AxGrab ^
  --icon "%~dp0axgrab.ico" --add-data "%~dp0ui;ui" ^
  --distpath . --workpath "%TEMP%\axgrab-build" --specpath "%TEMP%\axgrab-build" app.py
if errorlevel 1 (
  echo Creazione dell'EXE non riuscita.
  pause
  exit /b 1
)
rem firma dell'EXE: senza AxGrab.exe.sig nella release, le app installate rifiutano l'aggiornamento
".venv\Scripts\python.exe" firma.py AxGrab.exe
if errorlevel 1 (
  echo.
  echo ATTENZIONE: EXE creato ma NON firmato. Non pubblicarlo cosi': vedi il messaggio qui sopra.
  pause
  exit /b 1
)
echo.
echo Fatto! Carica nella release sia AxGrab.exe sia AxGrab.exe.sig.
pause
