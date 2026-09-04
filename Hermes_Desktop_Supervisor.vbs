' Hermes Desktop lifecycle supervisor - silent autostart launcher
' This is the ONLY entry that should live in the Startup folder.
' It starts/stops the gateway + Desktop->Discord mirror alongside Hermes Desktop.
Option Explicit
Dim sh, env
Set sh = CreateObject("WScript.Shell")
Set env = sh.Environment("PROCESS")
env.Item("HERMES_HOME") = "C:\Users\Admin\AppData\Local\hermes"
env.Item("PYTHONIOENCODING") = "utf-8"
sh.CurrentDirectory = "D:\coding\hermes-discord-router"
sh.Run "C:\Users\Admin\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw.exe ""D:\coding\hermes-discord-router\desktop_supervisor.py""", 0, False
