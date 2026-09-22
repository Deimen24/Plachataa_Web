#!/usr/bin/env python3
"""
Pre-download every checkpoint the web UI can need, so the first
conversion does not stall for minutes.  Safe to re-run; files already
in the cache are skipped.

Usage:  python tools/download_models.py [--v1] [--v2] [--rt]   (default: all)
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import settings  # noqa: E402

# (repo, [files]) fetched through seed-vc's hf_utils into
# <seed-vc>/checkpoints, exactly as the wrappers do at runtime.
V1_CKPTS = [
	("Plachta/Seed-VC", [
		"DiT_seed_v2_uvit_whisper_small_wavenet_bigvgan_pruned.pth",
		"config_dit_mel_seed_uvit_whisper_small_wavenet.yml",
		"DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema.pth",
		"config_dit_mel_seed_uvit_whisper_base_f0_44k.yml",
	]),
	("funasr/campplus", ["campplus_cn_common.bin"]),
	("lj1995/VoiceConversionWebUI", ["rmvpe.pt"]),
]
V2_CKPTS = [
	("Plachta/Seed-VC", ["v2/cfm_small.pth", "v2/ar_base.pth"]),
	("Plachta/ASTRAL-quantization", ["bsq32/bsq32_light.pth",
					 "bsq2048/bsq2048_light.pth"]),
	("funasr/campplus", ["campplus_cn_common.bin"]),
]

RT_CKPTS = [
	("Plachta/Seed-VC", ["DiT_uvit_tat_xlsr_ema.pth",
			     "config_dit_mel_seed_uvit_xlsr_tiny.yml"]),
	("funasr/campplus", ["campplus_cn_common.bin"]),
	("FunAudioLLM/CosyVoice-300M", ["hift.pt"]),
]

# Repos loaded with from_pretrained() (go to HF_HOME).
V1_HUB = [
	("openai/whisper-small", None),
	("nvidia/bigvgan_v2_22khz_80band_256x",
	 ["config.json", "bigvgan_generator.pt"]),
	("nvidia/bigvgan_v2_44khz_128band_512x",
	 ["config.json", "bigvgan_generator.pt"]),
]
V2_HUB = [
	("openai/whisper-small", None),
	("facebook/hubert-large-ll60k", None),
	("nvidia/bigvgan_v2_22khz_80band_256x",
	 ["config.json", "bigvgan_generator.pt"]),
]

RT_HUB = [("facebook/wav2vec2-xls-r-300m", None)]

# Skip the formats transformers never loads on this stack.
IGNORE = ["*.h5", "*.msgpack", "*.ot", "*.tflite", "*onnx*", "*.ckpt",
	  "*.gguf", "rust_model*", "flax_model*", "tf_model*"]


def fetch_ckpts(entries):
	from hf_utils import load_custom_model_from_hf
	for repo, files in entries:
		for name in files:
			print("  %s / %s" % (repo, name))
			load_custom_model_from_hf(repo, name, None)


def fetch_hub(entries):
	from huggingface_hub import hf_hub_download, snapshot_download
	for repo, files in entries:
		print("  %s" % repo)
		if files is None:
			snapshot_download(repo, ignore_patterns=ignore_for(repo))
			continue
		for name in files:
			hf_hub_download(repo, name)


def ignore_for(repo):
	"""
	whisper-small ships both .bin and .safetensors; transformers loads
	only one.  Skip the .bin when safetensors exist to halve the download.
	"""
	from huggingface_hub import list_repo_files
	ignore = list(IGNORE)
	try:
		names = list_repo_files(repo)
	except Exception:
		return ignore
	if any(n.endswith(".safetensors") for n in names):
		ignore.append("*.bin")
	return ignore


def main():
	p = argparse.ArgumentParser()
	p.add_argument("--v1", action="store_true")
	p.add_argument("--v2", action="store_true")
	p.add_argument("--rt", action="store_true",
		       help="real-time (tiny XLSR) model set")
	args = p.parse_args()
	if not (args.v1 or args.v2 or args.rt):
		args.v1 = args.v2 = args.rt = True

	settings.apply_environment()
	settings.ensure_dirs()
	if not settings.seedvc_present():
		sys.exit("seed-vc not found at %s" % settings.SEEDVC_DIR)
	sys.path.insert(0, str(settings.SEEDVC_DIR))
	os.chdir(settings.SEEDVC_DIR)
	print("HF cache: %s" % os.environ["HF_HOME"])
	print("seed-vc checkpoints: %s" % (settings.SEEDVC_DIR / "checkpoints"))

	if args.v1:
		print("\n[v1] checkpoints")
		fetch_ckpts(V1_CKPTS)
		print("[v1] hub models")
		fetch_hub(V1_HUB)
	if args.v2:
		print("\n[v2] checkpoints")
		fetch_ckpts(V2_CKPTS)
		print("[v2] hub models")
		fetch_hub(V2_HUB)
	if args.rt:
		print("\n[rt] checkpoints")
		fetch_ckpts(RT_CKPTS)
		print("[rt] hub models")
		fetch_hub(RT_HUB)
	print("\nAll models present.")


if __name__ == "__main__":
	main()
