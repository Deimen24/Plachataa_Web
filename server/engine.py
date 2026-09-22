"""
Thin, thread-safe wrapper around the seed-vc inference classes.

seed-vc is not a package: its modules use relative imports from the
checkout root and download checkpoints into ./checkpoints relative to
the working directory.  bootstrap() therefore puts the checkout on
sys.path and makes it the working directory once per process.

Only one conversion runs at a time (jobs.py serialises them), so the
engine does not need per-call locking beyond the load/unload lock.
"""

import gc
import math
import os
import sys
import threading
import time
import logging

from . import settings

log = logging.getLogger("plachataa.engine")

MODEL_V1 = "v1"
MODEL_V1_F0 = "v1_f0"
MODEL_V2 = "v2"
MODELS = (MODEL_V1, MODEL_V1_F0, MODEL_V2)

MODEL_INFO = {
	MODEL_V1: {
		"label": "Voice conversion (v1)",
		"family": "v1",
		"sr": 22050,
		"hint": "Speech to speech. Fast, good default.",
	},
	MODEL_V1_F0: {
		"label": "Singing voice conversion (v1 + F0)",
		"family": "v1",
		"sr": 44100,
		"hint": "Pitch-conditioned 44.1 kHz model for singing.",
	},
	MODEL_V2: {
		"label": "Voice + accent conversion (v2)",
		"family": "v2",
		"sr": 22050,
		"hint": "Best at removing the source speaker; can convert "
			"style/accent.",
	},
}

# seed-vc processes the source in windows of 30 s minus the reference
# length; used only to estimate progress.
CONTEXT_SECONDS = 30.0


class Cancelled(Exception):
	pass


class EngineError(Exception):
	pass


_bootstrapped = False


def bootstrap():
	global _bootstrapped
	if _bootstrapped:
		return
	if not settings.seedvc_present():
		raise EngineError("seed-vc checkout not found at %s; run the "
				  "installer" % settings.SEEDVC_DIR)
	settings.apply_environment()
	sys.path.insert(0, str(settings.SEEDVC_DIR))
	os.chdir(settings.SEEDVC_DIR)
	_bootstrapped = True


def _pick_device(torch):
	want = settings.DEVICE
	if want == "auto":
		if torch.cuda.is_available():
			return torch.device("cuda")
		mps = getattr(torch.backends, "mps", None)
		if mps is not None and mps.is_available():
			return torch.device("mps")
		return torch.device("cpu")
	return torch.device(want)


def _configure_pydub():
	from pydub import AudioSegment
	from .audio import ffmpeg_path
	exe = ffmpeg_path()
	if exe:
		AudioSegment.converter = exe


class Engine:
	def __init__(self):
		self.lock = threading.Lock()
		self.v1 = None
		self.v2 = None
		self._device = None
		self._torch = None
		self.load_times = {}
		self.loading = set()
		self.load_errors = {}

	# ---- device / status ------------------------------------------

	def torch(self):
		if self._torch is None:
			bootstrap()
			import torch
			self._torch = torch
		return self._torch

	def device(self):
		if self._device is None:
			self._device = _pick_device(self.torch())
		return self._device

	def status(self):
		try:
			torch = self.torch()
		except Exception as exc:
			return {"ok": False, "error": "%s: %s" % (
				type(exc).__name__, exc)}
		dev = self.device()
		info = {
			"ok": True,
			"device": dev.type,
			"torch": torch.__version__,
			"cuda": torch.version.cuda,
			"gpu": None,
			"vram_total_mb": None,
			"vram_used_mb": None,
			"loaded": {
				"v1": self.v1 is not None,
				"v2": self.v2 is not None,
			},
			"loading": sorted(self.loading),
			"load_errors": self.load_errors,
			"load_times": self.load_times,
		}
		if dev.type == "cuda":
			props = torch.cuda.get_device_properties(dev)
			info["gpu"] = props.name
			info["vram_total_mb"] = props.total_memory // (1 << 20)
			info["vram_used_mb"] = torch.cuda.memory_allocated(dev) \
				// (1 << 20)
		return info

	# ---- loading ---------------------------------------------------

	def load(self, family):
		if family not in ("v1", "v2"):
			raise EngineError("unknown model family " + str(family))
		with self.lock:
			self.loading.add(family)
			self.load_errors.pop(family, None)
			try:
				if family == "v1":
					self._load_v1()
				else:
					self._load_v2()
			except Exception as exc:
				self.load_errors[family] = "%s: %s" % (
					type(exc).__name__, exc)
				raise
			finally:
				self.loading.discard(family)

	def load_in_background(self, family):
		t = threading.Thread(target=self._load_quietly, args=(family,),
				     daemon=True, name="load-" + family)
		t.start()

	def _load_quietly(self, family):
		try:
			self.load(family)
		except Exception:
			log.exception("failed to load %s", family)

	def _load_v1(self):
		if self.v1 is not None:
			return
		bootstrap()
		_configure_pydub()
		t0 = time.time()
		log.info("loading seed-vc v1 models (this downloads ~3 GB the "
			 "first time)")
		from seed_vc_wrapper import SeedVCWrapper
		self.v1 = SeedVCWrapper(device=self.device())
		self.load_times["v1"] = round(time.time() - t0, 1)
		log.info("v1 ready in %.1fs", self.load_times["v1"])

	def _load_v2(self):
		if self.v2 is not None:
			return
		bootstrap()
		_configure_pydub()
		torch = self.torch()
		t0 = time.time()
		log.info("loading seed-vc v2 models (this downloads ~3 GB the "
			 "first time)")
		import yaml
		from hydra.utils import instantiate
		from omegaconf import DictConfig
		cfg_path = settings.SEEDVC_DIR / "configs" / "v2" / \
			"vc_wrapper.yaml"
		with open(cfg_path, "r", encoding="utf-8") as fh:
			cfg = DictConfig(yaml.safe_load(fh))
		wrapper = instantiate(cfg)
		wrapper.load_checkpoints()
		wrapper.to(self.device())
		wrapper.eval()
		wrapper.setup_ar_caches(max_batch_size=1, max_seq_len=4096,
					dtype=self._dtype(), device=self.device())
		self.v2 = wrapper
		self.load_times["v2"] = round(time.time() - t0, 1)
		log.info("v2 ready in %.1fs", self.load_times["v2"])

	def _dtype(self):
		torch = self.torch()
		if self.device().type == "cuda":
			return torch.float16
		return torch.float32

	def unload(self, family=None):
		with self.lock:
			if family in (None, "v1"):
				self.v1 = None
				self.load_times.pop("v1", None)
			if family in (None, "v2"):
				self.v2 = None
				self.load_times.pop("v2", None)
			self._free_memory()

	def _free_memory(self):
		gc.collect()
		torch = self._torch
		if torch is not None and torch.cuda.is_available():
			torch.cuda.empty_cache()

	# ---- conversion ------------------------------------------------

	def estimate_chunks(self, src_seconds, ref_seconds):
		ref = min(ref_seconds or 0.0, settings.MAX_REFERENCE_SECONDS)
		window = max(CONTEXT_SECONDS - ref, 1.0)
		return max(1, int(math.ceil((src_seconds or 0.0) / window)))

	def convert(self, model, source, reference, params, on_chunk,
		    cancel, partial_path=None):
		"""
		Run one conversion.  on_chunk(n) is called after each streamed
		chunk; cancel is a threading.Event.  Returns (sr, samples).
		"""
		if model not in MODELS:
			raise EngineError("unknown model " + str(model))
		family = MODEL_INFO[model]["family"]
		self.load(family)
		if family == "v1":
			gen = self._v1_generator(model, source, reference, params)
		else:
			gen = self._v2_generator(source, reference, params)
		try:
			return self._drain(gen, on_chunk, cancel, partial_path)
		finally:
			gen.close()
			self._free_memory()

	def _drain(self, gen, on_chunk, cancel, partial_path):
		result = None
		n = 0
		part = open(partial_path, "ab") if partial_path else None
		try:
			for mp3_bytes, full in gen:
				n += 1
				if part is not None and mp3_bytes:
					part.write(mp3_bytes)
					part.flush()
				on_chunk(n)
				if full is not None:
					result = full
				if cancel.is_set():
					raise Cancelled()
		finally:
			if part is not None:
				part.close()
		if result is None:
			raise EngineError("model produced no audio")
		sr, samples = result
		return int(sr), samples

	def _v1_generator(self, model, source, reference, params):
		f0 = model == MODEL_V1_F0
		return self.v1.convert_voice(
			source=str(source),
			target=str(reference),
			diffusion_steps=int(params.get("diffusion_steps", 10)),
			length_adjust=float(params.get("length_adjust", 1.0)),
			inference_cfg_rate=float(params.get("cfg_rate", 0.7)),
			f0_condition=f0,
			auto_f0_adjust=bool(params.get("auto_f0_adjust", True)),
			pitch_shift=int(params.get("pitch_shift", 0)),
			stream_output=True,
		)

	def _v2_generator(self, source, reference, params):
		return self.v2.convert_voice_with_streaming(
			source_audio_path=str(source),
			target_audio_path=str(reference),
			diffusion_steps=int(params.get("diffusion_steps", 30)),
			length_adjust=float(params.get("length_adjust", 1.0)),
			intelligebility_cfg_rate=float(
				params.get("intelligibility_cfg_rate", 0.0)),
			similarity_cfg_rate=float(
				params.get("similarity_cfg_rate", 0.7)),
			top_p=float(params.get("top_p", 0.9)),
			temperature=float(params.get("temperature", 1.0)),
			repetition_penalty=float(
				params.get("repetition_penalty", 1.0)),
			convert_style=bool(params.get("convert_style", False)),
			anonymization_only=bool(
				params.get("anonymization_only", False)),
			device=self.device(),
			dtype=self._dtype(),
			stream_output=True,
		)


engine = Engine()
