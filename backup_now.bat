@echo off
rem Robi jedna kopie zapasowa bazy danych i plikow stanu do folderu "backups".
rem Baza jest tylko czytana (transakcja READ ONLY) - nic nie jest zmieniane ani kasowane.
rem Bot nie musi byc wlaczony; mozna to uruchomic takze wtedy, gdy dziala.
rem Uwaga: w trybie debug (ALTERMMO_DEBUG=1 w pliku .env) kopiowana jest baza TESTOWA,
rem do folderu "backups-debug". Skrypt wypisuje, ktora baze skopiowal.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m altermmo.features.backup
echo.
pause
