"""
Process-wide paths and configuration.

Everything lives under the repository root so that a gaming PC can keep
the whole install (code, venv, models, outputs) on one drive.  All paths
are absolute because the engine changes the working directory to the
seed-vc checkout before importing it.

Environment variables override the defaults:

  PLACHATAA_SEEDVC_DIR   path to the seed-vc checkout
  PLACHATAA_DATA_DIR     uploads, outputs, voices, job history
  PLACHATAA_HOST         bind address (default 127.0.0.1)
  PLACHATAA_PORT         bind port (default 7870)
  PLACHATAA_DEVICE       cuda | cpu | mps (default: auto)
  PLACHATAA_MAX_UPLOAD_MB   upload size limit (default 200)
  PLACHATAA_ROOT_PATH    URL prefix when served under a sub-path by a
                         reverse proxy, e.g. /voice (default: none)
  PLACHATAA_FORWARDED_ALLOW_IPS  proxies whose X-Forwarded-* headers are
                         trusted (default 127.0.0.1; "*" for any)
  PLACHATAA_BASIC_AUTH   user:password to require HTTP basic auth
                         (default: none; let the proxy authenticate)
  PLACHATAA_LOG_FILE     also write logs to this file (rotated)
  HF_HOME                HuggingFace cache; defaults to DATA_DIR/hf_cache

A .env file in the repository root is loaded first, so the service unit
and the run scripts see the same configuration.
"""

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

try:
	from dotenv import load_dotenv
	load_dotenv(ROOT_DIR / ".env", override=False)
except ImportError:
	pass

SEEDVC_DIR = Path(os.environ.get("PLACHATAA_SEEDVC_DIR",
			      ROOT_DIR / "vendor" / "seed-vc")).resolve()
DATA_DIR = Path(os.environ.get("PLACHATAA_DATA_DIR",
			    ROOT_DIR / "data")).resolve()
WEB_DIR = ROOT_DIR / "web"

UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
VOICE_DIR = DATA_DIR / "voices"
JOBS_FILE = DATA_DIR / "jobs.json"
VOICES_FILE = DATA_DIR / "voices.json"

HOST = os.environ.get("PLACHATAA_HOST", "127.0.0.1")
PORT = int(os.environ.get("PLACHATAA_PORT", "7870"))
DEVICE = os.environ.get("PLACHATAA_DEVICE", "auto")
MAX_UPLOAD_BYTES = int(os.environ.get("PLACHATAA_MAX_UPLOAD_MB", "200")) \
	* 1024 * 1024
ROOT_PATH = os.environ.get("PLACHATAA_ROOT_PATH", "").rstrip("/")
FORWARDED_ALLOW_IPS = os.environ.get("PLACHATAA_FORWARDED_ALLOW_IPS",
				     "127.0.0.1")
BASIC_AUTH = os.environ.get("PLACHATAA_BASIC_AUTH", "")
LOG_FILE = os.environ.get("PLACHATAA_LOG_FILE", "")
LOG_DIR = DATA_DIR / "logs"

# Reference audio longer than this is clipped by seed-vc itself.
MAX_REFERENCE_SECONDS = 25
# Jobs kept in history before the oldest are pruned.
MAX_HISTORY = 200

ALLOWED_AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac",
		     ".opus", ".webm", ".aiff", ".aif", ".wma"}


def apply_environment():
	"""
	Set environment variables that must be in place before torch,
	transformers or huggingface_hub are imported.
	"""
	os.environ.setdefault("HF_HOME", str(DATA_DIR / "hf_cache"))
	os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
	os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
	os.environ.setdefault("PYTHONUNBUFFERED", "1")


def ensure_dirs():
	apply_environment()
	for d in (UPLOAD_DIR, OUTPUT_DIR, VOICE_DIR, Path(os.environ["HF_HOME"])):
		d.mkdir(parents=True, exist_ok=True)


def seedvc_present():
	return (SEEDVC_DIR / "seed_vc_wrapper.py").is_file()
