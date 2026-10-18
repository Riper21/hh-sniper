' Запустить командный файл в скрытом окне (0 = SW_HIDE).
' Планировщик дёргает wscript с этим VBS — консоль каждые 2 часа не мигает.
CreateObject("Wscript.Shell").Run """" & WScript.Arguments(0) & """", 0, False