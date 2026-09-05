# TheDoPixel

TheDoPixel (Pixel Relay) is a self-hosted media appliance for safely moving photos,
RAW images, and videos from server folders to a Google Pixel. It tracks each file
through transfer, Android MediaStore discovery, Google Photos verification, and
optional cleanup without deleting the authoritative source originals.

## Windows quick install

Requirements: Windows 10/11 or Windows Server 2016+, an internet connection, and
WinGet (included with current Windows releases).

Double-click `install-windows.bat`, or open Command Prompt in the cloned project
folder and run:

```bat
install-windows.bat
```

The installer adds missing prerequisites through WinGet (`uv`, Node.js LTS, and
Google Android Platform Tools), creates the locked Python environment, builds the
dashboard, and starts TheDoPixel. The first start prompts you to create the local
administrator.

After installation, start it by double-clicking `pixel-relay.cmd` or running:

```powershell
.\pixel-relay.cmd
```

Use `-SkipAdb` with the installer if the appliance will use FTP exclusively, or
`-NoStart` to install without starting the server:

```bat
install-windows.bat -SkipAdb -NoStart
```

The batch file forwards all options to the PowerShell installer, so the existing
`.ps1` entry point remains available for scripted deployments.

Runtime state defaults to `%LOCALAPPDATA%\PixelRelay`. Browser uploads default to
the repository's `data` folder unless `PIXEL_RELAY_IMPORT_ROOT` is configured.

## macOS and Linux

Run the cross-platform installer from the cloned project folder:

```bash
chmod +x install-unix.sh
./install-unix.sh
```

On macOS it uses Homebrew, installing Homebrew first when necessary. On Linux it
supports apt, dnf, yum, pacman, zypper, and apk. Missing `uv`, Node.js/npm, and
Android Platform Tools are installed before the locked Python environment and
dashboard are built.

Use `--skip-adb` for an FTP-only appliance or `--no-start` to install without
starting the service:

```bash
./install-unix.sh --skip-adb --no-start
```

After installation, start TheDoPixel with `./pixel-relay`. The first start prompts
you to create the local administrator and opens `http://127.0.0.1:8741`.

## First-time guide

The dashboard opens this guide automatically the first time you sign in. You can
open it again at any time with **Quick start** in the top bar.

1. Connect the Pixel, enable USB debugging, approve the computer, and unlock the
   phone with its PIN after each reboot. Keep any adopted storage drive connected.
2. Open **Settings**, choose USB, network ADB, or FTP, save, and confirm the Pixel's
   target storage is available on **Overview**. USB is simplest for a first run.
3. Open **Sources**, choose a server folder, and scan it. The service account needs
   read access. Photos, RAW images, and videos such as MP4 are supported.
4. Filter and select the files, give the batch a name, and create it. Keep the Pixel
   connected while Pixel Relay transfers, checksums, and registers the media.
5. When the batch is ready, confirm in Google Photos that backup finished. Mark the
   batch verified, then purge its Pixel copies when you want the space back. Source
   originals are never deleted by this purge.

If Overview reports that `/sdcard` is unavailable, unlock the Pixel fully after
startup, reconnect the selected drive if there is one, and refresh the device.

## Configuration

Copy `.env.example` to `.env` to override defaults. Important settings include:

- `PIXEL_RELAY_CONNECTION_MODE`: `usb`, `network`, or `ftp`
- `PIXEL_RELAY_DEVICE_SERIAL`: ADB serial such as `192.168.1.35:5555`
- `PIXEL_RELAY_ADB_PATH`: path to `adb` or `adb.exe`
- `PIXEL_RELAY_IMPORT_ROOT`: durable location for browser-uploaded originals
- `PIXEL_RELAY_DATA_DIR`: database, logs, sessions, and local appliance state
- `PIXEL_RELAY_PUSH_TIMEOUT_SECONDS`: timeout for large media transfers

The service binds to `0.0.0.0:8741` by default so other devices on the LAN can use
the dashboard. Keep it on a trusted network or place it behind an authenticated TLS
reverse proxy.

## Useful commands

```powershell
.\pixel-relay.cmd doctor
.\pixel-relay.cmd device status
.\pixel-relay.cmd source list
.\pixel-relay.cmd backup create
.\pixel-relay.cmd clean logs
```

On macOS and Linux, use the same commands through `./pixel-relay`:

```bash
./pixel-relay doctor
./pixel-relay device status
./pixel-relay source list
./pixel-relay backup create
./pixel-relay clean logs
```

## Development

```bash
uv sync --extra dev
uv run ruff check backend
uv run pytest
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
```

Source media is never included in application-state backups and is never removed by
source cleanup operations.
