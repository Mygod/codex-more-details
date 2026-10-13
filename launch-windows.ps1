[CmdletBinding()]
param([switch]$PackageContext, [switch]$CheckOnly, [string]$SmokeTestDir)

$ErrorActionPreference = 'Stop'
$supportDir = $PSScriptRoot
$logPath = Join-Path $supportDir 'launcher.log'
$launchLock = $null
function Write-LaunchLog([string]$Text) {
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $Text" -Encoding UTF8
}
function Invoke-Logged([string]$FilePath, [string[]]$Arguments) {
    # Windows PowerShell 5.1 turns redirected stderr into errors, so 'Stop' would abort at the first line.
    $ErrorActionPreference = 'Continue'
    & $FilePath @Arguments 2>&1 | ForEach-Object { "$_" } | Out-File -LiteralPath $logPath -Encoding utf8 -Append
}
function Show-LaunchMessage([string]$Text) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($Text, 'Codex More Details', 'OK', 'Information') | Out-Null
}

try {
    $settings = Get-Content -LiteralPath (Join-Path $supportDir 'windows-config.json') -Raw | ConvertFrom-Json
    $package = Get-AppxPackage -Name 'OpenAI.Codex' | Sort-Object Version -Descending | Select-Object -First 1
    if (-not $package) { throw 'The Microsoft Store Codex app is not installed.' }
    $appDir = Join-Path $package.InstallLocation 'app'
    $binary = Join-Path $appDir 'ChatGPT.exe'
    $patcher = Join-Path $supportDir 'more_details_patch.py'
    $injector = Join-Path $supportDir 'inject.mjs'
    foreach ($required in @($settings.node, $settings.python, $binary, $patcher, $injector)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Required file missing: $required" }
    }
    $env:PATH = (Split-Path -Parent $settings.node) + ';' + $env:PATH
    $patchHash = (Get-FileHash -LiteralPath $patcher -Algorithm SHA256).Hash.Substring(0, 16)
    $cacheDir = Join-Path $supportDir "cache\$($package.Version)-$patchHash"
    $manifestPath = Join-Path $cacheDir 'verified.json'
    $cacheValid = $false
    if (Test-Path -LiteralPath $manifestPath) {
        try {
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
            $cacheValid = $manifest.packageFullName -eq $package.PackageFullName -and @($manifest.bundles).Count -eq 2
            foreach ($bundle in $manifest.bundles) {
                $cachedFile = Join-Path $cacheDir $bundle.name
                if (-not (Test-Path -LiteralPath $cachedFile) -or
                    (Get-FileHash -LiteralPath $cachedFile -Algorithm SHA256).Hash -ne $bundle.sha256) {
                    $cacheValid = $false
                }
            }
        } catch { $cacheValid = $false }
    }
    if (-not $cacheValid) {
        Write-LaunchLog "Preparing patch for $($package.PackageFullName)"
        Invoke-Logged $settings.python @('-I', $patcher, '--app', $appDir, '--emit', $cacheDir)
        if ($LASTEXITCODE -ne 0) { throw 'This app version did not pass the patch compatibility checks. See launcher.log.' }
        $bundles = @(Get-ChildItem -LiteralPath $cacheDir -Filter '*.js' | ForEach-Object {
            @{ name = $_.Name; sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
        })
        if ($bundles.Count -ne 2) { throw 'Expected exactly two validated bundles.' }
        @{ packageFullName = $package.PackageFullName; bundles = $bundles } |
            ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    }
    if ($CheckOnly) {
        Write-Output "Verified Windows patch: $($package.Version); $cacheDir"
        exit 0
    }
    if (-not $SmokeTestDir) {
        $runningApp = @(Get-CimInstance Win32_Process -Filter "Name='ChatGPT.exe'" | Where-Object {
            $_.ExecutablePath -eq $binary -and $_.CommandLine -notmatch '--type='
        })
        if ($runningApp.Count -gt 0) {
            Show-LaunchMessage 'Codex is already open. Quit Codex completely, then open Codex More Details. No running chats were interrupted.'
            exit 0
        }
    }
    if (-not $PackageContext) {
        $packageArgs = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -PackageContext'
        if ($SmokeTestDir) { $packageArgs += ' -SmokeTestDir "' + $SmokeTestDir + '"' }
        Invoke-CommandInDesktopPackage -PackageFamilyName $package.PackageFamilyName -AppId 'App' `
            -Command "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" `
            -Args $packageArgs -PreventBreakaway -ErrorAction Stop
        exit 0
    }
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class MoreDetailsPackageIdentity {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetCurrentPackageFullName(ref uint length, StringBuilder name);
}
'@
    [uint32]$identityLength = 0
    $identityResult = [MoreDetailsPackageIdentity]::GetCurrentPackageFullName([ref]$identityLength, $null)
    if ($identityResult -ne 122) { throw "Windows package identity is missing (code $identityResult)." }
    $identityName = New-Object System.Text.StringBuilder ([int]$identityLength)
    $identityResult = [MoreDetailsPackageIdentity]::GetCurrentPackageFullName([ref]$identityLength, $identityName)
    if ($identityResult -ne 0 -or $identityName.ToString() -ne $package.PackageFullName) {
        throw 'The launcher does not have the expected Codex package identity.'
    }
    Write-LaunchLog "Verified package identity: $identityName"
    try {
        $launchLock = [System.IO.File]::Open((Join-Path $supportDir 'launcher.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    } catch {
        Write-LaunchLog 'A patch launcher is already running.'
        exit 0
    }
    Write-LaunchLog "Starting $($package.PackageFullName) with the runtime patch"
    $injectArgs = @($injector, '--app', $appDir, '--patch-dir', $cacheDir)
    if ($SmokeTestDir) { $injectArgs += @('--smoke-test-dir', $SmokeTestDir) }
    Invoke-Logged $settings.node $injectArgs
    $injectExit = $LASTEXITCODE
    if ($SmokeTestDir) { $injectExit | Set-Content -LiteralPath (Join-Path $supportDir 'smoke-test.exit') }
    if ($injectExit -ne 0) { throw "The patch launcher exited with code $injectExit. See launcher.log." }
} catch {
    Write-LaunchLog "ERROR: $($_.Exception.Message)"
    if ($CheckOnly -or $SmokeTestDir) { Write-Error $_; exit 1 }
    Show-LaunchMessage ("The More Details launcher could not start: " + $_.Exception.Message + "`n`nOpen Codex normally from its original shortcut.")
    exit 1
} finally {
    if ($null -ne $launchLock) { $launchLock.Dispose() }
}
