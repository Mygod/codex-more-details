[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$supportDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Codex\Tools\codex-more-details'
$node = (Get-Command node.exe -ErrorAction Stop).Source
# ASCII-escaped JSON survives Python's ANSI stdout being decoded as OEM by Windows PowerShell.
$python = & python -c 'import json, sys; print(json.dumps(sys.executable))' | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $python -or -not (Test-Path -LiteralPath $python)) { throw 'Working Python 3 is required.' }
& $node -e 'process.exit(parseInt(process.versions.node) < 18 ? 1 : 0)'
if ($LASTEXITCODE -ne 0) { throw 'Node.js 18 or newer is required.' }
$package = Get-AppxPackage -Name 'OpenAI.Codex' | Sort-Object Version -Descending | Select-Object -First 1
if (-not $package) { throw 'The Microsoft Store Codex app is not installed.' }
$stage = Join-Path $PSScriptRoot '.windows-install-check'
& $python -I (Join-Path $PSScriptRoot 'more_details_patch.py') --app (Join-Path $package.InstallLocation 'app') --emit $stage
if ($LASTEXITCODE -ne 0) { throw 'Compatibility check failed; no launcher was installed.' }
New-Item -ItemType Directory -Force -Path $supportDir | Out-Null
foreach ($name in @('more_details_patch.py', 'inject.mjs', 'launch-windows.ps1', 'uninstall-windows.ps1', 'WINDOWS-README.txt')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $supportDir $name) -Force
}
@{ node = $node; python = $python } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $supportDir 'windows-config.json') -Encoding UTF8
& "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass `
    -File (Join-Path $supportDir 'launch-windows.ps1') -CheckOnly
if ($LASTEXITCODE -ne 0) { throw 'Installed launcher validation failed.' }
$shortcutShell = New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $shortcut = $shortcutShell.CreateShortcut((Join-Path $folder 'Codex More Details.lnk'))
    $shortcut.TargetPath = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
    $shortcut.Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $supportDir 'launch-windows.ps1') + '"'
    $shortcut.WorkingDirectory = $supportDir
    $shortcut.IconLocation = (Join-Path $package.InstallLocation 'app\ChatGPT.exe') + ',0'
    $shortcut.Description = 'Codex with the restored More details selection action'
    $shortcut.WindowStyle = 7
    $shortcut.Save()
}
Write-Output "Installed Codex More Details to $supportDir"
Write-Output 'Quit Codex, then use the Codex More Details desktop or Start menu shortcut.'
