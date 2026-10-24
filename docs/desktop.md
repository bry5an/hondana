# The desktop app

`hondana desktop` runs Hondana as a tray (menu bar) app: it keeps the server running, opens it in
your browser, and adds an icon with **Open Hondana**, **Settings**, **Open data folder** and
**Quit**. The packaged downloads for macOS and Windows start this command when you open them; from a
checkout you can run it yourself:

```bash
uv sync --extra desktop          # adds pystray, the tray icon
uv run hondana desktop           # or: uv run python -m epublib desktop
```

`--no-browser` skips opening the page, `--no-tray` runs without an icon (until Ctrl-C), and
`--library PATH` is where the first run puts its folders.

## Installing a download

Each [release](https://github.com/bry5an/hondana/releases) has a build for **macOS on Apple
silicon** (`Hondana-<version>-macos-arm64.dmg`, macOS 12 or later) and **Windows 10/11 on x64**
(`Hondana-<version>-windows-x64-setup.exe`, or `…-portable.zip` to unzip and run
`hondana.exe`), with a `SHA256SUMS` file. Intel Macs and Linux: run [from source](../README.md) or
with [Docker](docker.md).

The builds are **not signed** unless the release notes say so, so your system warns you the first
time:

- **macOS:** open the disk image and drag Hondana to Applications. If it won't open ("can't be
  opened because Apple cannot check it"), go to System Settings › Privacy & Security and press
  **Open Anyway**, or run
  `xattr -dr com.apple.quarantine /Applications/Hondana.app` once.
- **Windows:** run the installer (it installs for you alone, no administrator needed). On the blue
  SmartScreen page press **More info**, then **Run anyway**.

Hondana has no Dock icon on macOS: look for it in the menu bar. To remove it, delete the app (or
uninstall it from Windows' Installed apps); your data folder stays until you delete it.

## The first run

With no config file and no saved settings, the app:

1. makes `~/Documents/Hondana/Books` and `~/Documents/Hondana/Audiobooks` and saves them as your
   library folders (change them on the Settings page);
2. listens on `127.0.0.1` only, so nothing else on your network can reach it until you choose.
   To use Hondana from your phone, set **Listen address** to `0.0.0.0` on Settings, set an admin
   password there first, and restart (see [networking.md](networking.md));
3. uses port 8000, or the next free one, and keeps it;
4. opens `/settings?welcome=1`, which says where to put your books.

Your data (profiles, known words, indexes, covers, settings) is in the app-data folder:
`~/Library/Application Support/Hondana/data` on macOS, `%LOCALAPPDATA%\Hondana\data` on Windows.
**Open data folder** in the tray menu shows it. Back it up.

## How it runs

The tray process starts `hondana serve` as a child process and starts it again when it exits:
**Restart now** on the Settings page, or a crash (it gives up after four crashes in a minute and
says so). Each start re-reads your settings. If the tray app is force-quit, the server stops with
it.

Only one copy runs. Opening the app again just opens the browser at the one that is running.

Logs are in `<data folder>/logs/`: `desktop.log` (the tray and anything the server printed before
it could log) and `server.log`.

## Is this install working?

```bash
hondana --self-test
```

starts the server on a free port in a scratch folder, checks that its pages and files are served,
analyzes a Japanese and an English book, and exercises the tokenizers. It prints one line per check
and exits non-zero if any fails. The release builds run it before they are published.
