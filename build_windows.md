# Building SavaariBot.exe on Windows

You only need this on a real Windows machine — PyInstaller produces native
binaries, so building on Linux yields a Linux executable, not a `.exe`.

## Option 1: Local Windows machine

```powershell
# in PowerShell, in the savaari_bot/ folder
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pyinstaller
pyinstaller SavaariBot.spec --noconfirm
```

Output: `dist\SavaariBot.exe`. Double-click it. The dashboard auto-opens
in your default browser the first time so you can paste your `vendorToken`.

## Option 2: GitHub Actions (no Windows machine needed)

Create `.github/workflows/build-windows.yml` in your repo:

```yaml
name: build-windows
on:
  push:
    tags: ["v*"]
  workflow_dispatch:
jobs:
  build:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r savaari_bot/requirements.txt pyinstaller
      - run: pyinstaller savaari_bot/SavaariBot.spec --noconfirm
        working-directory: savaari_bot
      - uses: actions/upload-artifact@v4
        with:
          name: SavaariBot-windows
          path: savaari_bot/dist/SavaariBot.exe
```

Push a tag (`git tag v0.1.0 && git push --tags`) or trigger the workflow
manually. Download the artifact from the run page when it's finished.

## Where things end up at runtime

The `.exe` is portable — it does not write next to itself. All runtime
state lives in the user's AppData:

```
%APPDATA%\SavaariBot\
  config.toml         user-editable settings (also written from the dashboard)
  savaari.sqlite3     broadcast history
  savaari.log         rotating log (2 MB × 3)
```

Move or delete the folder to fully reset the bot.

## First-launch experience

1. Double-click `SavaariBot.exe`.
2. A blue "S" icon appears in the system tray.
3. Default browser opens to <http://127.0.0.1:8765/> showing the
   first-run wizard.
4. Paste your `vendorToken` (from `sessionStorage.vendorToken` on
   `vendor.savaari.com`), optionally Telegram creds, click **Save and start**.
5. The poller starts within a second. Subsequent launches skip the wizard
   and go straight to the live dashboard.

## Antivirus

PyInstaller-packed `.exe`s sometimes trip Windows Defender's machine-learning
heuristics on first run. If that happens:

- Click **More info** → **Run anyway** in the SmartScreen dialog, **or**
- Add the file to Defender exclusions, **or**
- Sign the binary with a code-signing cert (~$80/year) for a permanent fix.

For a personal-use tool the exclusion is fine.

## Auto-start with Windows

The dashboard's **Settings** panel will get a "Run at login" toggle in
Phase 1. For now, drag a shortcut to `SavaariBot.exe` into:

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

…and it will launch on every login.
