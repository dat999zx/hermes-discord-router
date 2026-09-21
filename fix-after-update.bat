@echo off
REM Repair the Discord router after a `hermes update`.
REM
REM Every update resets the hermes-agent tree, which reverts the gateway patch
REM (plugins/platforms/discord/adapter.py + gateway/run.py) and can leave the
REM installed discord_router.py stale. Channel routing then silently stops:
REM messages land in the Home project instead of their folder.
REM
REM Double-click this after any Hermes update. Safe to run any time -- the
REM installer is idempotent and reuses the channels already in config.yaml.

setlocal
set "REPO=D:\coding\hermes-discord-router"
set "PY=C:\Users\Admin\AppData\Local\Python\pythoncore-3.14-64\python.exe"

echo ============================================
echo  Repairing the Hermes Discord router
echo ============================================
echo.

if not exist "%PY%" (
    echo [X] Python not found at:
    echo     %PY%
    echo     Install Python, or edit PY= at the top of this file.
    goto :fail
)
if not exist "%REPO%\install.py" (
    echo [X] Router repo not found at %REPO%
    goto :fail
)

cd /d "%REPO%"
"%PY%" install.py
if errorlevel 1 (
    echo.
    echo [X] install.py failed -- see the output above.
    echo     If it says ANCHOR NOT FOUND, the update moved the code the patch
    echo     attaches to and the anchors need re-pointing.
    goto :fail
)

echo.
echo Restarting the gateway (stop + start, not restart: a plain restart
echo inherits a stale DISCORD_CHANNEL_PROJECTS from the old process)...
hermes gateway stop
REM `ping` as the sleep: timeout.exe refuses redirected stdin ("Input redirection
REM is not supported"), and git-bash on PATH shadows both `timeout` and `sleep`
REM with POSIX versions that reject /t.
ping -n 5 127.0.0.1 >nul
echo N | hermes gateway start
ping -n 15 127.0.0.1 >nul

REM `hermes gateway start` re-adds Hermes's OWN login item, which would start a
REM second gateway at boot and fight the supervisor. Strip it again.
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Hermes_Gateway.vbs" >nul 2>&1

REM The mirror reads discord.channels ONCE at startup, so a running one holds a
REM stale channel map -- a newly added channel routes INBOUND but Desktop turns
REM in that folder never mirror OUT. Kill it; the supervisor respawns it in ~20s
REM (and the supervisor itself now watches config.yaml for this).
echo.
echo Recycling the mirror so it reloads the channel map...
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name like '%%python%%'\" |" ^
  "  Where-Object { $_.CommandLine -like '*desktop_mirror.py*' } |" ^
  "  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
ping -n 25 127.0.0.1 >nul

echo.
echo === Channels the gateway actually loaded ===
powershell -NoProfile -Command ^
  "$log = \"$env:LOCALAPPDATA\hermes\logs\gateway.log\";" ^
  "$line = Select-String -Path $log -Pattern 'Set DISCORD_CHANNEL_PROJECTS' | Select-Object -Last 1;" ^
  "if ($line) { ($line.Line -split 'Set DISCORD_CHANNEL_PROJECTS: ')[-1] }" ^
  "else { Write-Host '(no routing line found - check the log)' }"

echo.
echo Done. Verify a channel maps to its folder above, then prompt in Discord.
echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1
