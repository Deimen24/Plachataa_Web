# Architecture

Plachataa Web is a thin, local-only web layer over the unmodified
[Seed-VC](https://github.com/Plachtaa/seed-vc) inference code. Nothing
in `vendor/seed-vc` is patched; the wrapper adapts to it.

```
 browser ──HTTP/JSON──▶ FastAPI (server/main.py)
                            │
        uploads/voices ◀────┤ library.py  (data/uploads, data/voices)
                            │
                        jobs.py  ── one worker thread ──▶ engine.py
                            │                               │
                       data/jobs.json                 vendor/seed-vc
                       data/outputs/*.wav          (SeedVCWrapper, v2
                                                    VoiceConversionWrapper)
```

## Components

### `server/settings.py`
Absolute paths and environment. Everything defaults to the repository
root (`vendor/`, `data/`). `apply_environment()` sets `HF_HOME` to
`data/hf_cache` so HuggingFace downloads stay inside the install. It
must run before torch/transformers are imported, so `main.py` calls it
at import time.

### `server/engine.py`
Owns the model instances.

- `bootstrap()` puts the seed-vc checkout on `sys.path` and makes it the
  working directory. Seed-VC uses relative paths (`./checkpoints`,
  `configs/v2/vc_wrapper.yaml`), so this is done once per process and
  all our own paths are absolute.
- Two model *families* map to three UI models:
  - `v1` → `SeedVCWrapper`, which loads both the 22 kHz speech DiT and
    the 44.1 kHz F0-conditioned DiT plus Whisper-small, CAMPPlus, two
    BigVGAN vocoders and RMVPE. UI models `v1` and `v1_f0` select
    between them with the `f0_condition` flag.
  - `v2` → `modules.v2.vc_wrapper.VoiceConversionWrapper`, instantiated
    from the upstream Hydra config exactly like `app.py` does.
- Loading is lazy and guarded by one lock; `load_in_background()` lets
  the UI trigger it without blocking a request. `unload()` drops the
  references and empties the CUDA cache so VRAM goes back to games.
- `convert()` runs the upstream streaming generators with
  `stream_output=True`. Each yielded chunk bumps the job's progress and
  is appended as MP3 to `outputs/<job>.stream.mp3` for a live preview.
  The last yield carries the full float32 waveform, which we write as
  16-bit WAV. A cancel flag is checked between chunks; cancelling
  closes the generator, so the GPU stops within one window.
- Progress is estimated from durations: Seed-VC processes the source in
  windows of 30 s minus the (≤25 s) reference length.

### `server/jobs.py`
`JobStore` is an in-memory dict plus a `queue.Queue` consumed by one
daemon thread. Only one conversion runs at a time. State transitions:
`queued → running → done | error | cancelled`. The store is mirrored
to `data/jobs.json` after every change; on startup any job left in an
active state is marked as an error ("server restarted"). Output files
are deleted with the job. History is capped (`MAX_HISTORY`).

### `server/library.py`
`Library` is a small persistent list of audio files used twice: for
uploads and for the saved voice library. Uploads that libsndfile
cannot decode (mp3, m4a, webm from the browser recorder…) are
transcoded to mono 16-bit WAV with ffmpeg on arrival, so the models
only ever see WAV/FLAC/OGG and never depend on ffmpeg themselves.

### `server/audio.py`
ffmpeg discovery (system ffmpeg first, else the binary bundled in the
`imageio-ffmpeg` wheel, which is also exposed on `PATH` under the name
`ffmpeg` for pydub and librosa), duration probing, WAV writing and
atomic JSON persistence.

### `server/main.py`
FastAPI routes (see README for the table), static serving of `web/`,
and the CLI entry point (`python -m server.main`). `--open` launches
the browser after startup; the run scripts pass it by default.

### `web/`
No build step: `index.html`, `style.css`, `app.js`. State is a single
object; `render_*` functions repaint from it. The page polls
`/api/status` every 5 s and `/api/jobs` every 1.5 s while a job is
active. Parameters are persisted per model in `localStorage`.

## Installer design

- `seedvc.lock` pins the upstream commit. Installers and updaters
  `git fetch` + `checkout` that commit, so upstream changes never reach
  a user until this project has been tested against them.
- PyTorch is installed separately from the pip requirements because the
  wheel index depends on the machine: `cu121` for CUDA, `cpu`
  otherwise. Pins: torch 2.4.0 / torchvision 0.19.0 / torchaudio 2.4.0,
  the versions upstream targets; the CUDA 12.1 build supports every
  RTX card (Turing and newer) and GTX 10/16 series.
- `requirements-seedvc.txt` is the subset of upstream's requirements
  that the two inference wrappers import. Gradio, funasr, modelscope,
  resemblyzer, jiwer, sounddevice and FreeSimpleGUI are left out.
- `tools/check_env.py` is the single source of truth for hardware
  decisions. It runs on bare Python, parses `nvidia-smi`, compares the
  driver version to the CUDA 12.1 minimum (Linux 525.60, Windows
  528.33) and returns distinct exit codes (10 no driver, 11 too old,
  12 torch cannot see CUDA). The shell and PowerShell installers branch
  on those codes; `--torch` re-verifies after installation.
- Driver update: on Ubuntu `ubuntu-drivers install`, Fedora
  `akmod-nvidia`, Arch `nvidia`; on Windows the NVIDIA App via winget
  with the download page as fallback. A reboot is always required, so
  the installer stops and asks to be re-run.
- `update.*` re-runs the installer only when something is wrong
  (environment check fails) or when a driver/torch change is
  requested; otherwise it only pulls, re-pins and upgrades pip
  packages.

## Data layout

```
data/
  uploads/      <id>.wav          transient sources/references
  uploads.json
  voices/       <id>.wav          saved reference voices
  voices.json
  outputs/      <job>.wav         results
                <job>.stream.mp3  streamed preview
  jobs.json
  hf_cache/                       transformers / BigVGAN / HuBERT weights
vendor/seed-vc/checkpoints/       DiT, CAMPPlus, RMVPE, v2 checkpoints
```

## Adding a model

1. Add an entry to `MODEL_INFO` in `server/engine.py` and a generator
   method that adapts the upstream call.
2. Add a parameter group `#params-<id>` in `web/index.html` and toggle
   it in `on_model_change()`; add preset values in `PRESETS`.
3. Document the parameters in README.

## Testing

- `tools/test_jobs.py` exercises the job queue with a stubbed engine
  (no models, no torch needed).
- `tools/smoke_engine.py` bootstraps the vendored seed-vc, imports every
  inference module and builds the v1 DiT and CAMPPlus from local
  configs. It proves `requirements-seedvc.txt` is complete for the
  pinned revision without downloading weights.
- `tools/check_env.py --torch` validates an installed environment.

There is no automated GPU test; conversions are verified by running the
UI.
