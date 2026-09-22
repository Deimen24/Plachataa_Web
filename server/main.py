"""
HTTP API and static file server.

Run with:  python -m server.main [--listen] [--port 7870]
"""

import argparse
import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import settings
from .audio import ffmpeg_path, probe_duration, ensure_ffmpeg_on_path
from .engine import engine, MODELS, MODEL_INFO, EngineError
from .jobs import JobStore
from .library import make_libraries

log = logging.getLogger("plachataa")

settings.apply_environment()
settings.ensure_dirs()
ensure_ffmpeg_on_path()

app = FastAPI(title="Plachataa Web", docs_url="/api/docs",
	      redoc_url=None)
uploads, voices = make_libraries()
jobs = JobStore()

FILE_DIRS = {
	"uploads": settings.UPLOAD_DIR,
	"outputs": settings.OUTPUT_DIR,
	"voices": settings.VOICE_DIR,
}

EXAMPLE_DIRS = {
	"source": settings.SEEDVC_DIR / "examples" / "source",
	"reference": settings.SEEDVC_DIR / "examples" / "reference",
}


# ---- models ------------------------------------------------------------

class ConvertRequest(BaseModel):
	model: str = Field(default="v1")
	source_id: str
	reference_id: str | None = None
	voice_id: str | None = None
	params: dict = Field(default_factory=dict)


class VoiceFromUpload(BaseModel):
	upload_id: str
	name: str


class Rename(BaseModel):
	name: str


# ---- helpers -----------------------------------------------------------

def _safe_name(name):
	name = os.path.basename(name or "audio")
	return name[:120] or "audio"


def _check_ext(filename):
	ext = Path(filename).suffix.lower()
	if ext not in settings.ALLOWED_AUDIO_EXT:
		raise HTTPException(400, "unsupported audio type '%s'" % ext)
	return ext


async def _read_upload(file):
	data = await file.read()
	if not data:
		raise HTTPException(400, "empty upload")
	if len(data) > settings.MAX_UPLOAD_BYTES:
		raise HTTPException(413, "file larger than %d MB" %
				    (settings.MAX_UPLOAD_BYTES >> 20))
	return data


def _resolve_audio(lib, item_id, what):
	item = lib.get(item_id)
	path = lib.path(item_id)
	if item is None or path is None or not path.is_file():
		raise HTTPException(404, "%s audio not found" % what)
	return {"path": path, "name": item["name"],
		"duration": item.get("duration"), "url": item["url"]}


def _resolve_example(item_id, what):
	"""Example ids look like 'example:source:yae_0.wav'."""
	parts = item_id.split(":", 2)
	if len(parts) != 3 or parts[1] not in EXAMPLE_DIRS:
		return None
	path = EXAMPLE_DIRS[parts[1]] / os.path.basename(parts[2])
	if not path.is_file():
		raise HTTPException(404, "%s example not found" % what)
	return {"path": path, "name": path.name,
		"duration": probe_duration(path),
		"url": "/examples/%s/%s" % (parts[1], path.name)}


# ---- status ------------------------------------------------------------

@app.get("/api/status")
def api_status():
	info = engine.status()
	info["queue"] = jobs.queue_length()
	info["current_job"] = jobs.current
	info["ffmpeg"] = ffmpeg_path() is not None
	info["seedvc_dir"] = str(settings.SEEDVC_DIR)
	info["data_dir"] = str(settings.DATA_DIR)
	return info


@app.get("/api/models")
def api_models():
	return [{"id": m, **MODEL_INFO[m]} for m in MODELS]


@app.post("/api/models/{family}/load")
def api_model_load(family: str):
	if family not in ("v1", "v2"):
		raise HTTPException(404, "unknown model family")
	engine.load_in_background(family)
	return {"ok": True}


@app.post("/api/models/{family}/unload")
def api_model_unload(family: str):
	if family not in ("v1", "v2"):
		raise HTTPException(404, "unknown model family")
	if jobs.current is not None:
		raise HTTPException(409, "a job is running")
	engine.unload(family)
	return {"ok": True}


# ---- uploads -----------------------------------------------------------

@app.post("/api/uploads")
async def api_upload(file: UploadFile = File(...),
		     name: str = Form(default="")):
	ext = _check_ext(file.filename or "")
	data = await _read_upload(file)
	label = _safe_name(name or file.filename)
	return uploads.add_bytes(label, ext, data)


@app.get("/api/uploads")
def api_uploads():
	return uploads.list()


@app.delete("/api/uploads/{item_id}")
def api_upload_delete(item_id: str):
	if not uploads.delete(item_id):
		raise HTTPException(404, "not found")
	return {"ok": True}


# ---- voices ------------------------------------------------------------

@app.get("/api/voices")
def api_voices():
	return voices.list()


@app.post("/api/voices")
async def api_voice_add(file: UploadFile = File(...),
			name: str = Form(default="")):
	ext = _check_ext(file.filename or "")
	data = await _read_upload(file)
	label = _safe_name(name or Path(file.filename).stem)
	return voices.add_bytes(label, ext, data)


@app.post("/api/voices/from-upload")
def api_voice_from_upload(req: VoiceFromUpload):
	path = uploads.path(req.upload_id)
	if path is None or not path.is_file():
		raise HTTPException(404, "upload not found")
	return voices.add_copy(_safe_name(req.name), path)


@app.patch("/api/voices/{item_id}")
def api_voice_rename(item_id: str, req: Rename):
	item = voices.rename(item_id, _safe_name(req.name))
	if item is None:
		raise HTTPException(404, "not found")
	return item


@app.delete("/api/voices/{item_id}")
def api_voice_delete(item_id: str):
	if not voices.delete(item_id):
		raise HTTPException(404, "not found")
	return {"ok": True}


# ---- examples ----------------------------------------------------------

@app.get("/api/examples")
def api_examples():
	out = {}
	for kind, d in EXAMPLE_DIRS.items():
		items = []
		if d.is_dir():
			for p in sorted(d.iterdir()):
				if p.suffix.lower() in settings.ALLOWED_AUDIO_EXT:
					items.append({
						"id": "example:%s:%s" % (kind, p.name),
						"name": p.name,
						"url": "/examples/%s/%s" % (kind, p.name),
					})
		out[kind] = items
	return out


@app.get("/examples/{kind}/{name}")
def serve_example(kind: str, name: str):
	if kind not in EXAMPLE_DIRS:
		raise HTTPException(404)
	path = EXAMPLE_DIRS[kind] / os.path.basename(name)
	if not path.is_file():
		raise HTTPException(404)
	return FileResponse(path)


# ---- conversion jobs ---------------------------------------------------

@app.post("/api/convert")
def api_convert(req: ConvertRequest):
	if req.model not in MODELS:
		raise HTTPException(400, "unknown model")
	if not settings.seedvc_present():
		raise HTTPException(503, "seed-vc is not installed; run the "
				    "installer")
	source = _resolve_example(req.source_id, "source") or \
		_resolve_audio(uploads, req.source_id, "source")
	if req.voice_id:
		reference = _resolve_audio(voices, req.voice_id, "voice")
	elif req.reference_id:
		reference = _resolve_example(req.reference_id, "reference") \
			or _resolve_audio(uploads, req.reference_id,
					  "reference")
	else:
		raise HTTPException(400, "reference_id or voice_id required")
	return jobs.submit(req.model, source, reference, req.params)


@app.get("/api/jobs")
def api_jobs():
	return jobs.list()


@app.post("/api/jobs/clear")
def api_jobs_clear():
	return {"removed": jobs.clear_finished()}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
	job = jobs.get(job_id)
	if job is None:
		raise HTTPException(404, "not found")
	return job


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
	if not jobs.cancel(job_id):
		raise HTTPException(409, "job is not active")
	return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def api_job_delete(job_id: str):
	if not jobs.delete(job_id):
		raise HTTPException(409, "job not found or still running")
	return {"ok": True}


@app.post("/api/jobs/{job_id}/use-as-source")
def api_job_as_source(job_id: str):
	"""Register a finished output as an upload so it can be chained."""
	job = jobs.get(job_id)
	if job is None or not job.get("output"):
		raise HTTPException(404, "no output for this job")
	path = settings.OUTPUT_DIR / job["output"]["file"]
	label = "converted-%s.wav" % job_id
	return uploads.add_copy(label, path)


# ---- files & UI --------------------------------------------------------

@app.get("/files/{kind}/{name}")
def serve_file(kind: str, name: str):
	d = FILE_DIRS.get(kind)
	if d is None:
		raise HTTPException(404)
	path = d / os.path.basename(name)
	if not path.is_file():
		raise HTTPException(404)
	return FileResponse(path)


@app.get("/")
def index():
	return FileResponse(settings.WEB_DIR / "index.html",
			    headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=str(settings.WEB_DIR)),
	  name="static")


@app.exception_handler(EngineError)
def engine_error(_request, exc):
	return JSONResponse(status_code=500, content={"detail": str(exc)})


# ---- entry point -------------------------------------------------------

def parse_args():
	p = argparse.ArgumentParser(description="Plachataa Web server")
	p.add_argument("--host", default=settings.HOST)
	p.add_argument("--port", type=int, default=settings.PORT)
	p.add_argument("--listen", action="store_true",
		       help="bind 0.0.0.0 so other devices on the LAN can "
			    "connect")
	p.add_argument("--preload", default="",
		       help="comma separated model families to load at "
			    "start, e.g. v1 or v1,v2")
	p.add_argument("--reload", action="store_true",
		       help="developer auto-reload")
	p.add_argument("--open", action="store_true",
		       help="open the UI in the default browser once up")
	return p.parse_args()


def open_browser_later(url, delay=2.5):
	import threading
	import webbrowser

	def go():
		time.sleep(delay)
		try:
			webbrowser.open(url)
		except Exception:
			pass
	threading.Thread(target=go, daemon=True).start()


def main():
	import uvicorn
	args = parse_args()
	logging.basicConfig(level=logging.INFO,
			    format="%(asctime)s %(name)s: %(message)s")
	host = "0.0.0.0" if args.listen else args.host
	for family in [f for f in args.preload.split(",") if f]:
		engine.load_in_background(f.strip())
	log.info("seed-vc dir: %s (present=%s)", settings.SEEDVC_DIR,
		 settings.seedvc_present())
	url = "http://%s:%d/" % ("localhost" if host == "0.0.0.0" else host,
				 args.port)
	log.info("open %s in your browser", url)
	if args.open:
		open_browser_later(url)
	target = "server.main:app" if args.reload else app
	uvicorn.run(target, host=host, port=args.port, reload=args.reload,
		    log_level="info")


if __name__ == "__main__":
	main()
