"""
Persistent lists of audio files: uploaded sources/references and the
saved voice library.  Both are the same shape, so one class serves both.
"""

import shutil
import threading
import time
import uuid
from pathlib import Path

from . import settings
from .audio import probe_duration, dump_json, load_json, transcode_to_wav, \
	NATIVE_EXT


class Library:
	def __init__(self, directory, index_file, url_prefix):
		self.dir = Path(directory)
		self.index_file = Path(index_file)
		self.url_prefix = url_prefix
		self.lock = threading.Lock()
		self.items = {}
		self._load()

	def _load(self):
		raw = load_json(self.index_file, {})
		for item_id, item in raw.items():
			if (self.dir / item["file"]).is_file():
				self.items[item_id] = item

	def _save(self):
		dump_json(self.index_file, self.items)

	def _public(self, item):
		out = dict(item)
		out["url"] = "%s/%s" % (self.url_prefix, item["file"])
		return out

	def list(self):
		with self.lock:
			items = sorted(self.items.values(),
				       key=lambda i: i["created"], reverse=True)
			return [self._public(i) for i in items]

	def get(self, item_id):
		with self.lock:
			item = self.items.get(item_id)
			return self._public(item) if item else None

	def path(self, item_id):
		with self.lock:
			item = self.items.get(item_id)
		if item is None:
			return None
		return self.dir / item["file"]

	def add_bytes(self, name, ext, data):
		item_id = uuid.uuid4().hex
		ext = ext.lower()
		raw = self.dir / (item_id + ext)
		with open(raw, "wb") as fh:
			fh.write(data)
		if ext in NATIVE_EXT:
			return self._register(item_id, name, raw.name)
		wav = self.dir / (item_id + ".wav")
		try:
			transcode_to_wav(raw, wav)
		finally:
			raw.unlink(missing_ok=True)
		return self._register(item_id, name, wav.name)

	def add_copy(self, name, src_path):
		src_path = Path(src_path)
		item_id = uuid.uuid4().hex
		fname = item_id + src_path.suffix.lower()
		shutil.copyfile(src_path, self.dir / fname)
		return self._register(item_id, name, fname)

	def _register(self, item_id, name, fname):
		item = {
			"id": item_id,
			"name": name,
			"file": fname,
			"duration": probe_duration(self.dir / fname),
			"created": time.time(),
		}
		with self.lock:
			self.items[item_id] = item
			self._save()
		return self._public(item)

	def rename(self, item_id, name):
		with self.lock:
			item = self.items.get(item_id)
			if item is None:
				return None
			item["name"] = name
			self._save()
			return self._public(item)

	def delete(self, item_id):
		with self.lock:
			item = self.items.pop(item_id, None)
			if item is None:
				return False
			self._save()
		try:
			(self.dir / item["file"]).unlink()
		except OSError:
			pass
		return True


def make_libraries():
	settings.ensure_dirs()
	uploads = Library(settings.UPLOAD_DIR,
			  settings.DATA_DIR / "uploads.json", "/files/uploads")
	voices = Library(settings.VOICE_DIR, settings.VOICES_FILE,
			 "/files/voices")
	return uploads, voices
