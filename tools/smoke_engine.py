#!/usr/bin/env python3
"""
Offline smoke test of the seed-vc integration: imports every module the
two inference wrappers need and builds the v1 DiT from the preset config
shipped in the checkout.  No model download, no GPU required.  Verifies
that requirements-seedvc.txt is complete for the pinned revision.

    .venv/bin/python tools/smoke_engine.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import settings  # noqa: E402
from server.engine import bootstrap, engine  # noqa: E402


def step(name):
	print("  %-46s" % name, end="", flush=True)
	return time.time()


def done(t0):
	print("ok (%.1fs)" % (time.time() - t0))


def main():
	print("seed-vc dir: %s" % settings.SEEDVC_DIR)
	t = step("bootstrap + torch")
	bootstrap()
	torch = engine.torch()
	done(t)
	print("  torch %s, device %s" % (torch.__version__, engine.device()))

	t = step("import seed_vc_wrapper (v1)")
	import seed_vc_wrapper  # noqa: F401
	done(t)

	t = step("import modules.v2.vc_wrapper (v2)")
	import modules.v2.vc_wrapper  # noqa: F401
	from modules.astral_quantization import default_model  # noqa: F401
	done(t)

	t = step("import bigvgan, rmvpe, campplus")
	from modules.bigvgan import bigvgan  # noqa: F401
	from modules.rmvpe import RMVPE  # noqa: F401
	from modules.campplus.DTDNN import CAMPPlus  # noqa: F401
	done(t)

	t = step("build v1 DiT from preset config")
	import yaml
	from modules.commons import build_model, recursive_munch
	cfg_path = settings.SEEDVC_DIR / "configs" / "presets" / \
		"config_dit_mel_seed_uvit_whisper_small_wavenet.yml"
	with open(cfg_path, "r", encoding="utf-8") as fh:
		config = yaml.safe_load(fh)
	params = recursive_munch(config["model_params"])
	params.dit_type = "DiT"
	model = build_model(params, stage="DiT")
	n = sum(p.numel() for k in model for p in model[k].parameters())
	done(t)
	print("  DiT parameters: %.1fM" % (n / 1e6))

	t = step("build CAMPPlus style encoder")
	CAMPPlus(feat_dim=80, embedding_size=192)
	done(t)

	t = step("ffmpeg available")
	from server.audio import ffmpeg_path
	assert ffmpeg_path(), "no ffmpeg"
	done(t)
	print("smoke test passed")


if __name__ == "__main__":
	main()
