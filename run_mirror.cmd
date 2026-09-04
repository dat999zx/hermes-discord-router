@echo off
rem Hermes Desktop -> Discord mirror (manual / debug launcher)
rem Runs in the foreground so you can see logs. For silent autostart use the .vbs.
set "HERMES_HOME=C:\Users\Admin\AppData\Local\hermes"
set "PYTHONIOENCODING=utf-8"
"C:\Users\Admin\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" "D:\coding\hermes-discord-router\desktop_mirror.py"
