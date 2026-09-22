#!/usr/bin/env python3
"""
Environment check used by the installers, the update scripts and the
user.  Runs on a bare Python (no torch needed) and reports:

  * Python version
  * NVIDIA GPU + driver version (via nvidia-smi)
  * whether that driver is new enough for the CUDA build of torch we
    install (CUDA 12.1 wheels)
  * with --torch: whether the installed torch can actually see the GPU

Exit codes:
  0   CUDA-ready (or --cpu-ok given and no GPU)
  10  no NVIDIA driver / nvidia-smi not found
  11  NVIDIA driver too old for the CUDA runtime
  12  torch installed but cannot use CUDA (only with --torch)
  13  python version unsupported
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys

# torch 2.4.0 cu121 wheels bundle CUDA 12.1; NVIDIA's compatibility table
# gives these minimum driver versions.
CUDA_VERSION = "12.1"
MIN_DRIVER = {"Linux": (525, 60), "Windows": (528, 33)}
TORCH_INDEX = {"cuda": "https://download.pytorch.org/whl/cu121",
	       "cpu": "https://download.pytorch.org/whl/cpu"}
PY_MIN = (3, 10)
PY_MAX = (3, 11)

EXIT_OK = 0
EXIT_NO_DRIVER = 10
EXIT_OLD_DRIVER = 11
EXIT_TORCH_NO_CUDA = 12
EXIT_BAD_PYTHON = 13

NVSMI_CANDIDATES = [
	r"C:\Windows\System32\nvidia-smi.exe",
	r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
	"/usr/bin/nvidia-smi",
	"/usr/lib/wsl/lib/nvidia-smi",
]


def find_nvsmi():
	found = shutil.which("nvidia-smi")
	if found:
		return found
	for cand in NVSMI_CANDIDATES:
		if os.path.isfile(cand):
			return cand
	return None


def parse_version(text):
	parts = []
	for tok in text.strip().split("."):
		digits = "".join(ch for ch in tok if ch.isdigit())
		if not digits:
			break
		parts.append(int(digits))
	return tuple(parts)


def query_gpus():
	"""
	Returns (gpus, driver_version, error).  gpus is a list of dicts.
	"""
	exe = find_nvsmi()
	if exe is None:
		return [], None, "nvidia-smi not found"
	cmd = [exe, "--query-gpu=name,driver_version,memory.total",
	       "--format=csv,noheader,nounits"]
	try:
		res = subprocess.run(cmd, capture_output=True, text=True,
				     timeout=30)
	except (OSError, subprocess.TimeoutExpired) as exc:
		return [], None, "nvidia-smi failed: %s" % exc
	if res.returncode != 0:
		msg = (res.stderr or res.stdout).strip()
		return [], None, "nvidia-smi error: " + msg[:300]
	gpus = []
	driver = None
	for line in res.stdout.strip().splitlines():
		cols = [c.strip() for c in line.split(",")]
		if len(cols) < 3:
			continue
		driver = cols[1]
		try:
			mem = int(float(cols[2]))
		except ValueError:
			mem = None
		gpus.append({"name": cols[0], "vram_mb": mem})
	return gpus, driver, None


def driver_ok(driver, system):
	need = MIN_DRIVER.get(system, MIN_DRIVER["Linux"])
	have = parse_version(driver or "0")
	return have >= need, need


def check_torch():
	try:
		import torch
	except Exception as exc:
		return {"installed": False, "error": str(exc)}
	info = {"installed": True, "version": torch.__version__,
		"cuda_build": torch.version.cuda,
		"cuda_available": torch.cuda.is_available()}
	if info["cuda_available"]:
		info["device"] = torch.cuda.get_device_name(0)
		cap = torch.cuda.get_device_capability(0)
		info["compute_capability"] = "%d.%d" % cap
	return info


def python_ok():
	ver = sys.version_info[:2]
	return PY_MIN <= ver <= PY_MAX


def build_report(args):
	system = platform.system()
	gpus, driver, err = query_gpus()
	report = {
		"system": system,
		"python": platform.python_version(),
		"python_ok": python_ok(),
		"gpus": gpus,
		"driver": driver,
		"driver_error": err,
		"cuda_version": CUDA_VERSION,
		"min_driver": ".".join(str(x) for x in
				       MIN_DRIVER.get(system, MIN_DRIVER["Linux"])),
		"driver_ok": False,
		"recommended_device": "cpu",
		"torch_index": TORCH_INDEX["cpu"],
	}
	if driver:
		ok, _need = driver_ok(driver, system)
		report["driver_ok"] = ok
		if ok:
			report["recommended_device"] = "cuda"
			report["torch_index"] = TORCH_INDEX["cuda"]
	if args.torch:
		report["torch"] = check_torch()
	return report


def print_human(rep):
	print("Plachataa Web environment check")
	print("  OS:      %s" % rep["system"])
	print("  Python:  %s%s" % (rep["python"],
				  "" if rep["python_ok"] else
				  "  (unsupported, need 3.10 or 3.11)"))
	if rep["gpus"]:
		for g in rep["gpus"]:
			print("  GPU:     %s (%s MB)" % (g["name"], g["vram_mb"]))
		state = "ok" if rep["driver_ok"] else \
			"TOO OLD, need >= %s for CUDA %s" % (rep["min_driver"],
							      rep["cuda_version"])
		print("  Driver:  %s  [%s]" % (rep["driver"], state))
	else:
		print("  GPU:     none detected (%s)" % rep["driver_error"])
	print("  Device:  %s" % rep["recommended_device"])
	torch = rep.get("torch")
	if torch is not None:
		if not torch["installed"]:
			print("  torch:   not installed (%s)" % torch["error"])
		else:
			print("  torch:   %s (cuda build %s), cuda available: %s"
			      % (torch["version"], torch["cuda_build"],
				 torch["cuda_available"]))
			if torch.get("device"):
				print("           using %s, compute capability %s"
				      % (torch["device"],
					 torch["compute_capability"]))


def exit_code(rep, args):
	if not rep["python_ok"] and not args.ignore_python:
		return EXIT_BAD_PYTHON
	if args.torch and rep.get("torch", {}).get("installed"):
		if rep["driver_ok"] and not rep["torch"]["cuda_available"]:
			return EXIT_TORCH_NO_CUDA
	if not rep["driver"]:
		return EXIT_OK if args.cpu_ok else EXIT_NO_DRIVER
	if not rep["driver_ok"]:
		return EXIT_OK if args.cpu_ok else EXIT_OLD_DRIVER
	return EXIT_OK


def main():
	p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	p.add_argument("--json", action="store_true",
		       help="machine readable output")
	p.add_argument("--torch", action="store_true",
		       help="also import torch and test CUDA")
	p.add_argument("--cpu-ok", action="store_true",
		       help="exit 0 even without a usable GPU")
	p.add_argument("--ignore-python", action="store_true")
	p.add_argument("--print", dest="field",
		       help="print one field of the report, e.g. torch_index")
	args = p.parse_args()
	rep = build_report(args)
	if args.field:
		print(rep.get(args.field, ""))
	elif args.json:
		print(json.dumps(rep, indent=1))
	else:
		print_human(rep)
	sys.exit(exit_code(rep, args))


if __name__ == "__main__":
	main()
