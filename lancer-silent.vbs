' Lancement 100% silencieux : aucune fenetre MS-DOS (ni au boot ni pour docker/curl/wsl)
' Double-cliquez sur ce .vbs au lieu du .bat. pythonw + CREATE_NO_WINDOW font le reste.
' Equivalent a : pythonw opencode.py --gui  mais sans aucune console CMD.

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
' 0 = fenetre cachee, False = ne pas attendre
sh.Run "pythonw opencode.py", 0, False
