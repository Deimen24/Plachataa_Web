#!/usr/bin/env python3
"""
Offline test of the real-time pipeline with a stubbed model set: checks
buffer geometry, client-rate resampling, SOLA splicing and the
WebSocket protocol without any checkpoint.  Needs torch + fastapi.

    .venv/bin/python tools/test_realtime.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PLACHATAA_DATA_DIR"] = tempfile.mkdtemp(prefix="plachataa-rt-")

import numpy as np  # noqa: E402
import torch  # noqa: E402
import soundfile as sf  # noqa: E402

from server import settings  # noqa: E402
from server.realtime import RealtimeSession, clamp_params  # noqa: E402

SR = 22050
HOP = 256


class StubModel:
	"""Mimics the shapes of the DiT bundle used by RealtimeSession."""

	class Cfm:
		def inference(self, cat, lens, mel2, style, _x, n_timesteps,
			      inference_cfg_rate):
			frames = int(lens[0])
			t = torch.linspace(0, 1, frames)
			# a slowly varying "mel" so the vocoder makes a tone
			return t.repeat(80, 1).unsqueeze(0)

	def __init__(self):
		self.cfm = self.Cfm()

	def length_regulator(self, s, ylens, n_quantizers, f0):
		n = int(ylens[0])
		return (torch.zeros(1, n, 384),)


class StubModels:
	preset = "tiny"

	def __init__(self):
		self.device = torch.device("cpu")
		self.half = False
		self.sr = SR
		self.hop = HOP
		self.model = StubModel()

	def semantic_fn(self, waves_16k):
		return torch.zeros(1, waves_16k.size(-1) // 320, 1024)

	def campplus(self, feat):
		return torch.zeros(1, 192)

	def to_mel(self, x):
		frames = (x.size(-1) - 1024) // HOP + 1
		return torch.zeros(1, 80, frames)

	def vocoder(self, mel):
		n = mel.size(-1) * HOP
		t = torch.arange(n, dtype=torch.float32) / SR
		return (0.5 * torch.sin(2 * np.pi * 220 * t)).unsqueeze(0)


def make_reference(path):
	t = np.arange(SR * 4) / SR
	sf.write(path, (0.3 * np.sin(2 * np.pi * 300 * t)).astype(np.float32), SR)


def test_session(client_sr):
	ref = Path(settings.DATA_DIR) / "ref.wav"
	make_reference(ref)
	params = {"block_time": 0.26, "extra_time_right": 0.5, "threshold_db": -40}
	s = RealtimeSession(StubModels(), ref, params, client_sr)
	info = s.info()
	assert info["client_sr"] == client_sr
	assert s.block_frame % s.zc == 0
	expect = round(s.block_frame * client_sr / SR)
	assert info["block_samples"] == expect, (info, expect)
	t = np.arange(info["block_samples"]) / client_sr
	loud = (0.5 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
	quiet = np.zeros_like(loud)
	outs = [s.process(loud) for _ in range(5)]
	for o in outs:
		assert o.shape == (info["block_samples"],), o.shape
		assert o.dtype == np.float32
		assert np.isfinite(o).all()
	assert np.abs(outs[-1]).max() > 0.05, "no audio produced"
	gated = s.process(quiet)
	# The first samples still crossfade out the previous block (SOLA);
	# the rest must be silent.
	half = gated.shape[0] // 2
	assert np.abs(gated[half:]).max() < 0.05, np.abs(gated[half:]).max()
	assert s.stats["gated"] == 1
	# push-to-talk released: loud input must be muted too
	s.talk = False
	muted = s.process(loud)
	assert np.abs(muted[half:]).max() < 0.05
	s.talk = True
	assert np.abs(s.process(loud)).max() > 0.05
	assert info["fp16"] is False
	print("ok: session at client rate %d (block %d samples, delay %d ms)"
	      % (client_sr, info["block_samples"], info["algorithm_latency_ms"]))
	return s


def test_params():
	p = clamp_params({"block_time": 99, "diffusion_steps": "7",
			  "extra_time_ce": 0.2, "extra_time": 1.0, "junk": 1})
	assert p["block_time"] == 3.0 and p["diffusion_steps"] == 7
	assert p["extra_time_ce"] >= p["extra_time"]
	print("ok: parameter clamping")


def test_websocket():
	from fastapi.testclient import TestClient
	from server import main as srv
	from server.engine import engine
	engine.rt = {"tiny": StubModels()}
	engine.load_rt = lambda preset: engine.rt["tiny"]
	ref = srv.voices.add_copy("ref", Path(settings.DATA_DIR) / "ref.wav")
	client = TestClient(srv.app)
	with client.websocket_connect("/ws/realtime") as ws:
		ws.send_text(json.dumps({"type": "start", "voice_id": ref["id"],
					 "sample_rate": 48000, "model": "tiny",
					 "params": {"block_time": 0.2, "fp16": 1}}))
		msg = json.loads(ws.receive_text())
		assert msg["type"] == "loading", msg
		msg = json.loads(ws.receive_text())
		assert msg["type"] == "ready", msg
		assert msg["model"] == "tiny" and msg["fp16"] is False  # cpu
		n = msg["block_samples"]
		block = (0.3 * np.random.randn(n)).astype(np.float32)
		for _ in range(3):
			ws.send_bytes(block.tobytes())
		got = 0
		while got < 3:
			data = ws.receive()
			if "bytes" in data and data["bytes"] is not None:
				assert len(data["bytes"]) == n * 4
				got += 1
		ws.send_text(json.dumps({"type": "talk", "on": False}))
		ws.send_bytes(block.tobytes())
		while True:
			data = ws.receive()
			if "bytes" in data and data["bytes"] is not None:
				out = np.frombuffer(data["bytes"], dtype=np.float32)
				assert np.abs(out[n // 2:]).max() < 0.05
				break
		ws.send_text(json.dumps({"type": "stop"}))
	assert not srv.rt_active
	print("ok: websocket start/stream/talk/stop")


def main():
	settings.apply_environment()
	settings.ensure_dirs()
	test_params()
	test_session(SR)
	test_session(48000)
	test_session(44100)
	test_websocket()
	print("all real-time tests passed")


if __name__ == "__main__":
	main()
