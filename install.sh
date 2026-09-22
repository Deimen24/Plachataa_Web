#!/usr/bin/env bash
#
# Plachataa Web installer for Linux (Debian/Ubuntu, Fedora, Arch; others
# work if python3.10/3.11, git and a C compiler-free wheel set are
# available).  Idempotent: re-run it any time.
#
#   ./install.sh                 auto-detect GPU, install everything
#   ./install.sh --cpu           force the CPU build of torch (slow)
#   ./install.sh --download-models   also pre-fetch all checkpoints (~6 GB)
#   ./install.sh --update-driver     install/upgrade the NVIDIA driver
#                                    when it is missing or too old
#   ./install.sh --yes           never prompt (for the above)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
# shellcheck source=seedvc.lock
source "$ROOT/seedvc.lock"

VENV="$ROOT/.venv"
VENDOR="$ROOT/vendor/seed-vc"
TORCH_PKGS="torch==2.4.0 torchvision==0.19.0 torchaudio==2.4.0"

FORCE_DEVICE=""
DOWNLOAD_MODELS=0
UPDATE_DRIVER=0
ASSUME_YES=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

confirm() {
	[ "$ASSUME_YES" = 1 ] && return 0
	read -r -p "$1 [y/N] " ans
	[[ "$ans" =~ ^[Yy] ]]
}

parse_args() {
	while [ $# -gt 0 ]; do
		case "$1" in
		--cpu) FORCE_DEVICE=cpu ;;
		--cuda) FORCE_DEVICE=cuda ;;
		--download-models) DOWNLOAD_MODELS=1 ;;
		--update-driver) UPDATE_DRIVER=1 ;;
		--yes|-y) ASSUME_YES=1 ;;
		-h|--help) usage ;;
		*) die "unknown option: $1" ;;
		esac
		shift
	done
}

find_python() {
	local cand
	for cand in python3.10 python3.11 python3; do
		if command -v "$cand" >/dev/null 2>&1 &&
		   "$cand" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,11) else 1)' 2>/dev/null; then
			echo "$cand"
			return 0
		fi
	done
	return 1
}

pkg_manager() {
	if command -v apt-get >/dev/null; then echo apt
	elif command -v dnf >/dev/null; then echo dnf
	elif command -v pacman >/dev/null; then echo pacman
	else echo none
	fi
}

system_deps_present() {
	local py
	command -v git >/dev/null || return 1
	command -v ffmpeg >/dev/null || return 1
	py="$(find_python)" || return 1
	"$py" -m venv --help >/dev/null 2>&1
}

install_system_deps() {
	local pm; pm="$(pkg_manager)"
	if system_deps_present; then
		log "System packages present (git, python, ffmpeg)"
		return
	fi
	log "Installing system packages (git, python3.10 + venv, ffmpeg)"
	case "$pm" in
	apt)
		local pkgs="git ffmpeg libsndfile1"
		if ! find_python >/dev/null; then
			pkgs="$pkgs python3.10 python3.10-venv python3.10-dev"
			# python3.10 is not in newer Ubuntu default repos.
			if ! apt-cache show python3.10 >/dev/null 2>&1; then
				warn "python3.10 not in apt; adding deadsnakes PPA"
				sudo apt-get install -y software-properties-common
				sudo add-apt-repository -y ppa:deadsnakes/ppa
			fi
		fi
		sudo apt-get update -qq
		# shellcheck disable=SC2086
		sudo apt-get install -y $pkgs
		local py; py="$(find_python)" || true
		if [ -n "$py" ] && ! "$py" -m venv --help >/dev/null 2>&1; then
			sudo apt-get install -y "${py}-venv"
		fi
		;;
	dnf)
		sudo dnf install -y git ffmpeg-free libsndfile python3.11 || \
			sudo dnf install -y git ffmpeg libsndfile python3.11
		;;
	pacman)
		sudo pacman -S --needed --noconfirm git ffmpeg libsndfile python
		;;
	*)
		warn "unknown package manager; make sure git, ffmpeg and python 3.10/3.11 are installed"
		;;
	esac
}

vendor_seedvc() {
	log "Fetching seed-vc @ ${SEEDVC_COMMIT:0:10}"
	mkdir -p "$ROOT/vendor"
	if [ ! -d "$VENDOR/.git" ]; then
		git clone --quiet "$SEEDVC_REPO" "$VENDOR"
	fi
	git -C "$VENDOR" fetch --quiet origin
	git -C "$VENDOR" checkout --quiet "$SEEDVC_COMMIT"
}

# ---- NVIDIA driver -------------------------------------------------------

has_nvidia_gpu() {
	if command -v lspci >/dev/null; then
		lspci 2>/dev/null | grep -qi 'nvidia'
	else
		[ -e /proc/driver/nvidia ] || [ -e /dev/nvidia0 ]
	fi
}

install_nvidia_driver() {
	local pm; pm="$(pkg_manager)"
	log "Installing/upgrading the NVIDIA driver via $pm"
	case "$pm" in
	apt)
		sudo apt-get update -qq
		if command -v ubuntu-drivers >/dev/null 2>&1 || \
		   sudo apt-get install -y ubuntu-drivers-common; then
			sudo ubuntu-drivers install
		else
			sudo apt-get install -y nvidia-driver-550
		fi
		;;
	dnf)
		# RPM Fusion must be enabled on Fedora.
		sudo dnf install -y akmod-nvidia xorg-x11-drv-nvidia-cuda
		;;
	pacman)
		sudo pacman -S --needed --noconfirm nvidia nvidia-utils
		;;
	*)
		die "cannot install a driver automatically here; get it from https://www.nvidia.com/Download/index.aspx"
		;;
	esac
	echo
	warn "Driver installed. REBOOT, then run ./install.sh again."
	exit 0
}

# Sets DEVICE to cuda or cpu.
decide_device() {
	local py="$1" rc=0
	"$py" tools/check_env.py --ignore-python || rc=$?
	if [ -n "$FORCE_DEVICE" ]; then
		DEVICE="$FORCE_DEVICE"
		log "Device forced to $DEVICE"
		return
	fi
	case "$rc" in
	0)
		DEVICE=cuda ;;
	10|11)
		if has_nvidia_gpu; then
			if [ "$rc" = 10 ]; then
				warn "An NVIDIA GPU is present but no driver is loaded."
			else
				warn "The NVIDIA driver is too old for CUDA 12.1."
			fi
			if [ "$UPDATE_DRIVER" = 1 ] || confirm "Install/upgrade the NVIDIA driver now?"; then
				install_nvidia_driver
			fi
			warn "Continuing with the CPU build; re-run with --update-driver later."
		fi
		DEVICE=cpu ;;
	*)
		die "environment check failed (exit $rc)" ;;
	esac
}

# ---- python environment --------------------------------------------------

make_venv() {
	local py="$1"
	if [ ! -x "$VENV/bin/python" ]; then
		log "Creating virtualenv with $py"
		"$py" -m venv "$VENV"
	fi
	"$VENV/bin/python" -m pip install --quiet --upgrade pip wheel setuptools
}

install_torch() {
	local index="https://download.pytorch.org/whl/cpu"
	[ "$DEVICE" = cuda ] && index="https://download.pytorch.org/whl/cu121"
	log "Installing torch ($DEVICE) from $index"
	# shellcheck disable=SC2086
	"$VENV/bin/python" -m pip install $TORCH_PKGS --index-url "$index"
}

install_python_deps() {
	log "Installing seed-vc and web server dependencies"
	"$VENV/bin/python" -m pip install -r requirements-seedvc.txt \
		-r requirements-web.txt
}

verify() {
	log "Verifying"
	local rc=0
	"$VENV/bin/python" tools/check_env.py --torch --cpu-ok --ignore-python || rc=$?
	if [ "$rc" = 12 ]; then
		die "torch cannot see the GPU although the driver looks fine; try a reboot or ./install.sh --cpu"
	fi
	"$VENV/bin/python" -c 'import fastapi, uvicorn, librosa, transformers; print("python deps ok")'
}

download_models() {
	log "Pre-downloading models (several GB, resumable)"
	"$VENV/bin/python" tools/download_models.py
}

main() {
	parse_args "$@"
	command -v sudo >/dev/null || warn "sudo not found; system package steps may fail"
	install_system_deps
	local py
	py="$(find_python)" || die "python 3.10 or 3.11 not found"
	log "Using $py ($("$py" --version))"
	vendor_seedvc
	decide_device "$py"
	make_venv "$py"
	install_torch
	install_python_deps
	verify
	[ "$DOWNLOAD_MODELS" = 1 ] && download_models
	chmod +x run.sh update.sh 2>/dev/null || true
	echo
	log "Done. Start the web UI with:  ./run.sh   (then open http://localhost:7870)"
	[ "$DEVICE" = cpu ] && warn "Running on CPU: conversions will take minutes, not seconds."
	return 0
}

main "$@"
