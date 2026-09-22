"""
Small audio helpers that do not need the models loaded.
"""

import os
import shutil
import subprocess
import json
from pathlib import Path

import numpy as np
import soundfile as sf


def ffmpeg_path():
	"""
	Return the ffmpeg binary to use.  A system ffmpeg wins; otherwise the
	one bundled with imageio-ffmpeg is used so Windows users need nothing.
	"""
	found = shutil.which("ffmpeg")
	if found:
		return found
	try:
		import imageio_ffmpeg
		return imageio_ffmpeg.get_ffmpeg_exe()
	except Exception:
		return None


def ensure_ffmpeg_on_path():
	"""
	librosa (via audioread) and pydub look for `ffmpeg` on PATH.  If only
	the imageio-ffmpeg binary exists, expose it under that name.
	"""
	if shutil.which("ffmpeg"):
		return True
	exe = ffmpeg_path()
	if exe is None:
		return False
	link_dir = Path(exe).parent / "plachataa-bin"
	link_dir.mkdir(exist_ok=True)
	suffix = ".exe" if os.name == "nt" else ""
	alias = link_dir / ("ffmpeg" + suffix)
	if not alias.exists():
		try:
			os.symlink(exe, alias)
		except OSError:
			shutil.copyfile(exe, alias)
			alias.chmod(0o755)
	os.environ["PATH"] = str(link_dir) + os.pathsep + os.environ["PATH"]
	return True


# Formats soundfile/libsndfile decode natively; everything else is
# transcoded to wav on upload so the models never depend on ffmpeg.
NATIVE_EXT = {".wav", ".flac", ".ogg", ".aiff", ".aif"}


def transcode_to_wav(src, dst, sr=None):
	exe = ffmpeg_path()
	if exe is None:
		raise RuntimeError("ffmpeg not available to decode %s" %
				   Path(src).suffix)
	cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i",
	       str(src), "-vn", "-ac", "1", "-acodec", "pcm_s16le"]
	if sr:
		cmd += ["-ar", str(sr)]
	cmd.append(str(dst))
	res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
	if res.returncode != 0:
		raise RuntimeError("ffmpeg failed: " + res.stderr.strip()[-400:])


def ffprobe_duration(path):
	"""
	Duration in seconds via ffmpeg (handles mp3/m4a/webm that soundfile
	cannot open).  Returns None on failure.
	"""
	exe = ffmpeg_path()
	if exe is None:
		return None
	cmd = [exe, "-hide_banner", "-i", str(path), "-f", "null", "-"]
	try:
		res = subprocess.run(cmd, capture_output=True, text=True,
				     timeout=60)
	except Exception:
		return None
	# ffmpeg prints "time=HH:MM:SS.xx" in the progress line on stderr.
	tail = res.stderr.rsplit("time=", 1)
	if len(tail) < 2:
		return None
	stamp = tail[1].split(" ", 1)[0]
	try:
		h, m, s = stamp.split(":")
		return int(h) * 3600 + int(m) * 60 + float(s)
	except ValueError:
		return None


def probe_duration(path):
	try:
		info = sf.info(str(path))
		return info.frames / float(info.samplerate)
	except Exception:
		return ffprobe_duration(path)


def write_wav(path, samples, sr):
	"""
	Write float samples as 16-bit PCM wav, clipped to [-1, 1].
	"""
	data = np.asarray(samples, dtype=np.float32)
	data = np.clip(data, -1.0, 1.0)
	sf.write(str(path), data, sr, subtype="PCM_16")


def dump_json(path, obj):
	tmp = path.with_suffix(path.suffix + ".tmp")
	with open(tmp, "w", encoding="utf-8") as fh:
		json.dump(obj, fh, indent=1)
	tmp.replace(path)


def load_json(path, default):
	try:
		with open(path, "r", encoding="utf-8") as fh:
			return json.load(fh)
	except (OSError, ValueError):
		return default
