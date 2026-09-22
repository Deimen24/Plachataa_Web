#!/usr/bin/env python3
"""
Job-lifecycle test that runs without models: the engine's convert()
is replaced by a stub that streams a few chunks.  Exercises submit,
progress, output writing, cancel, delete and persistence.

    python tools/test_jobs.py
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp(prefix="plachataa-test-")
os.environ["PLACHATAA_DATA_DIR"] = tmp

from server import settings  # noqa: E402
from server import jobs as jobs_mod  # noqa: E402
from server.engine import engine, Cancelled  # noqa: E402

settings.apply_environment()
settings.ensure_dirs()

import numpy as np  # noqa: E402


def fake_convert(model, source, reference, params, on_chunk, cancel,
		 partial_path=None):
	sr = 22050
	chunks = []
	for i in range(3):
		time.sleep(0.05)
		if cancel.is_set():
			raise Cancelled()
		chunks.append(np.zeros(sr // 10, dtype=np.float32))
		if partial_path:
			with open(partial_path, "ab") as fh:
				fh.write(b"\xff\xfb")
		on_chunk(i + 1)
	return sr, np.concatenate(chunks)


def wait_for(store, job_id, states, timeout=5):
	t0 = time.time()
	while time.time() - t0 < timeout:
		job = store.get(job_id)
		if job["status"] in states:
			return job
		time.sleep(0.02)
	raise AssertionError("timeout waiting for %s" % states)


def audio(name):
	path = Path(tmp) / name
	path.write_bytes(b"RIFF")
	return {"path": path, "name": name, "duration": 12.0, "url": "/x"}


def main():
	engine.convert = fake_convert
	engine.estimate_chunks = lambda s, r: 3
	store = jobs_mod.JobStore()

	job = store.submit("v1", audio("a.wav"), audio("b.wav"),
			   {"diffusion_steps": 10})
	assert job["status"] == "queued"
	done = wait_for(store, job["id"], ("done", "error"))
	assert done["status"] == "done", done
	assert done["progress"]["percent"] == 100
	out = settings.OUTPUT_DIR / done["output"]["file"]
	assert out.is_file() and out.stat().st_size > 44
	assert abs(done["output"]["duration"] - 0.3) < 0.01
	print("ok: job completes and writes wav")

	# cancel a running job
	engine.convert = lambda *a, **k: slow_convert(*a, **k)
	job2 = store.submit("v2", audio("a.wav"), audio("b.wav"), {})
	wait_for(store, job2["id"], ("running",))
	assert store.cancel(job2["id"])
	c = wait_for(store, job2["id"], ("cancelled", "done", "error"))
	assert c["status"] == "cancelled", c
	print("ok: running job cancels")

	# cancel a queued job while another runs
	job3 = store.submit("v1", audio("a.wav"), audio("b.wav"), {})
	job4 = store.submit("v1", audio("a.wav"), audio("b.wav"), {})
	assert store.cancel(job4["id"])
	assert store.get(job4["id"])["status"] == "cancelled"
	store.cancel(job3["id"])
	wait_for(store, job3["id"], ("cancelled", "done"))
	print("ok: queued job cancels")

	# delete removes files; persistence marks interrupted jobs
	assert store.delete(job["id"])
	assert not out.exists()
	store2 = jobs_mod.JobStore()
	assert store2.get(job2["id"])["status"] == "cancelled"
	print("ok: delete and reload")
	print("all job tests passed")


def slow_convert(model, source, reference, params, on_chunk, cancel,
		 partial_path=None):
	for i in range(50):
		time.sleep(0.05)
		on_chunk(i + 1)
		if cancel.is_set():
			raise Cancelled()
	return 22050, np.zeros(100, dtype=np.float32)


if __name__ == "__main__":
	main()
