[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$taskName = 'Claude Monitor'
$taskPath = '\CCX\'

if ($Uninstall) {
  Disable-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue | Out-Null
  Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue | Out-Null
  Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false -ErrorAction SilentlyContinue
  $ownerPath = Join-Path $env:USERPROFILE '.ccx\auto.lock\owner.json'
  $monitorStopped = $false
  for ($attempt = 0; $attempt -lt 10 -and -not $monitorStopped; $attempt++) {
    try {
      $owner = Get-Content -LiteralPath $ownerPath -Raw -ErrorAction Stop | ConvertFrom-Json
      $monitorPid = [int]$owner.pid
      $process = Get-CimInstance Win32_Process -Filter "ProcessId = $monitorPid" -ErrorAction Stop
      $scriptPath = [regex]::Escape((Join-Path $PSScriptRoot 'ccx.py'))
      if ($process.Name -match '^pythonw?\.exe$' -and
          $process.CommandLine -match $scriptPath -and
          $process.CommandLine -match '(^|\s)auto(\s|$)') {
        Stop-Process -Id $monitorPid -Force -ErrorAction Stop
        $monitorStopped = $true
      }
    } catch {
      # A tarefa foi removida de qualquer forma; nao force um PID nao verificado.
    }
    if (-not $monitorStopped -and $attempt -lt 9) { Start-Sleep -Milliseconds 200 }
  }
  if ($monitorStopped) {
    Write-Output 'Monitor permanente do CCX removido e processo atual encerrado.'
  } else {
    Write-Output 'Monitor permanente do CCX removido. Nenhum processo CCX verificavel precisou ser encerrado.'
  }
  exit 0
}

$python = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
if (-not $python.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase) -or
    $python -like "$env:LOCALAPPDATA\Microsoft\WindowsApps\*") {
  throw "Python executavel invalido para o monitor: $python"
}
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) {
  throw "pythonw.exe nao encontrado ao lado de $python"
}
$probe = Start-Process -FilePath $pythonw -ArgumentList '-c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"' -Wait -PassThru -NoNewWindow
if ($probe.ExitCode -ne 0) {
  throw "Python 3.10+ exigido para o monitor: $python"
}
$watchdog = Join-Path $PSScriptRoot 'ccx_watchdog.py'
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$watchdog`"" -WorkingDirectory $PSScriptRoot
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
Enable-ScheduledTask -TaskName $taskName -TaskPath $taskPath | Out-Null
Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath
Write-Output 'Watchdog permanente do CCX habilitado e iniciado. Logs: ~/.ccx/auto.log'
