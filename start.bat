@echo off
setlocal
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
if not exist ".venv\Scripts\python.exe" goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed

:run
".venv\Scripts\python.exe" bot.py
set "BOT_EXIT_CODE=%ERRORLEVEL%"
if "%BOT_EXIT_CODE%"=="75" goto run
if not "%BOT_EXIT_CODE%"=="0" goto failed
goto finished

:failed
echo Ocorreu um erro ao preparar ou executar o bot. Confira a mensagem acima.
pause
exit /b 1

:finished
pause
exit /b 0
