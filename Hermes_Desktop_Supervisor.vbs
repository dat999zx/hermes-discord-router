' Hermes Desktop lifecycle supervisor - silent autostart launcher
' This is the ONLY entry that should live in the Startup folder.
' It starts/stops the gateway + Desktop->Discord mirror alongside Hermes Desktop.
'
' Runs on the REPO's own venv, never Hermes's: a long-running process on
' Hermes's venv holds its native .pyd files and makes `hermes update` abort.
Option Explicit
Dim sh, env
Set sh = CreateObject("WScript.Shell")
Set env = sh.Environment("PROCESS")
env.Item("HERMES_HOME") = "C:\Users\Admin\AppData\Local\hermes"
env.Item("PYTHONIOENCODING") = "utf-8"
sh.CurrentDirectory = "D:\coding\hermes-discord-router"
sh.Run "D:\coding\hermes-discord-router\.venv\Scripts\pythonw.exe ""D:\coding\hermes-discord-router\desktop_supervisor.py""", 0, False
