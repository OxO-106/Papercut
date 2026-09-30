# Papercut

A local reader for academic papers. Papercut reflows a PDF into one customizable column and uses a local model (Qwen, through [Ollama](https://ollama.com)) to read the paper with you. Nothing leaves your machine except optional web searches.

See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

## Features

- **Reflowed reading view:** a single column with adjustable font, size, line height, width and theme. The reader has:
  - numbered references;
  - an outline sidebar;
  - a Figures & tables tab;
  - folded References and Appendix sections;
  - a remembered reading position.
- **AI highlights:** the model reads the whole paper, takes notes, and highlights the sentences that matter. Colours show each sentence's role: objective, novelty, method, result, limitation or definition. Hover a highlight to see its margin note.
- **Explain and Translate:** hover any sentence, or select any text, to get an explanation or a translation. Figures and tables have their own Explain button; the model reads the image.
- **Ask:** chat about the paper, with answers grounded in its passages. It can optionally search arXiv/OpenAlex for related work and the web for background concepts.
- **Questions:** Papercut generates insightful questions about the key highlights and tries to answer them from the paper or from outside sources.
- **Library:** upload PDFs or arXiv links. Processing is queued, and the queue survives restarts.
- **Installable app (PWA)** with a Windows tray icon that shows whether the server is running and restarts it if it stops.

## Requirements

- Python 3.12
- [Ollama](https://ollama.com) with these models:
  ```
  ollama pull qwen3.5:9b-q8_0
  ollama pull qwen3-embedding:0.6b
  ```
  The chat model needs about 10 GB of VRAM at a 32k context. To use another Qwen variant, set it in `<library>/settings.json`: `{"ai": {"model": "qwen3.5:4b"}}`.
- An NVIDIA GPU is recommended for Docling's PDF layout models; it also runs on CPU, but more slowly.
- Windows for the launcher, tray and Edge app scripts. The server itself is plain FastAPI.

## Setup

```
git clone https://github.com/OxO-106/Papercut.git paper-reader
cd paper-reader
py -3.12 -m venv .venv
.venv\Scripts\pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
.venv\Scripts\pip install -r requirements.txt
```

Pick the PyTorch build that matches your hardware at https://pytorch.org/get-started/locally/. Docling downloads its layout models on first run.

### Optional: web search

Ask and Questions can search the web through Ollama's web search API. Create an API key at https://ollama.com/settings/keys, then either:
- set the `OLLAMA_API_KEY` environment variable, or
- put the key in `ollama-api-key.txt` next to the project folder, not inside it.

Paper search (arXiv, OpenAlex) needs no key.

## Run

**Windows:**

```
powershell -ExecutionPolicy Bypass -File start-papercut.ps1
```

This does four things:
1. Starts Ollama and the server (hidden, logging to `papercut.log`).
2. Starts Tailscale Serve if Tailscale is installed.
3. Starts the tray icon.
4. Opens Papercut in an Edge app window.

`stop-papercut.bat` stops the server.

For a Start-menu entry, create a shortcut to `launch-papercut.vbs`, which runs the same script without a console window. In Edge, *Apps → Install Papercut* installs it as an app with its own window and taskbar icon.

**Any OS:**

```
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open http://127.0.0.1:8000.

### Tray icon

- **Colours:** coloured while the server is running, grey when it has stopped.
- **Automatic restart:** if the server stops unexpectedly, the icon restarts it (at most 3 times an hour) and shows a notification.
- **Controls:** double-click to open Papercut. Right-click to open it, copy the Tailscale link, start or stop the server, or hide the icon.
- **Logs:** events go to `papercut-events.log`. Each start keeps the previous server log as `papercut.prev.log`.

### From another device

The server only listens on 127.0.0.1. To read on another device, such as a laptop, use [Tailscale](https://tailscale.com), a private network between your own devices:
1. The start script runs `tailscale serve`. This gives Papercut a private HTTPS address, `https://<pc>.<tailnet>.ts.net`, that only devices on your tailnet can reach.
2. On the other device, sign in to Tailscale with the same account and open that address.

`tailscale serve reset` stops sharing. The PC must stay on and awake.

## Library

Processed papers live in `../library`, next to the project folder. There is one folder per paper, holding `original.pdf`, `paper.json` and `assets/`.

To move the library, set `PAPER_READER_LIBRARY`, for example to a cloud-synced folder.

## Checking parse quality

```
.venv\Scripts\python scripts\process_corpus.py path\to\pdfs
```

This re-parses every PDF in the folder and prints per-paper stats. `lowcov` counts sentences that could not be located on the PDF page; these would be missing from exported highlights. Add `?debug` to the reader URL to underline them.

## Project layout

```
app/       FastAPI server: parsing (Docling + PyMuPDF), highlighting, explain,
           translate, ask, questions, research tools, job queue
web/       Front end (vanilla JS/CSS), fonts, icons, PWA manifest
scripts/   Maintenance scripts
docs/      Design document
*.ps1      Windows launcher, tray icon and helpers
```

## License

The code is released under the [MIT License](LICENSE). The fonts bundled in `web/fonts` are under the SIL Open Font License; see [web/fonts/README.md](web/fonts/README.md).
