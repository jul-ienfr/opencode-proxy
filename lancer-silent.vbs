' Lancement 100% silencieux : aucune fenetre MS-DOS (ni au boot ni pour docker/curl/wsl)
' Double-cliquez sur ce .vbs au lieu du .bat. pythonw + CREATE_NO_WINDOW font le reste.
' Equivalent a : pythonw opencode.py --gui  mais sans aucune console CMD.

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
' 0 = fenetre cachee, False = ne pas attendre
' [fix 2026-10-28] `pythonw` du PATH resout vers C:\Python314 (install
' incomplete : pas de Lib\ a cote de l'exe) -> forcer Python 3.12, celui
' qui porte les dependances du projet.
py = "C:\Users\julie\AppData\Local\Programs\Python\Python312\pythonw.exe"
If fso.FileExists(py) Then
  sh.Run """" & py & """ opencode.py", 0, False
Else
  sh.Run "pythonw opencode.py", 0, False
End If
