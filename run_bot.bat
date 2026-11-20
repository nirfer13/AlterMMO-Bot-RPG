@echo off
rem Uruchamia bota i wznawia go automatycznie, gdyby proces sie zakonczyl
rem (np. brak internetu przy starcie). Zamknij okno, zeby zatrzymac.
rem Wyjatek: blad konfiguracji (kod 2 lub 3 - brak tokenu / adresu bazy, odrzucony token,
rem wylaczony intent w Developer Portal). Wtedy bot NIE jest wznawiany, bo logowanie co
rem 15 sekund przez wiele godzin konczy sie zresetowaniem tokenu przez Discorda.
cd /d "%~dp0"
:loop
".venv\Scripts\python.exe" -m altermmo
if %errorlevel%==2 goto fatal
if %errorlevel%==3 goto fatal
echo.
echo [%date% %time%] Bot zakonczyl prace (kod %errorlevel%). Ponowny start za 15 sekund...
timeout /t 15 /nobreak >nul
goto loop
:fatal
echo.
echo [%date% %time%] BLAD KONFIGURACJI (kod %errorlevel%) - bot NIE zostanie wznowiony.
echo Przyczyna jest opisana powyzej oraz w pliku logs\altermmo.log.
echo Popraw ustawienia i uruchom run_bot.bat ponownie.
pause
