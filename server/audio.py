"""
Small audio helpers that do not need the models loaded.
"""

import shutil
import subprocess
import json

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
