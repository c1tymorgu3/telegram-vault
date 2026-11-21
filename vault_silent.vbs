' Telegram Vault - sessiz baslatici.
' Panel ve dinleyiciyi pencere acmadan baslatir; cikti logs\ altina yazilir.
' bot.py ayni session'da ikinci sureci kendi reddeder, o yuzden bu betik
' birden fazla kez cagrilirsa zararsizdir.

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base

logDir = base & "\logs"
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)

py = base & "\.venv\Scripts\python.exe"

cmdPanel = "cmd /c """"" & py & """ -m uvicorn app:app --host 127.0.0.1 --port 8000 >> """ & logDir & "\panel.log"" 2>&1"""
cmdBot = "cmd /c """"" & py & """ -u bot.py >> """ & logDir & "\bot.log"" 2>&1"""

' Panel zaten ayaktaysa ikinci kez baslatma (port 8000 zaten dinleniyor olur).
panelUp = False
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", "http://127.0.0.1:8000/login", False
http.Send
If Err.Number = 0 Then panelUp = True
Err.Clear
On Error GoTo 0

If Not panelUp Then
    sh.Run cmdPanel, 0, False
    WScript.Sleep 3000
End If
sh.Run cmdBot, 0, False