Codex More Details - Windows adaptation

Source: https://github.com/Mygod/codex-more-details
Tested on October 13, 2026 with Store package 26.1007.2314.0
(desktop version 26.1007.21434).

USE
Quit Codex completely, then open "Codex More Details" from the desktop or
Start menu. Select text in a Codex response to use "More details".
Ctrl+Shift+Space toggles Quick Chat while the app is focused.
The original Codex shortcut opens the unpatched app.

WHAT WAS INSTALLED
Documents\Codex\Tools\codex-more-details contains the launcher, patch generator,
injector, generated bundle cache, configuration, and launcher.log.
The signed app installation is unchanged. The launcher substitutes the patched
JavaScript responses through a private DevTools pipe. No TCP debugging port
is opened. Windows package context is preserved through
Invoke-CommandInDesktopPackage with PreventBreakaway.

Updates are checked on launch. A new app version causes the patch to be
regenerated and syntax checked. If compatibility checks fail, the launcher
stops and explains how to open the original app.

This is an unofficial workaround. Runtime bundle loading was tested in an
isolated, signed-out profile; the signed-in More details interaction still
needs verification after restarting the normal profile with this launcher.
Quick Chat remains subject to the app's existing capability checks.

REMOVE
Quit Codex, then run in PowerShell:
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$([Environment]::GetFolderPath('MyDocuments'))\Codex\Tools\codex-more-details\uninstall-windows.ps1"

REINSTALL
Run install-windows.ps1 from the supplied source folder with Python 3 and
Node.js 18+ available. It checks compatibility before creating shortcuts.
