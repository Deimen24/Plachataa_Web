"""
Job queue: one background worker runs conversions strictly one at a
time (a single GPU cannot usefully do more).  Job state is kept in
memory and mirrored to data/jobs.json so history survives restarts.
"""

import logging
import queue
import threading
import time
import traceback
import uuid

from . import settings
from .audio import write_wav, dump_json, load_json
from .engine import engine, Cancelled, MODEL_INFO

log = logging.getLogger("plachataa.jobs")

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"
ACTIVE = (QUEUED, RUNNING)


class JobStore:
	def __init__(self):
		self.lock = threading.Lock()
		self.jobs = {}
		self.order = []
		self.queue = queue.Queue()
		self.cancel_flags = {}
		self.current = None
		self._load()
		self.worker = threading.Thread(target=self._run, daemon=True,
					       name="vc-worker")
		self.worker.start()

	# ---- persistence ----------------------------------------------

	def _load(self):
		raw = load_json(settings.JOBS_FILE, {"order": [], "jobs": {}})
		for job_id in raw.get("order", []):
			job = raw["jobs"].get(job_id)
			if job is None:
				continue
			if job["status"] in ACTIVE:
				job["status"] = ERROR
				job["error"] = "server restarted before the job " \
					"finished"
			self.jobs[job_id] = job
			self.order.append(job_id)

	def _save(self):
		dump_json(settings.JOBS_FILE,
			  {"order": self.order, "jobs": self.jobs})

	def _prune(self):
		while len(self.order) > settings.MAX_HISTORY:
			oldest = self.order[0]
			if self.jobs[oldest]["status"] in ACTIVE:
				break
			self._remove_files(self.jobs[oldest])
			del self.jobs[oldest]
			self.order.pop(0)

	# ---- public api ------------------------------------------------

	def submit(self, model, source, reference, params):
		"""
		source / reference are dicts {"path", "name", "duration"}.
		"""
		job_id = uuid.uuid4().hex[:12]
		total = engine.estimate_chunks(source.get("duration"),
					       reference.get("duration"))
		job = {
			"id": job_id,
			"status": QUEUED,
			"model": model,
			"model_label": MODEL_INFO[model]["label"],
			"params": params,
			"source": {"name": source["name"],
				   "url": source.get("url")},
			"reference": {"name": reference["name"],
				      "url": reference.get("url")},
			"created": time.time(),
			"started": None,
			"finished": None,
			"progress": {"done": 0, "total": total, "percent": 0},
			"output": None,
			"partial_url": None,
			"error": None,
		}
		with self.lock:
			self.jobs[job_id] = job
			self.order.append(job_id)
			self.cancel_flags[job_id] = threading.Event()
			self._prune()
			self._save()
		self.queue.put((job_id, source["path"], reference["path"]))
		return dict(job)

	def list(self):
		with self.lock:
			return [dict(self.jobs[j]) for j in reversed(self.order)]

	def get(self, job_id):
		with self.lock:
			job = self.jobs.get(job_id)
			return dict(job) if job else None

	def cancel(self, job_id):
		with self.lock:
			job = self.jobs.get(job_id)
			if job is None or job["status"] not in ACTIVE:
				return False
			self.cancel_flags[job_id].set()
			if job["status"] == QUEUED:
				self._finish(job, CANCELLED)
			return True

	def delete(self, job_id):
		with self.lock:
			job = self.jobs.get(job_id)
			if job is None or job["status"] == RUNNING:
				return False
			self.cancel_flags.pop(job_id, None)
			del self.jobs[job_id]
			self.order.remove(job_id)
			self._save()
		self._remove_files(job)
		return True

	def clear_finished(self):
		with self.lock:
			gone = [j for j in self.order
				if self.jobs[j]["status"] not in ACTIVE]
			for job_id in gone:
				self._remove_files(self.jobs[job_id])
				del self.jobs[job_id]
				self.order.remove(job_id)
				self.cancel_flags.pop(job_id, None)
			self._save()
		return len(gone)

	def queue_length(self):
		with self.lock:
			return sum(1 for j in self.jobs.values()
				   if j["status"] == QUEUED)

	# ---- worker ----------------------------------------------------

	def _run(self):
		while True:
			job_id, src, ref = self.queue.get()
			try:
				self._process(job_id, src, ref)
			except Exception:
				log.exception("worker crashed on job %s", job_id)
			finally:
				self.queue.task_done()

	def _process(self, job_id, src, ref):
		with self.lock:
			job = self.jobs.get(job_id)
			if job is None or job["status"] != QUEUED:
				return
			job["status"] = RUNNING
			job["started"] = time.time()
			job["partial_url"] = "files/outputs/%s.stream.mp3" \
				% job_id
			self.current = job_id
			self._save()
		cancel = self.cancel_flags[job_id]
		partial = settings.OUTPUT_DIR / ("%s.stream.mp3" % job_id)

		def on_chunk(n):
			with self.lock:
				prog = job["progress"]
				prog["done"] = n
				if n >= prog["total"]:
					prog["total"] = n + 1
				prog["percent"] = int(100 * n / prog["total"])

		try:
			sr, samples = engine.convert(job["model"], src, ref,
						     job["params"], on_chunk,
						     cancel, partial)
			out_name = "%s.wav" % job_id
			write_wav(settings.OUTPUT_DIR / out_name, samples, sr)
			with self.lock:
				job["output"] = {
					"url": "files/outputs/" + out_name,
					"file": out_name,
					"sr": sr,
					"duration": len(samples) / float(sr),
				}
				job["progress"]["percent"] = 100
				job["progress"]["done"] = job["progress"]["total"]
				self._finish(job, DONE)
		except Cancelled:
			with self.lock:
				self._finish(job, CANCELLED)
		except Exception as exc:
			log.error("job %s failed:\n%s", job_id,
				  traceback.format_exc())
			with self.lock:
				job["error"] = "%s: %s" % (type(exc).__name__, exc)
				self._finish(job, ERROR)
		finally:
			with self.lock:
				self.current = None

	def _finish(self, job, status):
		"""Caller holds self.lock."""
		job["status"] = status
		job["finished"] = time.time()
		if job["started"]:
			job["elapsed"] = round(job["finished"] - job["started"], 1)
		self._save()

	def _remove_files(self, job):
		for name in ("%s.wav" % job["id"], "%s.stream.mp3" % job["id"]):
			try:
				(settings.OUTPUT_DIR / name).unlink()
			except OSError:
				pass
