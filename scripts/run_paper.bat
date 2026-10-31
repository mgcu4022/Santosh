@echo off
REM Start today's paper-trading session (Windows).
REM Schedule it once (run in Command Prompt, change the path):
REM   schtasks /create /tn "tradebot paper" /tr "C:\path\to\Santosh\scripts\run_paper.bat" /sc weekly /d MON,TUE,WED,THU,FRI /st 09:00
REM Keep "Run only when user is logged on" so the Upstox login page can open.
REM The bot exits by itself on weekends/holidays and after 15:30.
cd /d "%~dp0\.."
set CONFIG=%1
if "%CONFIG%"=="" set CONFIG=config\my.yaml
if exist .venv\Scripts\activate.bat call .venv\Scripts\activate.bat
if not exist runs\logs mkdir runs\logs
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set D=%%i
python -m tradebot paper -c %CONFIG% --login -v >> runs\logs\%D%.log 2>&1
python -m tradebot report --telegram >> runs\logs\%D%.log 2>&1 || python -m tradebot report >> runs\logs\%D%.log 2>&1
