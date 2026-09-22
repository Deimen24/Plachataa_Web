#!/usr/bin/env bash
#
# Update Plachataa Web in place:
#   * pull the latest version of this repository
#   * move the vendored seed-vc to the revision pinned in seedvc.lock
#   * upgrade Python dependencies
#   * re-check the NVIDIA driver (and upgrade it with --update-driver)
#
#   ./update.sh [--update-driver] [--torch] [--download-models] [--yes]
#
#   --torch   also reinstall torch (only needed if the pin in install.sh
#             changed)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
VENV="$ROOT/.venv"
VENDOR="$ROOT/vendor/seed-vc"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

INSTALL_ARGS=()
REINSTALL_TORCH=0
for arg in "$@"; do
	case "$arg" in
	--torch) REINSTALL_TORCH=1 ;;
	--update-driver|--download-models|--yes|-y|--cpu|--cuda)
		INSTALL_ARGS+=("$arg") ;;
	-h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
	*) die "unknown option: $arg" ;;
	esac
done

[ -x "$VENV/bin/python" ] || die "no .venv found; run ./install.sh first"

update_repo() {
	if [ -d "$ROOT/.git" ]; then
		log "Updating Plachataa Web"
		local branch
		branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
		if ! git -C "$ROOT" diff --quiet; then
			warn "local changes present; stashing them"
			git -C "$ROOT" stash push -q -m "update.sh $(date -Is)"
		fi
		git -C "$ROOT" pull --ff-only origin "$branch"
	else
		warn "not a git checkout; skipping self-update"
	fi
}

update_seedvc() {
	# shellcheck source=seedvc.lock
	source "$ROOT/seedvc.lock"
	log "Updating seed-vc to ${SEEDVC_COMMIT:0:10}"
	[ -d "$VENDOR/.git" ] || git clone --quiet "$SEEDVC_REPO" "$VENDOR"
	git -C "$VENDOR" fetch --quiet origin
	git -C "$VENDOR" checkout --quiet "$SEEDVC_COMMIT"
}

update_deps() {
	log "Upgrading Python dependencies"
	"$VENV/bin/python" -m pip install --quiet --upgrade pip
	"$VENV/bin/python" -m pip install --upgrade -r requirements-seedvc.txt \
		-r requirements-web.txt
}

main() {
	update_repo
	update_seedvc
	if [ "$REINSTALL_TORCH" = 1 ]; then
		log "Reinstalling torch via install.sh"
		./install.sh ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"}
		return
	fi
	update_deps
	log "Driver / environment check"
	local rc=0
	"$VENV/bin/python" tools/check_env.py --torch --cpu-ok --ignore-python || rc=$?
	if [ "$rc" != 0 ]; then
		warn "environment check returned $rc; running install.sh to repair"
		./install.sh ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"}
		return
	fi
	for a in ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"}; do
		if [ "$a" = "--update-driver" ]; then
			./install.sh ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"}
			return
		fi
		if [ "$a" = "--download-models" ]; then
			"$VENV/bin/python" tools/download_models.py
		fi
	done
	restart_service
	log "Update complete."
}

restart_service() {
	if [ -f /etc/systemd/system/plachataa-web.service ]; then
		log "Restarting service"
		./service.sh restart
	fi
}

main
