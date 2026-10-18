# ChatGPT More Details

Brings back **More details** in the Codex text-selection menu of the ChatGPT desktop app on macOS and Windows, and adds a keyboard shortcut for Quick Chat. This is a workaround for [openai/codex#52483](https://github.com/openai/codex/issues/52483), where ChatGPT 26.1007.x dropped both.

> **Not affiliated with OpenAI.** This is an unofficial, community workaround, not endorsed or supported by OpenAI. Generated app bundles are kept on your own computer and are not distributed in this repository. The signed app installation is never modified.

## Windows installation

Requires the Microsoft Store **OpenAI.Codex** package, Python 3, Node.js 18 or later, and Windows PowerShell 5.1 with the Appx module. Run the installer as your normal user. It supports the Codex app that is now branded ChatGPT; the separate legacy `OpenAI.ChatGPT-Desktop` package is not a target.

```powershell
git clone https://github.com/Mygod/codex-more-details.git
cd codex-more-details
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1
```

Quit Codex completely, then open **Codex More Details** from the desktop or Start menu. Select text in a Codex response and choose **More details**. **Ctrl+Shift+Space** toggles Quick Chat while the app is focused; Alt+Space remains available for the Windows system menu.

The installer validates the patch before installing scripts under your Windows Documents folder at `Codex\Tools\codex-more-details`. It creates separate shortcuts. Opening the original Codex shortcut starts the app without this workaround. An already-running Codex session is left running; the launcher tells you to quit it first.

The Windows launcher preserves the Store package identity using `Invoke-CommandInDesktopPackage -PreventBreakaway`, verifies that identity, and substitutes the two bundles over a private DevTools pipe. It does not open a TCP debugging port. It checks cached bundle hashes and regenerates the patch when the app version or patch generator changes. If compatibility checks fail, it stops and tells you to use the original shortcut.

### Windows validation and limitations

Tested on Store package **26.1007.2314.0**, desktop **26.1007.21434**, with Python 3.11 and Node.js 22. Both generated bundles passed `node --check`. An isolated, signed-out app loaded both patched bundles and evaluated the restored `__qcMDRun` and `__qcCan` exports. The launcher also verified its Store package identity.

The signed-in selection-to-Quick-Chat interaction has **not yet been verified**. Quick Chat still depends on the app's existing capability checks. Other Windows versions, architectures, and later app builds have not been tested.

For logs, open `launcher.log` in the installation folder. To recheck patch compatibility without starting the app:

```powershell
$patchDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Codex\Tools\codex-more-details'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$patchDir\launch-windows.ps1" -CheckOnly
```

To uninstall, quit Codex and run:

```powershell
$patchDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Codex\Tools\codex-more-details'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$patchDir\uninstall-windows.ps1"
```

This removes the workaround and its shortcuts. The original app and its chats are unaffected. See [WINDOWS-README.txt](WINDOWS-README.txt) for a compact reference.

## What it restores

In 26.1002, selecting text in a Codex conversation offered **More details**. It opened Quick Chat and asked "Tell me more about this", with the selection and the message it came from attached as a targeted reply and the Codex thread as context. 26.1007 still has the button in its shared selection menu, but the Codex conversation view no longer passes it a handler, and the helper that started Quick Chat from selected text is gone. 26.1007 also removed the standalone Quick Chat window.

With this patch:

- **More details** is back in the Codex selection menu and behaves like 26.1002. It opens the in-window Quick Chat panel.
- **⌘⇧Space** toggles the Quick Chat panel. **⌥Space** also works if you clear ChatGPT's global "Toggle Mini" shortcut, which otherwise takes that key first.

## macOS requirements

- macOS, with the ChatGPT desktop app at `/Applications/ChatGPT.app`
- Xcode Command Line Tools (`xcode-select --install`), for `cc` and `/usr/bin/python3`
- Node.js 18 or later (for example `brew install node`)

## macOS installation

```bash
git clone https://github.com/Mygod/codex-more-details.git
```

```bash
cd codex-more-details && ./install.sh
```

Then start ChatGPT with **ChatGPT More Details**, which is in `~/Applications` and findable in Spotlight. Put it in your Dock in place of ChatGPT. If ChatGPT is already running without the patch, the launcher offers to quit and reopen it.

`install.sh` checks the requirements, does a dry run to confirm the patch applies to your ChatGPT version, compiles a small helper, and then installs:

- `~/Library/Application Support/codex-more-details/`: the scripts, the helper, the patch cache and `launcher.log`
- `~/Applications/ChatGPT More Details.app`: a small AppleScript launcher

If a check, the dry run or the compile fails, nothing is installed. To use a specific Node.js, run `NODE=/path/to/node ./install.sh`. The Node.js path is saved at install time, so re-run `install.sh` if it moves (for example after switching versions with nvm). Re-running `install.sh` is also how you update.

## macOS uninstall

```bash
./uninstall.sh
```

This removes the support folder and the launcher. ChatGPT itself was never changed, so there is nothing else to undo. If ChatGPT is running with the patch, it stays patched until you quit it.

## How the macOS launcher works

1. **ChatGPT More Details.app** runs `launch.sh` in the background.
2. **`launch.sh`** reads ChatGPT's version. The first time it sees a version (or a changed patch script), it runs `more_details_patch.py` and caches the result. If ChatGPT is already running unpatched, it then offers to quit and reopen it.
3. **`more_details_patch.py`** reads the two webview bundles it needs (`app-initial-*.js` and `app-primary-*.js`) out of `app.asar`, read-only. It finds the minified names it needs by regex instead of hard-coding them, adds the More details handler and the shortcut, and checks the output with `node --check`. If any pattern doesn't match exactly once, it fails and writes nothing.
4. **`inject.mjs`** starts ChatGPT with `--remote-debugging-pipe`. This speaks the DevTools protocol over a private pipe that only the launcher holds, with no TCP port opened. It auto-attaches to each new page and holds it before any script runs (`waitForDebuggerOnStart`). It then intercepts responses for those two files (Fetch interception at the Response stage) and serves the patched copies instead.
5. **`spawn_disclaim`** starts ChatGPT as its own "responsible process", so macOS privacy permissions (microphone, screen recording and so on) and your existing grants apply to ChatGPT rather than to the launcher.

**Why not just edit the app?** 26.1007's Electron reads the `app.asar` integrity hash through the code signature, so any edit to the bundle crashes the app at launch. Re-signing it would drop OpenAI's restricted entitlements. Serving patched files at runtime avoids both problems.

## macOS limits

- **The shortcut only works while ChatGPT is frontmost.** 26.1007 removed the standalone Quick Chat window from the main process, so it can't be a global shortcut.
- **You must launch ChatGPT with "ChatGPT More Details".** Opening ChatGPT any other way (its own icon, Spotlight, a login item) starts it unpatched. The same goes for ChatGPT restarting itself, for example to install an update: quit it and reopen it with the launcher.
- **Updates are handled automatically, or fall back to unpatched.** After a ChatGPT update, the next launch re-patches the new version. If OpenAI has changed the code enough that the patch no longer matches, you get a notification and ChatGPT opens normally, without the patch.
- **Tested on ChatGPT 26.1007.30912 (20156) and 26.1013.40934 (20770)** on macOS.

## macOS troubleshooting

Check `~/Library/Application Support/codex-more-details/launcher.log`. It records the minified names the patch resolved, any patch error, and each time a patched file was served.

## Files

| File | Purpose |
| --- | --- |
| `more_details_patch.py` | Generates the patched bundles from your `app.asar` (`--emit DIR`) |
| `inject.mjs` | Launches ChatGPT over a DevTools pipe and serves the patched bundles |
| `spawn_disclaim.c` | Helper that makes ChatGPT the responsible process for privacy permissions |
| `launch.sh.in` | Template for the launcher script; `install.sh` fills in the paths |
| `install.sh`, `uninstall.sh` | Set up and remove the launcher and support folder |
| `install-windows.ps1`, `uninstall-windows.ps1` | Set up and remove the Windows launcher and shortcuts |
| `launch-windows.ps1` | Validate the Windows patch cache and launch with Store package identity |
| `WINDOWS-README.txt` | Windows usage and removal reference |
