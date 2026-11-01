@echo off
rem Startet die astrostack-Oberflaeche im Browser. Beim ersten Mal wird das Programm installiert.
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% -c "import astrostack" 2>nul || (
  echo Installiere astrostack ...
  %PY% -m pip install --user -e . || (echo Installation fehlgeschlagen & pause & exit /b 1)
)
start "" %PY%w -m astrostack app

