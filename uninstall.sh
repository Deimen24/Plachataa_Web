#!/usr/bin/env bash
#
# Remove Plachataa Web from this machine.
#
#   ./uninstall.sh              remove service, venv, seed-vc; ask about data/
#   ./uninstall.sh --keep-data  keep data/ (voices, outputs, model cache)
#   ./uninstall.sh --purge      delete data/ without asking
#   ./uninstall.sh --remove-dir also delete this whole folder afterwards
#   ./uninstall.sh --yes        never prompt
#
# Not touched: system packages (python, git, ffmpeg) and the NVIDIA driver.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

KEEP_DATA=""
REMOVE_DIR=0
ASSUME_YES=0

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

confirm() {
	[ "$ASSUME_YES" = 1 ] && return 0
	read -r -p "$1 [y/N] " ans
	[[ "$ans" =~ ^[Yy] ]]
}

for arg in "$@"; do
	case "$arg" in
	--keep-data) KEEP_DATA=1 ;;
	--purge) KEEP_DATA=0 ;;
	--remove-dir) REMOVE_DIR=1 ;;
	--yes|-y) ASSUME_YES=1 ;;
	-h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
	*) die "unknown option: $arg" ;;
	esac
done

stop_running() {
	# Kill a manually started server so files are not busy.
	pkill -f "python -m server.main" 2>/dev/null && log "Stopped running server" || true
}

remove_service() {
	if [ -f /etc/systemd/system/plachataa-web.service ]; then
		./service.sh uninstall
	fi
}

remove_dir() {
	local d="$1"
	if [ -e "$ROOT/$d" ]; then
		log "Removing $d"
		rm -rf "${ROOT:?}/$d"
	fi
}

remove_data() {
	[ -d "$ROOT/data" ] || return 0
	if [ -z "$KEEP_DATA" ]; then
		local size; size="$(du -sh "$ROOT/data" 2>/dev/null | cut -f1)"
		if confirm "Delete data/ ($size: saved voices, outputs, model cache)?"; then
			KEEP_DATA=0
		else
			KEEP_DATA=1
		fi
	fi
	if [ "$KEEP_DATA" = 0 ]; then
		remove_dir data
	else
		log "Keeping data/"
	fi
}

remove_self() {
	[ "$REMOVE_DIR" = 1 ] || return 0
	if [ "$KEEP_DATA" = 1 ] && [ -d "$ROOT/data" ]; then
		warn "--remove-dir ignored because data/ is kept"
		return 0
	fi
	confirm "Delete the folder $ROOT entirely?" || return 0
	cd /
	rm -rf "$ROOT"
	log "Removed $ROOT"
}

main() {
	log "Uninstalling Plachataa Web from $ROOT"
	remove_service
	stop_running
	remove_dir .venv
	remove_dir vendor
	remove_data
	find "$ROOT" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	remove_self
	log "Done. System packages and the NVIDIA driver were left in place."
}

main
