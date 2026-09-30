# Papercut

A personal reader for academic papers. It reflows a PDF into one customizable column, colour-codes key sentences by their role (objective, novelty, method, result, limitation, definition), explains and translates sentences or any selected text, and answers questions about the paper. All AI runs locally through Ollama. See [docs/DESIGN.md](docs/DESIGN.md).

Requires [Ollama](https://ollama.com) with `qwen3.5:9b-q8_0` and `qwen3-embedding:0.6b` pulled.

## Run

**Start menu → Papercut.** It starts Ollama, the Papercut server and Tailscale Serve (whatever isn't already running), then opens Papercut: as the installed Edge app if Papercut is installed (Edge menu, Apps, Install Papercut; sharp title-bar and taskbar icons), otherwise in an Edge app window. No console; a message box appears only if something fails. `stop-papercut.bat` stops the server.

**Tray icon.** While Papercut is started, its icon sits in the taskbar's notification area (it may be under the ^ arrow): coloured when the server is running, grey when it has stopped. If the server stops without being asked to (not via `stop-papercut.bat` or the icon's Stop), the icon restarts it (at most 3 times an hour) and shows a notification. Stops, restarts and starts are written to `papercut-events.log`; each start keeps the previous server log as `papercut.prev.log`, which shows why a stopped server stopped. Double-click to open Papercut; right-click to open it, copy the laptop link, start or stop the server, or hide the icon. It's `papercut-tray.ps1`, launched by the start script; only one copy runs. Both open Papercut through `papercut-open.ps1`, which finds the installed app in Edge's profile data.

How it's wired: the shortcut (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Papercut.lnk`) runs `launch-papercut.vbs`, which runs `start-papercut.ps1 -App` without a window. Run `start-papercut.ps1` from a PowerShell console to see each step and both addresses. The server listens on this PC only (127.0.0.1:8000) and logs to `papercut.log`.

## Using Papercut from the laptop

All processing stays on this PC; the laptop opens Papercut in a browser over [Tailscale](https://tailscale.com), a private network between your own devices. The Start menu entry already turns on Tailscale Serve, which gives Papercut a private HTTPS address (`https://<pc>.<tailnet>.ts.net`) reachable only by devices signed in to your Tailscale account. `tailscale serve status` shows it; `tailscale serve reset` stops sharing it.

**On the laptop:** install Tailscale, sign in with the same account, open the address. In Edge, *Apps → Install Papercut* gives it its own window and taskbar icon.

**Requirements:** this PC must be on and awake, with Papercut started from the Start menu.

## Library

Processed papers live in `D:\Read\library` (one folder per paper: `original.pdf`, `paper.json`, `assets/`).
Set `PAPER_READER_LIBRARY` to move it, e.g. into a cloud-synced folder.

## Checking parse quality

```
.venv\Scripts\python scripts\process_corpus.py ..\Papers
```

Re-parses every PDF in the folder and prints per-paper stats. `lowcov` counts sentences that could not be located on the PDF page (these would be missing from exported highlights). Add `?debug` to the reader URL to underline them.

## Setup from scratch

```
py -3.12 -m venv .venv
.venv\Scripts\pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
.venv\Scripts\pip install docling pymupdf pysbd fastapi "uvicorn[standard]" python-multipart
```

Docling downloads its layout models on first run.
