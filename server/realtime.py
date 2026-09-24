"""
Real-time voice conversion, ported from seed-vc's real-time-gui.py.

The desktop GUI reads the microphone with sounddevice; here the browser
captures audio, sends fixed-size blocks over a WebSocket and plays the
converted blocks back.  The DSP is the same: a sliding input window
with extra context on both sides, the tiny XLSR DiT model with HiFT
vocoder, and SOLA (from DDSP-SVC) to splice consecutive blocks without
clicks.  Voice activity detection uses an RMS gate instead of the
funasr VAD model, which is not installed.

Only one RealtimeSession should run at a time per GPU.
"""

import logging
import math
import threading
import time

import numpy as np

from . import settings

log = logging.getLogger("plachataa.realtime")

FRAMES_PER_SECOND = 50		# content-encoder frame rate (20 ms)
SAMPLES_16K_PER_FRAME = 320

# Model presets usable in real time.  "tiny" is the upstream real-time
# model; "small" is the offline v1 speech model, noticeably better but
# roughly 4x the GPU work per block.
RT_MODELS = {
	"tiny": {
		"label": "Tiny (fastest, real-time model)",
		"ckpt": "DiT_uvit_tat_xlsr_ema.pth",
		"config": "config_dit_mel_seed_uvit_xlsr_tiny.yml",
		"hint": "25M params. Lowest delay; works on any RTX card.",
	},
	"small": {
		"label": "Small (better quality, Whisper-small)",
		"ckpt": "DiT_seed_v2_uvit_whisper_small_wavenet_bigvgan_pruned.pth",
		"config": "config_dit_mel_seed_uvit_whisper_small_wavenet.yml",
		"hint": "98M params, same model as file conversion. Needs a "
			"block time of 0.4 s or more on mid-range cards.",
	},
}
DEFAULT_MODEL = "tiny"

DEFAULT_PARAMS = {
	"fp16": 0,			# 1 = half precision (faster, slightly worse)
	"block_time": 0.25,		# seconds of audio per block
	"crossfade_time": 0.05,
	"extra_time_ce": 2.5,		# left context for the content encoder
	"extra_time": 0.5,		# left context for the DiT
	"extra_time_right": 2.0,	# right context (adds latency)
	"diffusion_steps": 10,
	"inference_cfg_rate": 0.7,
	"max_prompt_length": 3.0,	# seconds of reference used as prompt
	"threshold_db": -60,		# RMS gate; -60 disables
}

PARAM_LIMITS = {
	"fp16": (0, 1),
	"block_time": (0.04, 3.0),
	"crossfade_time": (0.02, 0.5),
	"extra_time_ce": (0.5, 10.0),
	"extra_time": (0.1, 5.0),
	"extra_time_right": (0.02, 10.0),
	"diffusion_steps": (1, 50),
	"inference_cfg_rate": (0.0, 1.0),
	"max_prompt_length": (1.0, 25.0),
	"threshold_db": (-60, 0),
}


def clamp_params(raw):
	out = dict(DEFAULT_PARAMS)
	for key, (lo, hi) in PARAM_LIMITS.items():
		if key in raw:
			try:
				val = float(raw[key])
			except (TypeError, ValueError):
				continue
			out[key] = min(max(val, lo), hi)
	out["diffusion_steps"] = int(out["diffusion_steps"])
	out["fp16"] = bool(int(out["fp16"]))
	if out["extra_time_ce"] < out["extra_time"]:
		out["extra_time_ce"] = out["extra_time"]
	return out


class RealtimeModels:
	"""
	One model set (port of load_models in real-time-gui.py): content
	encoder (XLSR or Whisper), DiT, CAMPPlus style encoder and vocoder
	(HiFT or BigVGAN).
	"""

	def __init__(self, device, preset=DEFAULT_MODEL):
		import torch
		import yaml
		from hf_utils import load_custom_model_from_hf
		from modules.commons import build_model, load_checkpoint, \
			recursive_munch
		from modules.audio import mel_spectrogram
		from modules.campplus.DTDNN import CAMPPlus

		self.torch = torch
		self.device = device
		self.preset = preset
		# Encoders run in half precision on CUDA as upstream does; the
		# CFM/vocoder precision is a per-session choice (fp16 param).
		self.half = device.type == "cuda"
		spec = RT_MODELS[preset]
		ckpt, cfg_path = load_custom_model_from_hf(
			"Plachta/Seed-VC", spec["ckpt"], spec["config"])
		with open(cfg_path, "r", encoding="utf-8") as fh:
			config = yaml.safe_load(fh)
		params = recursive_munch(config["model_params"])
		params.dit_type = "DiT"
		spect = config["preprocess_params"]["spect_params"]
		self.sr = int(config["preprocess_params"]["sr"])
		self.hop = int(spect["hop_length"])

		model = build_model(params, stage="DiT")
		model, _, _, _ = load_checkpoint(model, None, ckpt,
						 load_only_params=True,
						 ignore_modules=[],
						 is_distributed=False)
		for key in model:
			model[key].eval()
			model[key].to(device)
		model.cfm.estimator.setup_caches(max_batch_size=1,
						 max_seq_length=8192)
		self.model = model

		cp_path = load_custom_model_from_hf("funasr/campplus",
						    "campplus_cn_common.bin", None)
		self.campplus = CAMPPlus(feat_dim=80, embedding_size=192)
		self.campplus.load_state_dict(torch.load(cp_path,
							 map_location="cpu"))
		self.campplus.eval().to(device)

		self.vocoder = self._load_vocoder(params, load_custom_model_from_hf)
		tok_type = config["model_params"]["speech_tokenizer"]["type"]
		if tok_type == "xlsr":
			self.semantic_fn = self._load_xlsr(config)
		elif tok_type == "whisper":
			self.semantic_fn = self._load_whisper(config)
		else:
			raise RuntimeError("unsupported speech tokenizer " + tok_type)

		fmax = spect.get("fmax", "None")
		self.mel_args = {
			"n_fft": spect["n_fft"], "win_size": spect["win_length"],
			"hop_size": spect["hop_length"], "num_mels": spect["n_mels"],
			"sampling_rate": self.sr, "fmin": spect.get("fmin", 0),
			"fmax": None if fmax in (None, "None") else 8000,
			"center": False,
		}
		self.to_mel = lambda x: mel_spectrogram(x, **self.mel_args)

	def _load_vocoder(self, params, load_custom_model_from_hf):
		import yaml
		torch = self.torch
		kind = params.vocoder.type
		if kind == "hifigan":
			from modules.hifigan.generator import HiFTGenerator
			from modules.hifigan.f0_predictor import ConvRNNF0Predictor
			cfg_path = settings.SEEDVC_DIR / "configs" / "hifigan.yml"
			with open(cfg_path, "r", encoding="utf-8") as fh:
				hcfg = yaml.safe_load(fh)
			gen = HiFTGenerator(**hcfg["hift"], f0_predictor=
					    ConvRNNF0Predictor(**hcfg["f0_predictor"]))
			path = load_custom_model_from_hf(
				"FunAudioLLM/CosyVoice-300M", "hift.pt", None)
			gen.load_state_dict(torch.load(path, map_location="cpu"))
			return gen.eval().to(self.device)
		if kind == "bigvgan":
			from modules.bigvgan import bigvgan
			gen = bigvgan.BigVGAN.from_pretrained(params.vocoder.name,
							      use_cuda_kernel=False)
			gen.remove_weight_norm()
			return gen.eval().to(self.device)
		raise RuntimeError("unsupported vocoder " + str(kind))

	def _load_whisper(self, config):
		from transformers import AutoFeatureExtractor, WhisperModel
		torch = self.torch
		name = config["model_params"]["speech_tokenizer"]["name"]
		dtype = torch.float16 if self.half else torch.float32
		whisper = WhisperModel.from_pretrained(name, torch_dtype=dtype)
		whisper = whisper.to(self.device).eval()
		del whisper.decoder
		extractor = AutoFeatureExtractor.from_pretrained(name)

		def semantic_fn(waves_16k):
			inputs = extractor([waves_16k.squeeze(0).cpu().numpy()],
					   return_tensors="pt",
					   return_attention_mask=True,
					   sampling_rate=16000)
			feats = whisper._mask_input_features(
				inputs.input_features,
				attention_mask=inputs.attention_mask).to(self.device)
			with torch.no_grad():
				out = whisper.encoder(feats.to(whisper.encoder.dtype),
						      head_mask=None,
						      output_attentions=False,
						      output_hidden_states=False,
						      return_dict=True)
			hidden = out.last_hidden_state.float()
			return hidden[:, :waves_16k.size(-1) // 320 + 1]
		return semantic_fn

	def _load_xlsr(self, config):
		from transformers import Wav2Vec2FeatureExtractor, Wav2Vec2Model
		torch = self.torch
		tok = config["model_params"]["speech_tokenizer"]
		extractor = Wav2Vec2FeatureExtractor.from_pretrained(tok["name"])
		w2v = Wav2Vec2Model.from_pretrained(tok["name"])
		w2v.encoder.layers = w2v.encoder.layers[:tok["output_layer"]]
		w2v = w2v.to(self.device).eval()
		if self.half:
			w2v = w2v.half()
		in_dtype = torch.float16 if self.half else torch.float32

		def semantic_fn(waves_16k):
			inputs = extractor([w.cpu().numpy() for w in waves_16k],
					   return_tensors="pt",
					   return_attention_mask=True,
					   padding=True, sampling_rate=16000)
			values = inputs.input_values.to(self.device, in_dtype)
			with torch.no_grad():
				out = w2v(values)
			return out.last_hidden_state.float()
		return semantic_fn


class Resampler:
	"""
	Block resampler that keeps a little context between calls so block
	boundaries do not click.  Assumes a fixed input block length.
	"""

	def __init__(self, torch, src, dst, device, ctx=1024):
		self.torch = torch
		self.src = src
		self.dst = dst
		self.device = device
		self.ctx = ctx
		self.tail = torch.zeros(ctx, device=device)

	def __call__(self, block, out_len):
		import torchaudio
		if self.src == self.dst:
			return block
		joined = self.torch.cat([self.tail, block])
		self.tail = block[-self.ctx:] if block.numel() >= self.ctx \
			else joined[-self.ctx:]
		res = torchaudio.functional.resample(joined, self.src, self.dst)
		if res.numel() < out_len:
			pad = self.torch.zeros(out_len - res.numel(),
					       device=self.device)
			res = self.torch.cat([pad, res])
		return res[-out_len:]


class RealtimeSession:
	def __init__(self, models, reference_path, params, client_sr):
		import librosa
		import torch
		self.torch = torch
		self.m = models
		self.p = clamp_params(params)
		self.device = models.device
		self.sr = models.sr
		self.client_sr = int(client_sr) if client_sr else self.sr
		self.lock = threading.Lock()
		self.stats = {"blocks": 0, "infer_ms": 0.0, "gated": 0}
		self.talk = True		# push-to-talk: False mutes the input
		self.fp16 = bool(self.p["fp16"]) and self.device.type == "cuda"
		self._layout()
		wav, _ = librosa.load(str(reference_path), sr=self.sr)
		self._prompt(wav)
		self.in_res = Resampler(torch, self.client_sr, self.sr, self.device)
		self.out_res = Resampler(torch, self.sr, self.client_sr,
					 self.device)

	# ---- geometry (start_vc in the GUI) -----------------------------

	def _frames(self, seconds):
		return int(round(seconds * self.sr / self.zc)) * self.zc

	def _layout(self):
		torch = self.torch
		p = self.p
		self.zc = self.sr // FRAMES_PER_SECOND
		self.block_frame = self._frames(p["block_time"])
		self.block_frame_16k = SAMPLES_16K_PER_FRAME * self.block_frame \
			// self.zc
		self.crossfade_frame = self._frames(p["crossfade_time"])
		self.sola_buffer_frame = min(self.crossfade_frame, 4 * self.zc)
		self.sola_search_frame = self.zc
		self.extra_frame = self._frames(p["extra_time_ce"])
		self.extra_frame_right = self._frames(p["extra_time_right"])
		total = self.extra_frame + self.crossfade_frame + \
			self.sola_search_frame + self.block_frame + \
			self.extra_frame_right
		self.input_wav = torch.zeros(total, device=self.device)
		self.input_wav_res = torch.zeros(
			SAMPLES_16K_PER_FRAME * total // self.zc, device=self.device)
		self.sola_buffer = torch.zeros(self.sola_buffer_frame,
					       device=self.device)
		self.skip_head = self.extra_frame // self.zc
		self.skip_tail = self.extra_frame_right // self.zc
		self.return_length = (self.block_frame + self.sola_buffer_frame +
				      self.sola_search_frame) // self.zc
		ramp = torch.linspace(0.0, 1.0, steps=self.sola_buffer_frame,
				      device=self.device)
		self.fade_in = torch.sin(0.5 * math.pi * ramp) ** 2
		self.fade_out = 1 - self.fade_in
		self.ce_dit_diff = int((p["extra_time_ce"] - p["extra_time"]) *
				       FRAMES_PER_SECOND)
		self.client_block = int(round(self.block_frame * self.client_sr /
					      self.sr))

	def info(self):
		total = self.input_wav.numel()
		return {
			"model": self.m.preset,
			"fp16": self.fp16,
			"sr": self.sr,
			"client_sr": self.client_sr,
			"block_samples": self.client_block,
			"block_ms": round(1000.0 * self.block_frame / self.sr),
			"algorithm_latency_ms": round(1000.0 * (
				self.block_frame + self.extra_frame_right +
				self.sola_buffer_frame) / self.sr),
			"window_ms": round(1000.0 * total / self.sr),
			"params": self.p,
		}

	# ---- reference prompt (first half of custom_infer) ---------------

	def _prompt(self, wav):
		import torchaudio
		torch = self.torch
		m = self.m
		wav = wav[:int(self.sr * self.p["max_prompt_length"])]
		ref = torch.from_numpy(wav).float().to(self.device)
		ref16 = torchaudio.functional.resample(ref, self.sr, 16000)
		with torch.no_grad():
			s_ori = m.semantic_fn(ref16.unsqueeze(0))
			feat = torchaudio.compliance.kaldi.fbank(
				ref16.unsqueeze(0), num_mel_bins=80, dither=0,
				sample_frequency=16000)
			feat = feat - feat.mean(dim=0, keepdim=True)
			self.style = m.campplus(feat.unsqueeze(0))
			self.mel2 = m.to_mel(ref.unsqueeze(0))
			lens = torch.LongTensor([self.mel2.size(2)]).to(self.device)
			self.prompt_cond = m.model.length_regulator(
				s_ori, ylens=lens, n_quantizers=3, f0=None)[0]

	# ---- per block -----------------------------------------------------

	def process(self, pcm):
		"""
		pcm: float32 numpy block of client_block samples at client_sr.
		Returns a float32 numpy block of the same size.
		"""
		with self.lock:
			t0 = time.perf_counter()
			block = self.torch.from_numpy(np.ascontiguousarray(pcm)) \
				.float().to(self.device)
			block = self.in_res(block, self.block_frame)
			self._push(block)
			with self.torch.no_grad():
				voiced = self.talk and self._voiced(block)
				out = self._infer() if voiced else self._silence()
				out = self._sola(out)
				out = self.out_res(out, self.client_block)
			self.stats["blocks"] += 1
			self.stats["infer_ms"] = round(
				(time.perf_counter() - t0) * 1000, 1)
			return out.clamp_(-1.0, 1.0).cpu().numpy()

	def _voiced(self, block):
		thr = self.p["threshold_db"]
		if thr <= -60:
			return True
		rms = float(self.torch.sqrt((block ** 2).mean() + 1e-12))
		db = 20 * math.log10(rms + 1e-9)
		if db < thr:
			self.stats["gated"] += 1
			return False
		return True

	def _push(self, block):
		import torchaudio
		bf = self.block_frame
		self.input_wav[:-bf] = self.input_wav[bf:].clone()
		self.input_wav[-bf:] = block
		bf16 = self.block_frame_16k
		self.input_wav_res[:-bf16] = self.input_wav_res[bf16:].clone()
		tail = self.input_wav[-(bf + 2 * self.zc):]
		res = torchaudio.functional.resample(tail, self.sr, 16000)
		n = SAMPLES_16K_PER_FRAME * (bf // self.zc + 1)
		self.input_wav_res[-n:] = res[SAMPLES_16K_PER_FRAME:][-n:]

	def _silence(self):
		n = self.return_length * self.sr // FRAMES_PER_SECOND
		return self.torch.zeros(n, device=self.device)

	def _infer(self):
		torch = self.torch
		m = self.m
		s_alt = m.semantic_fn(self.input_wav_res.unsqueeze(0))
		s_alt = s_alt[:, self.ce_dit_diff:]
		frames = self.skip_head + self.return_length + self.skip_tail - \
			self.ce_dit_diff
		lens = torch.LongTensor([frames * self.sr // FRAMES_PER_SECOND //
					 self.m.hop]).to(self.device)
		cond = m.model.length_regulator(s_alt, ylens=lens, n_quantizers=3,
						f0=None)[0]
		cat = torch.cat([self.prompt_cond, cond], dim=1)
		with torch.autocast(device_type=self.device.type,
				    dtype=torch.float16, enabled=self.fp16):
			target = m.model.cfm.inference(
				cat, torch.LongTensor([cat.size(1)]).to(self.device),
				self.mel2, self.style, None,
				n_timesteps=self.p["diffusion_steps"],
				inference_cfg_rate=self.p["inference_cfg_rate"])
			target = target[:, :, self.mel2.size(-1):]
			wave = m.vocoder(target.float()).squeeze()
		out_len = self.return_length * self.sr // FRAMES_PER_SECOND
		tail = self.skip_tail * self.sr // FRAMES_PER_SECOND
		if tail > 0:
			return wave[-out_len - tail:-tail]
		return wave[-out_len:]

	def _sola(self, wave):
		"""SOLA splice (DDSP-SVC): align the new block to the last one."""
		import torch.nn.functional as F
		torch = self.torch
		n = self.sola_buffer_frame
		probe = wave[None, None, :n + self.sola_search_frame]
		nom = F.conv1d(probe, self.sola_buffer[None, None, :])
		den = torch.sqrt(F.conv1d(probe ** 2, torch.ones(
			1, 1, n, device=self.device)) + 1e-8)
		corr = nom[0, 0] / den[0, 0]
		offset = int(torch.argmax(corr).item()) if corr.numel() > 1 else 0
		wave = wave[offset:]
		wave[:n] *= self.fade_in
		wave[:n] += self.sola_buffer * self.fade_out
		self.sola_buffer[:] = wave[self.block_frame:self.block_frame + n]
		return wave[:self.block_frame].clone()
