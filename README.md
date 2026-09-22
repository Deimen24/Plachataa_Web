# Plachataa Web

A self-hosted web interface for [Seed-VC](https://github.com/Plachtaa/seed-vc),
the zero-shot voice and singing-voice conversion model, built for a
gaming PC with an NVIDIA RTX card. One installer script sets up
everything; one script starts it; one script updates it.

Drop in a recording, pick a reference voice, press Convert.

![UI](docs/ui.png)

## Features

- **Voice conversion (v1)**, **singing voice conversion (v1 + F0)** and
  **voice + accent conversion (v2)** from the same page.
- Drag and drop, file picker or **microphone recording** for source and
  reference audio; browser recordings are transcoded server-side.
- **Voice library**: save reference clips under a name and reuse them.
- **Job queue** with live progress, streamed preview while converting,
  cancel, download, and "use as source" to chain conversions.
- Presets (fast / default / quality) and every Seed-VC parameter
  exposed with sensible defaults per model.
- Models load on first use and can be **unloaded to free VRAM** for
  games without stopping the server.
- **NVIDIA driver check**: the installer refuses to pick CUDA on a
  driver too old for the bundled CUDA 12.1 runtime and can update it.
- Everything (code, venv, models, outputs) lives inside this folder.

## Requirements

| | Minimum | Notes |
|---|---|---|
| GPU | NVIDIA RTX 20-series or newer, 6 GB VRAM | 8 GB+ recommended for v2; GTX 10/16-series also work |
| Driver | 528.33 (Windows) / 525.60 (Linux) | needed by CUDA 12.1; the installer checks and can update it |
| OS | Windows 10/11, Ubuntu 22.04+, Fedora, Arch | macOS untested |
| Python | 3.10 or 3.11 | installed by the installer if missing |
| Disk | ~12 GB | ~2.5 GB Python packages + ~6 GB models + outputs |
| Network | first run only | models are downloaded from HuggingFace |

CPU-only works but a 10 s clip takes minutes instead of seconds.

## Install

### Windows

1. Clone or download this repository (Code → Download ZIP → extract).
2. Double-click **`install.bat`**.

The installer uses `winget` to add Python 3.10 and Git if they are
missing, checks the NVIDIA driver, downloads Seed-VC into `vendor/`,
creates `.venv` and installs PyTorch with CUDA 12.1.

Optional switches (run from a terminal in this folder):

```
install.bat -DownloadModels    pre-fetch all models now (~6 GB)
install.bat -UpdateDriver      install/upgrade the NVIDIA driver if needed
install.bat -Cpu               force the CPU build
install.bat -Yes               never prompt
```

### Linux

```bash
git clone https://github.com/Deimen24/Plachataa_Web.git
cd Plachataa_Web
./install.sh                 # add --download-models, --update-driver, --cpu, --yes
```

On Debian/Ubuntu the script installs `python3.10`, `git`, `ffmpeg` and
`libsndfile1` with `apt` (adding the deadsnakes PPA when needed). On
Fedora it uses `dnf`, on Arch `pacman`. Driver updates use
`ubuntu-drivers`, `akmod-nvidia` or the `nvidia` package respectively
and require a reboot afterwards.

## Run

```
run.bat            (Windows)
./run.sh           (Linux)
```

The server starts on <http://localhost:7870> and opens your browser.
The first conversion with each model family loads it (about 30 s to a
few minutes) and, on the very first run, downloads its checkpoints.

Options are passed straight through:

```
./run.sh --listen          # reachable from your phone / other PCs on the LAN
./run.sh --port 8000
./run.sh --preload v1      # load the v1 models at start
PLACHATAA_NO_BROWSER=1 ./run.sh
```

Stop with Ctrl+C.

## Update

```
update.bat         (Windows)   options: -UpdateDriver -Torch -DownloadModels
./update.sh        (Linux)     options: --update-driver --torch --download-models
```

This pulls the newest version of this repository, moves the vendored
Seed-VC to the revision pinned in `seedvc.lock`, upgrades the Python
dependencies and re-runs the driver / CUDA check. If the check fails it
falls back to the installer to repair the environment. `--update-driver`
installs a newer NVIDIA driver when the current one is missing or too
old for CUDA 12.1.

To check your machine at any time:

```
.venv/bin/python tools/check_env.py --torch        # Linux: GPU, driver, torch
.venv\Scripts\python tools\check_env.py --torch    # Windows
.venv/bin/python tools/smoke_engine.py             # imports seed-vc, builds a model, no download
```

## Using the web UI

1. **Source audio**: the recording whose voice you want to change.
   Any length; long files are processed in ~30 s windows.
2. **Reference voice**: a clean clip of the target speaker, 5 to 25 s
   (longer clips are clipped to 25 s). Click *save to library* to keep
   it.
3. **Model**
   - *Voice conversion (v1)*: speech, fastest, good default.
   - *Singing voice conversion (v1 + F0)*: pitch-conditioned 44.1 kHz
     model. Use it for singing; turn on *Auto pitch adjust* for speech
     with a big pitch difference, or shift semitones manually.
   - *Voice + accent conversion (v2)*: best at removing the source
     speaker's traits; can optionally convert style, emotion and
     accent too.
4. **Convert**. Jobs run one at a time; queue as many as you like.

Parameter notes: *Diffusion steps* trades quality for time (10 for
speech, 30 to 50 for singing, 50 to 100 for best quality). *Length
adjust* speeds up or slows down the result. The CFG rates have subtle
effects; the presets cover the useful range.

## Configuration

Copy `.env.example` to `.env` and uncomment what you need:

| Variable | Default | Purpose |
|---|---|---|
| `PLACHATAA_PORT` | `7870` | HTTP port |
| `PLACHATAA_HOST` | `127.0.0.1` | bind address (`run --listen` uses 0.0.0.0) |
| `PLACHATAA_DEVICE` | `auto` | `cuda`, `cpu` or `mps` |
| `PLACHATAA_DATA_DIR` | `./data` | uploads, outputs, voices, HuggingFace cache |
| `PLACHATAA_SEEDVC_DIR` | `./vendor/seed-vc` | upstream checkout |
| `PLACHATAA_MAX_UPLOAD_MB` | `200` | upload size limit |
| `HF_ENDPOINT` | unset | e.g. `https://hf-mirror.com` if huggingface.co is blocked |

## HTTP API

The UI talks to a small JSON API (interactive docs at `/api/docs`):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | device, VRAM, loaded models, queue |
| GET | `/api/models` | available models and hints |
| POST | `/api/models/{v1,v2}/load` · `/unload` | manage VRAM |
| POST | `/api/uploads` | multipart upload → `{id, url, duration}` |
| GET/POST/PATCH/DELETE | `/api/voices` | voice library |
| POST | `/api/convert` | `{model, source_id, reference_id|voice_id, params}` → job |
| GET | `/api/jobs` · `/api/jobs/{id}` | job list / state |
| POST | `/api/jobs/{id}/cancel` · `/use-as-source` | |
| GET | `/files/outputs/{name}.wav` | results |

## Troubleshooting

- **"seed-vc missing" in the header**: run the installer; it clones
  Seed-VC into `vendor/seed-vc`.
- **Driver too old / torch cannot see the GPU**: run
  `update.bat -UpdateDriver` (or `./update.sh --update-driver`), reboot,
  start again. `tools/check_env.py --torch` shows what the server sees.
- **Out of VRAM**: unload the family you are not using (header →
  *Models*), lower diffusion steps, or close the game.
- **Slow first conversion**: model download and load; later ones are
  fast. Pre-download with `install.bat -DownloadModels`.
- **HuggingFace unreachable**: set `HF_ENDPOINT=https://hf-mirror.com`
  in `.env`.
- **Port in use**: `run.bat --port 7871`.

## Project layout

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the pieces fit together.

```
install.sh / install.ps1 / install.bat   one-shot installer
update.sh  / update.ps1  / update.bat    updater (repo, seed-vc, deps, driver)
run.sh     / run.ps1     / run.bat       start the server
seedvc.lock                               pinned upstream revision
requirements-seedvc.txt                   inference deps of seed-vc (no torch)
requirements-web.txt                      FastAPI, uvicorn, bundled ffmpeg
server/                                   API, engine wrapper, job queue
web/                                      static single-page UI
tools/                                    check_env, download_models, tests
vendor/seed-vc                            upstream checkout (created by installer)
data/                                     uploads, outputs, voices, model cache
```

## License

GPL-3.0, the same as Seed-VC, whose code this project imports. Model
weights are downloaded from their respective HuggingFace repositories
under their own licenses.
