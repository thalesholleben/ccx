[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$taskName = 'Claude Monitor'
$taskPath = '\CCX\'

if ($Uninstall) {
  Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false -ErrorAction SilentlyContinue
  Write-Output 'Monitor permanente do CCX removido.'
  exit 0
}

$python = (Get-Command python -ErrorAction Stop).Source
$watchdog = Join-Path $PSScriptRoot 'ccx_watchdog.py'
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$watchdog`"" -WorkingDirectory $PSScriptRoot
$triggers = @(
  (New-ScheduledTaskTrigger -AtLogOn -User $user),
  (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650))
)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
  -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Days 365) `
  -MultipleInstances IgnoreNew `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -StartWhenAvailable

Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Action $action `
  -Trigger $triggers -Principal $principal -Settings $settings `
  -Description 'Mantem o monitor de contas Claude Code vivo e o relanca apos falha.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath
Write-Output 'Watchdog permanente do CCX instalado e iniciado. Logs: ~/.ccx/auto.log'
