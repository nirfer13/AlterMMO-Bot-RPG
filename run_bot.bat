@echo off
rem Uruchamia bota i wznawia go automatycznie, gdyby proces sie zakonczyl
rem (np. brak internetu przy starcie). Zamknij okno, zeby zatrzymac.
cd /d "%~dp0"
:loop
".venv\Scripts\python.exe" -m altermmo
echo.
echo [%date% %time%] Bot zakonczyl prace (kod %errorlevel%). Ponowny start za 15 sekund...
timeout /t 15 /nobreak >nul
goto loop
