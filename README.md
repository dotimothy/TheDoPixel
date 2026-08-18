# TheDoPixel

TheDoPixel (Pixel Relay) is a self-hosted media appliance for safely moving photos,
RAW images, and videos from server folders to a Google Pixel. It tracks each file
through transfer, Android MediaStore discovery, Google Photos verification, and
optional cleanup without deleting the authoritative source originals.

## Windows quick install

Requirements: Windows 10/11 or Windows Server 2016+, an internet connection, and
WinGet (included with current Windows releases).

Open PowerShell in the cloned project folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
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

```powershell
.\install-windows.ps1 -SkipAdb -NoStart
```

Runtime state defaults to `%LOCALAPPDATA%\PixelRelay`. Browser uploads default to
the repository's `data` folder unless `PIXEL_RELAY_IMPORT_ROOT` is configured.

## macOS and Linux

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), Node.js LTS,
and Android Platform Tools, then run:

```bash
chmod +x pixel-relay
./pixel-relay
```

The launcher installs locked Python dependencies, builds the dashboard when needed,
starts the API and worker, and opens `http://127.0.0.1:8741`.

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

On macOS/Linux, replace `.\pixel-relay.cmd` with `./pixel-relay`:

```powershell
.\pixel-relay.cmd doctor
.\pixel-relay.cmd device status
.\pixel-relay.cmd source list
.\pixel-relay.cmd backup create
.\pixel-relay.cmd clean logs
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
