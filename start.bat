@echo off
cd /d "%~dp0"
echo Starting SKHY watchdog (keeps http://127.0.0.1:8765/ alive)...
echo Logs: %~dp0watchdog.log
python watchdog.py
pause
