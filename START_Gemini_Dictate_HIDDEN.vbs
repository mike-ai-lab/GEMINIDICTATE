Set sh = CreateObject("WScript.Shell")
Dim scriptDir
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.CurrentDirectory = scriptDir
sh.Run "pythonw.exe """ & scriptDir & "gemini_dictate.py""", 0, False
