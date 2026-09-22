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
#   ./install.sh --no-service    do not install the systemd service that
#                                starts the server at boot
#   ./install.sh --proxy-ip IP   reverse-proxy address whose forwarded
#                                headers are trusted (default: any)
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
INSTALL_SERVICE=1
PROXY_IP="*"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

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
		--no-service) INSTALL_SERVICE=0 ;;
		--proxy-ip) shift; PROXY_IP="${1:-*}" ;;
		-h|--help) usage ;;
		*) die "unknown option: $1" ;;
		esac
		shift
	done
}

find_python() {
	local cand
	export PATH="$HOME/.local/bin:$PATH"
	if command -v uv >/dev/null; then
		cand="$(uv python find 3.11 2>/dev/null || true)"
		if [ -n "$cand" ] && [ -x "$cand" ]; then
			echo "$cand"
			return 0
		fi
	fi
	for cand in python3.10 python3.11 python3; do
		if command -v "$cand" >/dev/null 2>&1 &&
		   "$cand" -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,11) else 1)' 2>/dev/null; then
			echo "$cand"
			return 0
		fi
	done
	return 1
}

refuse_root() {
	# The script calls sudo itself where needed.  Running it entirely as
	# root makes .venv and data/ root-owned, and the service (which runs
	# as the normal user) could not write to them.
	if [ "$(id -u)" = 0 ] && [ -n "${SUDO_USER:-}" ]; then
		die "run ./install.sh as your normal user, not with sudo (it asks for sudo when needed)"
	fi
}

# Python 3.10/3.11 is not packaged on rolling distros (Arch/CachyOS ship
# 3.13+).  uv can fetch a self-contained 3.11 build for any distro.
ensure_uv() {
	if command -v uv >/dev/null; then
		return 0
	fi
	log "Installing uv to fetch Python 3.11"
	if [ "$(pkg_manager)" = pacman ]; then
		sudo pacman -S --needed --noconfirm uv || \
			warn "pacman could not install uv (mirror or keyring problem); using uv's own installer"
	fi
	if ! command -v uv >/dev/null; then
		curl -LsSf https://astral.sh/uv/install.sh | \
			env UV_INSTALL_DIR="$HOME/.local/bin" sh
		export PATH="$HOME/.local/bin:$PATH"
	fi
	command -v uv >/dev/null || die "uv installation failed; install uv or python 3.11 manually and re-run"
}

python_via_uv() {
	ensure_uv
	log "Fetching Python 3.11 with uv (managed, does not touch the system python)"
	uv python install 3.11 >&2
	uv python find 3.11
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
		sudo pacman -S --needed --noconfirm git ffmpeg libsndfile uv
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

write_env() {
	[ -f "$ROOT/.env" ] && return 0
	log "Creating .env"
	cp "$ROOT/.env.example" "$ROOT/.env"
	printf '\nPLACHATAA_FORWARDED_ALLOW_IPS=%s\n' "$PROXY_IP" >> "$ROOT/.env"
}

install_service() {
	[ "$INSTALL_SERVICE" = 1 ] || return 0
	if [ ! -d /run/systemd/system ]; then
		warn "systemd not running; skipping the boot service (use ./run.sh --listen)"
		return 0
	fi
	./service.sh install
}

main() {
	parse_args "$@"
	refuse_root
	command -v sudo >/dev/null || warn "sudo not found; system package steps may fail"
	install_system_deps
	local py
	if ! py="$(find_python)"; then
		warn "python 3.10/3.11 not found on the system (found: $(python3 --version 2>/dev/null || echo none))"
		py="$(python_via_uv)"
	fi
	log "Using $py ($("$py" --version))"
	vendor_seedvc
	decide_device "$py"
	make_venv "$py"
	install_torch
	install_python_deps
	verify
	[ "$DOWNLOAD_MODELS" = 1 ] && download_models
	chmod +x run.sh update.sh service.sh uninstall.sh 2>/dev/null || true
	write_env
	install_service
	echo
	if [ "$INSTALL_SERVICE" = 1 ]; then
		log "Done. The server runs at boot; manage it with ./service.sh (status|logs|restart)."
	else
		log "Done. Start the web UI with:  ./run.sh   (then open http://localhost:7870)"
	fi
	[ "$DEVICE" = cpu ] && warn "Running on CPU: conversions will take minutes, not seconds."
	return 0
}

main "$@"
