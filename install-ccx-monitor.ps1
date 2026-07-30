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
$ccx = Join-Path $PSScriptRoot 'ccx.py'
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$ccx`" auto" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
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
  -Trigger $trigger -Principal $principal -Settings $settings `
  -Description 'Monitora e alterna automaticamente as contas do Claude Code.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath
Write-Output 'Monitor permanente do CCX instalado e iniciado. Logs: ~/.ccx/auto.log'
