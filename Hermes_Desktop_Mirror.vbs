' Hermes Desktop -> Discord mirror - silent autostart launcher
' Mirrors the gateway's own Hermes_Gateway.vbs style: sets env, runs hidden (0), non-blocking.
Option Explicit
Dim sh, env
Set sh = CreateObject("WScript.Shell")
Set env = sh.Environment("PROCESS")
env.Item("HERMES_HOME") = "C:\Users\Admin\AppData\Local\hermes"
env.Item("PYTHONIOENCODING") = "utf-8"
sh.CurrentDirectory = "D:\coding\hermes-discord-router"
sh.Run "C:\Users\Admin\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe ""D:\coding\hermes-discord-router\desktop_mirror.py""", 0, False
