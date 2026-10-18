# Регистрация плановой задачи hh-snpr: прогон КАЖДУЮ МИНУТУ (18.10 ~02:00).
# Причина: просмотры hh не лимитированы, лимит только на отклики; вакансии
# старше 10 минут не отправлаются (гейт в [send] max_age_minutes) — значит
# ловить надо в первые минуты после публикации.
# Запуск: powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
# (обычно не нужен руками: `python -m snpr job on` сам до-регистрирует)
# Джоба скрыта (wscript + run_hidden.vbs): консоль не мигает. Когда есть
# готовые к пуску — открывается видимое окно `snpr approve` + звук.

$root = Split-Path -Parent $PSScriptRoot
$vbs = Join-Path $root "scripts\run_hidden.vbs"
$cmd = Join-Path $root "scripts\sne_job.cmd"

$action = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument "`"$vbs`" `"$cmd`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

# только когда пользователь в системе: окно approve должно быть видимым
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName "hh-snpr" `
    -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Задача 'hh-snpr' зарегистрирована: каждую минуту (скрыто)."
Write-Host "Первый запуск через ~2 мин. Удаление: Unregister-ScheduledTask hh-snpr"