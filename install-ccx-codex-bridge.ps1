[CmdletBinding()]
param(
    [switch]$Uninstall,
    [string]$SettingsPath = (Join-Path $env:APPDATA "Code\User\settings.json"),
    [string]$InstallDir = (Join-Path $env:USERPROFILE ".ccx\bin"),
    [string]$CodexExecutable,
    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Launcher = Join-Path $InstallDir "ccx-codex-bridge.exe"
$Config = Join-Path $InstallDir "ccx-codex-bridge.cfg"
$State = Join-Path $InstallDir "ccx-codex-bridge.install.json"
$SettingsBackup = $SettingsPath + ".ccx-codex-bridge.bak"
$Source = Join-Path $RepoDir "ccx_codex_bridge_launcher.cs"
$BridgeScript = Join-Path $RepoDir "ccx_codex_bridge.py"
$SettingName = "chatgpt.cliExecutable"
$IgnoredSettingName = "settingsSync.ignoredSettings"
$script:IgnoredSettingAdded = $false

function Write-AtomicText([string]$Path, [string]$Text, [string]$BackupPath = "") {
    $parent = Split-Path -Parent $Path
    [System.IO.Directory]::CreateDirectory($parent) | Out-Null
    $temp = Join-Path $parent (([System.IO.Path]::GetFileName($Path)) + "." + [guid]::NewGuid().ToString("N") + ".tmp")
    [System.IO.File]::WriteAllText($temp, $Text, $Utf8NoBom)
    $keepBackup = -not [string]::IsNullOrEmpty($BackupPath)
    $persistentBackupExists = $keepBackup -and [System.IO.File]::Exists($BackupPath)
    $backup = if ($keepBackup -and -not $persistentBackupExists) {
        $BackupPath
    } else {
        Join-Path $parent (([System.IO.Path]::GetFileName($Path)) + "." + [guid]::NewGuid().ToString("N") + ".bak")
    }
    try {
        if ([System.IO.File]::Exists($Path)) {
            [System.IO.File]::Replace($temp, $Path, $backup)
            if (-not $keepBackup -or $persistentBackupExists) {
                [System.IO.File]::Delete($backup)
            }
        } else {
            [System.IO.File]::Move($temp, $Path)
        }
    } finally {
        if ([System.IO.File]::Exists($temp)) {
            [System.IO.File]::Delete($temp)
        }
        if ((-not $keepBackup -or $persistentBackupExists) -and [System.IO.File]::Exists($backup)) {
            [System.IO.File]::Delete($backup)
        }
    }
}

function Get-SettingsText {
    if (-not [System.IO.File]::Exists($SettingsPath)) {
        return "{}"
    }
    return [System.IO.File]::ReadAllText($SettingsPath, [System.Text.Encoding]::UTF8)
}

function Find-Setting([string]$Text) {
    $pattern = '(?m)^(?<indent>[ \t]*)"chatgpt\.cliExecutable"(?<colon>[ \t]*:[ \t]*)"(?<value>(?:\\.|[^"\\])*)"'
    return [regex]::Match($Text, $pattern)
}

function Decode-JsonString([string]$EncodedValue) {
    return ('"' + $EncodedValue + '"' | ConvertFrom-Json)
}

function Find-NextJsoncToken([string]$Text, [int]$Start) {
    $index = $Start
    while ($index -lt $Text.Length) {
        $character = $Text[$index]
        if ([char]::IsWhiteSpace($character) -or [int]$character -eq 0xFEFF) {
            $index++
            continue
        }
        if ($character -eq '/' -and $index + 1 -lt $Text.Length) {
            $next = $Text[$index + 1]
            if ($next -eq '/') {
                $newline = $Text.IndexOf("`n", $index + 2)
                if ($newline -lt 0) {
                    return -1
                }
                $index = $newline + 1
                continue
            }
            if ($next -eq '*') {
                $close = $Text.IndexOf('*/', $index + 2, [System.StringComparison]::Ordinal)
                if ($close -lt 0) {
                    throw "settings.json tem comentario de bloco incompleto. Nada foi alterado."
                }
                $index = $close + 2
                continue
            }
        }
        return $index
    }
    return -1
}

function Find-RootObjectStart([string]$Text) {
    $index = Find-NextJsoncToken $Text 0
    if ($index -lt 0 -or $Text[$index] -ne '{') {
        throw "settings.json nao contem um objeto JSONC raiz. Nada foi alterado."
    }
    return $index
}

function Insert-RootSetting([string]$Text, [string]$Name, [string]$EncodedValue) {
    $open = Find-RootObjectStart $Text
    $next = Find-NextJsoncToken $Text ($open + 1)
    if ($next -lt 0) {
        throw "settings.json tem objeto raiz incompleto. Nada foi alterado."
    }
    $comma = if ($Text[$next] -eq '}') { "" } else { "," }
    $newline = if ($Text.Contains("`r`n")) { "`r`n" } else { "`n" }
    return $Text.Substring(0, $open + 1) + $newline + "    `"$Name`": $EncodedValue$comma" + $Text.Substring($open + 1)
}

function Set-Setting([string]$Text, [string]$Value) {
    $encoded = $Value | ConvertTo-Json -Compress
    $encodedValue = $encoded.Substring(1, $encoded.Length - 2)
    $match = Find-Setting $Text
    if ($match.Success) {
        $group = $match.Groups["value"]
        return $Text.Substring(0, $group.Index) + $encodedValue + $Text.Substring($group.Index + $group.Length)
    }
    return Insert-RootSetting $Text $SettingName $encoded
}

function Remove-Setting([string]$Text) {
    $pattern = '(?m)^[ \t]*"chatgpt\.cliExecutable"[ \t]*:[ \t]*"(?:\\.|[^"\\])*"[ \t]*,?[ \t]*(?:\r?\n)?'
    return [regex]::Replace($Text, $pattern, "", 1)
}

function Find-IgnoredSettings([string]$Text) {
    $pattern = '(?ms)^(?<indent>[ \t]*)"settingsSync\.ignoredSettings"(?<colon>[ \t]*:[ \t]*)(?<value>\[(?:[^\]"]|"(?:\\.|[^"\\])*")*\])'
    return [regex]::Match($Text, $pattern)
}

function Ensure-IgnoredSetting([string]$Text) {
    $script:IgnoredSettingAdded = $false
    $match = Find-IgnoredSettings $Text
    if ($match.Success) {
        try {
            $values = @($match.Groups["value"].Value | ConvertFrom-Json)
        } catch {
            throw "settingsSync.ignoredSettings usa JSONC complexo; nada foi alterado."
        }
        if ($values -contains $SettingName) {
            return $Text
        }
        $values += $SettingName
        $encoded = ConvertTo-Json -InputObject @($values) -Compress
        $group = $match.Groups["value"]
        $script:IgnoredSettingAdded = $true
        return $Text.Substring(0, $group.Index) + $encoded + $Text.Substring($group.Index + $group.Length)
    }
    if ($Text -match '"settingsSync\.ignoredSettings"') {
        throw "Nao foi possivel preservar settingsSync.ignoredSettings; nada foi alterado."
    }
    $encodedName = $SettingName | ConvertTo-Json -Compress
    $script:IgnoredSettingAdded = $true
    return Insert-RootSetting $Text $IgnoredSettingName "[$encodedName]"
}

function Remove-IgnoredSettingValue([string]$Text) {
    $match = Find-IgnoredSettings $Text
    if (-not $match.Success) {
        return $Text
    }
    try {
        $values = @($match.Groups["value"].Value | ConvertFrom-Json)
    } catch {
        Write-Warning "settingsSync.ignoredSettings mudou; a lista foi preservada."
        return $Text
    }
    $remaining = @($values | Where-Object { $_ -ne $SettingName })
    if ($remaining.Count -eq 0) {
        $pattern = '(?ms)^[ \t]*"settingsSync\.ignoredSettings"[ \t]*:[ \t]*\[(?:[^\]"]|"(?:\\.|[^"\\])*")*\][ \t]*,?[ \t]*(?:\r?\n)?'
        return [regex]::Replace($Text, $pattern, "", 1)
    }
    $encoded = ConvertTo-Json -InputObject @($remaining) -Compress
    $group = $match.Groups["value"]
    return $Text.Substring(0, $group.Index) + $encoded + $Text.Substring($group.Index + $group.Length)
}

function Resolve-Python {
    if ($PythonExecutable) {
        $candidate = $PythonExecutable
    } else {
        $candidate = Get-Command python.exe -CommandType Application -ErrorAction Stop |
            Where-Object { $_.Source -notlike "*\WindowsApps\*" } |
            Select-Object -First 1 -ExpandProperty Source
    }
    if (-not $candidate -or -not [System.IO.File]::Exists($candidate)) {
        throw "Python real nao encontrado. Informe -PythonExecutable."
    }
    & $candidate -c "import sys; raise SystemExit(sys.version_info < (3, 10))"
    if ($LASTEXITCODE -ne 0) {
        throw "O bridge requer Python 3.10 ou mais novo."
    }
    return [System.IO.Path]::GetFullPath($candidate)
}

function Resolve-Codex {
    if ($CodexExecutable) {
        $candidate = [System.IO.Path]::GetFullPath($CodexExecutable)
    } else {
        $extensionsRoot = Join-Path $env:USERPROFILE ".vscode\extensions"
        $manifest = Join-Path $extensionsRoot "extensions.json"
        $candidate = $null
        if ([System.IO.File]::Exists($manifest)) {
            $entries = @((Get-Content -Raw -LiteralPath $manifest | ConvertFrom-Json) |
                Where-Object {
                    $_.identifier.id -eq "openai.chatgpt" -and
                    $_.metadata.targetPlatform -eq "win32-x64"
                } |
                Sort-Object { [int64]$_.metadata.installedTimestamp } -Descending)
            foreach ($entry in $entries) {
                $root = if ([System.IO.Path]::IsPathRooted($entry.relativeLocation)) {
                    $entry.relativeLocation
                } else {
                    Join-Path $extensionsRoot $entry.relativeLocation
                }
                $path = Join-Path $root "bin\windows-x86_64\codex.exe"
                if ([System.IO.File]::Exists($path)) {
                    $candidate = $path
                    break
                }
            }
        }
        if (-not $candidate) {
            $candidate = Get-ChildItem -Path (Join-Path $extensionsRoot "openai.chatgpt-*-win32-x64\bin\windows-x86_64\codex.exe") -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTimeUtc -Descending |
                Select-Object -First 1 -ExpandProperty FullName
        }
    }
    if (-not $candidate -or -not [System.IO.File]::Exists($candidate)) {
        throw "codex.exe da extensao oficial nao encontrado. Informe -CodexExecutable."
    }
    $candidate = [System.IO.Path]::GetFullPath($candidate)
    if (-not $CodexExecutable) {
        $extensionsRootFull = [System.IO.Path]::GetFullPath($extensionsRoot).TrimEnd('\') + '\'
        if (-not $candidate.StartsWith($extensionsRootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "O caminho do Codex escapou da raiz de extensoes do VS Code."
        }
        $extensionDir = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $candidate))
        $packagePath = Join-Path $extensionDir "package.json"
        if (-not [System.IO.File]::Exists($packagePath)) {
            throw "Manifesto da extensao OpenAI nao encontrado."
        }
        $package = Get-Content -Raw -LiteralPath $packagePath | ConvertFrom-Json
        if ($package.publisher -ne "OpenAI" -or $package.name -ne "chatgpt") {
            throw "O binario nao pertence ao pacote oficial openai.chatgpt."
        }
    }
    $signature = Get-AuthenticodeSignature -FilePath $candidate
    if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "OpenAI") {
        throw "O codex.exe encontrado nao tem assinatura valida da OpenAI."
    }
    return $candidate
}

if ($Uninstall) {
    if (-not [System.IO.File]::Exists($State)) {
        Write-Host "Bridge CCX nao esta instalado neste diretorio."
        exit 0
    }
    $saved = Get-Content -Raw -LiteralPath $State | ConvertFrom-Json
    $text = Get-SettingsText
    $match = Find-Setting $text
    $current = if ($match.Success) { Decode-JsonString $match.Groups["value"].Value } else { $null }
    if ($current -eq $saved.installedValue) {
        if ($saved.previousExists) {
            $text = Set-Setting $text ([string]$saved.previousValue)
        } else {
            $text = Remove-Setting $text
        }
        if ($saved.ignoredSettingAdded) {
            $text = Remove-IgnoredSettingValue $text
        }
        Write-AtomicText $SettingsPath $text
        $savedBackup = [string]$saved.settingsBackup
        if ($savedBackup -and [System.IO.File]::Exists($savedBackup)) {
            [System.IO.File]::Delete($savedBackup)
        }
    } else {
        Write-Warning "chatgpt.cliExecutable mudou depois da instalacao; o valor do usuario foi preservado."
        exit 1
    }
    foreach ($path in @($Launcher, $Config, $State)) {
        if ([System.IO.File]::Exists($path)) {
            try {
                [System.IO.File]::Delete($path)
            } catch {
                Write-Warning "Nao foi possivel remover $path; ele pode estar em uso ate o reload."
            }
        }
    }
    Write-Host "Bridge CCX removido. Recarregue a janela do VS Code para aplicar."
    exit 0
}

if (-not [System.IO.File]::Exists($Source) -or -not [System.IO.File]::Exists($BridgeScript)) {
    throw "Arquivos do bridge nao encontrados ao lado do instalador."
}

$settingsText = Get-SettingsText
if ([System.IO.File]::Exists($SettingsBackup) -and -not [System.IO.File]::Exists($State)) {
    throw "Existe um backup de instalacao anterior em $SettingsBackup; nada foi alterado."
}
if ($settingsText -match '(?m)^[ \t]*"chatgpt\.runCodexInWindowsSubsystemForLinux"[ \t]*:[ \t]*true\b') {
    throw "O Codex esta configurado para WSL; este bridge Windows nao sera instalado."
}
$existing = Find-Setting $settingsText
if ($existing.Success) {
    $configuredLauncher = Decode-JsonString $existing.Groups["value"].Value
    if ($configuredLauncher -ne $Launcher) {
        throw "chatgpt.cliExecutable ja aponta para outro executavel; nada foi alterado."
    }
}

$python = Resolve-Python
$codex = Resolve-Codex
$cscCandidates = @(
    "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
)
$csc = $cscCandidates | Where-Object { [System.IO.File]::Exists($_) } | Select-Object -First 1
if (-not $csc) {
    throw "Compilador C# do Windows nao encontrado."
}

[System.IO.Directory]::CreateDirectory($InstallDir) | Out-Null
$tempExe = Join-Path $InstallDir ("ccx-codex-bridge." + [guid]::NewGuid().ToString("N") + ".tmp.exe")
try {
    & $csc /nologo /optimize+ /target:exe "/out:$tempExe" $Source
    if ($LASTEXITCODE -ne 0 -or -not [System.IO.File]::Exists($tempExe)) {
        throw "Falha ao compilar o launcher nativo do bridge."
    }
    if ([System.IO.File]::Exists($Launcher)) {
        $backupExe = $Launcher + "." + [guid]::NewGuid().ToString("N") + ".bak"
        [System.IO.File]::Replace($tempExe, $Launcher, $backupExe)
        [System.IO.File]::Delete($backupExe)
    } else {
        [System.IO.File]::Move($tempExe, $Launcher)
    }
} finally {
    if ([System.IO.File]::Exists($tempExe)) {
        [System.IO.File]::Delete($tempExe)
    }
}

foreach ($pathValue in @($python, $BridgeScript, $codex)) {
    if ($pathValue.Contains("`r") -or $pathValue.Contains("`n")) {
        throw "Caminho invalido para o arquivo de configuracao do bridge."
    }
}
Write-AtomicText $Config (($python, $BridgeScript, $codex) -join "`n")

$oldState = if ([System.IO.File]::Exists($State)) {
    Get-Content -Raw -LiteralPath $State | ConvertFrom-Json
} else {
    $null
}
if ($oldState -and $oldState.installedValue -eq $Launcher) {
    $previousExists = [bool]$oldState.previousExists
    $previousValue = [string]$oldState.previousValue
} else {
    $previousExists = $existing.Success
    $previousValue = if ($existing.Success) {
        Decode-JsonString $existing.Groups["value"].Value
    } else {
        ""
    }
}

$updatedSettings = Set-Setting $settingsText $Launcher
$updatedSettings = Ensure-IgnoredSetting $updatedSettings
$ignoredSettingAdded = $script:IgnoredSettingAdded
if ($oldState -and $oldState.installedValue -eq $Launcher -and $oldState.ignoredSettingAdded) {
    $ignoredSettingAdded = $true
}

$installState = [ordered]@{
    version = 1
    installedValue = $Launcher
    previousExists = $previousExists
    previousValue = $previousValue
    ignoredSettingAdded = $ignoredSettingAdded
    settingsPath = [System.IO.Path]::GetFullPath($SettingsPath)
    settingsBackup = [System.IO.Path]::GetFullPath($SettingsBackup)
}
Write-AtomicText $State (($installState | ConvertTo-Json) + "`n")
if ($updatedSettings -ne $settingsText) {
    Write-AtomicText $SettingsPath $updatedSettings $SettingsBackup
}

Write-Host "Bridge CCX instalado em $Launcher"
Write-Host "Recarregue a janela do VS Code uma vez; a sessao atual nao foi interrompida."
