[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$expected = [System.IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Codex\Tools\codex-more-details')).TrimEnd('\')
$actual = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
if ($actual -ne $expected) { throw 'Run this uninstaller from the installed codex-more-details folder.' }
$lockPath = Join-Path $actual 'launcher.lock'
if (Test-Path -LiteralPath $lockPath) {
    try { $lock = [System.IO.File]::Open($lockPath, 'Open', 'ReadWrite', 'None'); $lock.Dispose() }
    catch { throw 'Quit Codex and its patch launcher before uninstalling.' }
}
foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $shortcutPath = Join-Path $folder 'Codex More Details.lnk'
    if (Test-Path -LiteralPath $shortcutPath) {
        $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
        if ($shortcut.Arguments.Contains($actual)) { Remove-Item -LiteralPath $shortcutPath }
    }
}
# Both absolute paths were resolved and compared above before recursive deletion.
Remove-Item -LiteralPath $actual -Recurse -Force
Write-Output 'Removed Codex More Details. Open Codex using its original shortcut.'
