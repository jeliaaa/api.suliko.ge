<#
.SYNOPSIS
    One-time setup for order file storage on the API server (STORAGE_BACKEND=local).

.DESCRIPTION
    Run once, as Administrator, on the server that runs the SulikoAPI service:

      1. Creates the storage folder and locks it down to SYSTEM, Administrators
         and the account the SulikoAPI service runs as. Inheritance is cut, so
         ordinary users of the machine cannot read customers' documents.
      2. Warns if the drive holding it is not BitLocker-encrypted.
      3. Registers a daily scheduled task that runs `suliko.cli purge-files`,
         which deletes the bytes of files removed more than FILE_RETENTION_DAYS
         ago. Safe to re-run: the task is replaced, the folder kept.

    It does NOT edit .env. Set these yourself, then restart the service:

      STORAGE_BACKEND=local
      STORAGE_LOCAL_DIR=<the -StorageDir you pass here>

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\setup-file-storage.ps1 -StorageDir D:\suliko-files
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$StorageDir,

    # The api.suliko.ge checkout. Defaults to the folder above this script
    # (worked out below: Windows PowerShell 5.1 leaves $PSScriptRoot empty
    # while parameter defaults are evaluated).
    [string]$RepoDir = "",

    [string]$ServiceName = "SulikoAPI",

    # When the daily purge runs (server local time).
    [string]$PurgeAt = "03:30"
)

$ErrorActionPreference = "Stop"

if (-not $RepoDir) {
    $RepoDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an elevated PowerShell (Run as Administrator)."
}

$python = Join-Path $RepoDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "No virtualenv at $python. Pass -RepoDir pointing at the api.suliko.ge checkout."
}

# ── 1. The folder ──────────────────────────────────────────────────────────
$StorageDir = [IO.Path]::GetFullPath($StorageDir)
$repoFull = [IO.Path]::GetFullPath($RepoDir).TrimEnd('\') + '\'
if (($StorageDir.TrimEnd('\') + '\').StartsWith($repoFull, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Keep the storage folder outside the code checkout ($RepoDir): a 'git clean' must never be able to touch customers' files."
}
New-Item -ItemType Directory -Force -Path $StorageDir | Out-Null
Write-Host "Folder: $StorageDir"

$service = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
if ($null -eq $service) {
    Write-Warning "No Windows service named '$ServiceName'. Granting only SYSTEM and Administrators."
    $serviceAccount = $null
} else {
    $serviceAccount = $service.StartName
    Write-Host "Service $ServiceName runs as: $serviceAccount"
}

$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)   # cut inheritance, drop inherited rules
$inherit = [Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
$none = [Security.AccessControl.PropagationFlags]::None
$identities = @("NT AUTHORITY\SYSTEM", "BUILTIN\Administrators")
if ($serviceAccount -and $serviceAccount -notin @("LocalSystem", "NT AUTHORITY\SYSTEM")) {
    $identities += $serviceAccount
}
foreach ($identity in $identities) {
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, "FullControl", $inherit, $none, "Allow")
    $acl.AddAccessRule($rule)
}
Set-Acl -Path $StorageDir -AclObject $acl
Write-Host "Access limited to: $($identities -join ', ')"

# ── 2. Encryption at rest ──────────────────────────────────────────────────
$drive = [IO.Path]::GetPathRoot($StorageDir).TrimEnd('\')
try {
    $volume = Get-BitLockerVolume -MountPoint $drive -ErrorAction Stop
    if ($volume.ProtectionStatus -eq "On") {
        Write-Host "BitLocker: ON for $drive"
    } else {
        Write-Warning "BitLocker is OFF for $drive. These are customers' identity documents - encrypt the drive."
    }
} catch {
    Write-Warning "Could not read BitLocker status for $drive ($($_.Exception.Message)). Check encryption by hand."
}

# ── 3. The daily purge ─────────────────────────────────────────────────────
$taskName = "Suliko - purge removed order files"
$logDir = Join-Path $RepoDir "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "purge-files.log"

# cmd /c so the output lands in a log file; the working directory is the
# checkout so the app finds its .env.
$command = "`"$python`" -m suliko.cli purge-files >> `"$log`" 2>&1"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $command" -WorkingDirectory $RepoDir
$trigger = New-ScheduledTaskTrigger -Daily -At $PurgeAt
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$runAs = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $runAs -Description "Deletes the bytes of order files removed longer ago than FILE_RETENTION_DAYS." `
    -Force | Out-Null
Write-Host "Scheduled task '$taskName' runs daily at $PurgeAt (log: $log)"

Write-Host ""
Write-Host "Next:"
Write-Host "  1. In $RepoDir\.env set:"
Write-Host "       STORAGE_BACKEND=local"
Write-Host "       STORAGE_LOCAL_DIR=$StorageDir"
Write-Host "  2. .venv\Scripts\python.exe -m suliko.cli check     (expects: write, read and delete all work)"
Write-Host "  3. Restart-Service $ServiceName"
Write-Host "  4. Add $StorageDir to the server backup, next to the database dump."
