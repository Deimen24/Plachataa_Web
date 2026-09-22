#!/usr/bin/env bash
#
# Manage the Plachataa Web systemd service (starts at boot, restarts on
# failure, listens on all interfaces for a reverse proxy on another
# machine, opens the port in ufw/firewalld when present).
#
#   ./service.sh install      create + enable + start the service
#   ./service.sh uninstall    stop + disable + remove it
#   ./service.sh start|stop|restart|status|logs
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="plachataa-web"
UNIT="/etc/systemd/system/${NAME}.service"
RUN_USER="${SUDO_USER:-$(id -un)}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

need_systemd() {
	command -v systemctl >/dev/null || die "systemd not found; start ./run.sh --listen from your own init instead"
	[ -d /run/systemd/system ] || die "systemd is not running (WSL without systemd?)"
}

port_from_env() {
	local port=""
	[ -f "$ROOT/.env" ] && port="$(sed -n 's/^PLACHATAA_PORT=\([0-9]*\).*/\1/p' "$ROOT/.env" | tail -1)"
	echo "${port:-7870}"
}

open_firewall() {
	local port="$1"
	if command -v ufw >/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
		log "Opening port $port/tcp in ufw"
		sudo ufw allow "$port/tcp" >/dev/null
	elif command -v firewall-cmd >/dev/null && sudo firewall-cmd --state >/dev/null 2>&1; then
		log "Opening port $port/tcp in firewalld"
		sudo firewall-cmd --permanent --add-port="$port/tcp" >/dev/null
		sudo firewall-cmd --reload >/dev/null
	fi
}

close_firewall() {
	local port="$1"
	if command -v ufw >/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
		sudo ufw delete allow "$port/tcp" >/dev/null 2>&1 || true
	elif command -v firewall-cmd >/dev/null && sudo firewall-cmd --state >/dev/null 2>&1; then
		sudo firewall-cmd --permanent --remove-port="$port/tcp" >/dev/null 2>&1 || true
		sudo firewall-cmd --reload >/dev/null 2>&1 || true
	fi
}

ensure_env_file() {
	if [ ! -f "$ROOT/.env" ]; then
		log "Creating .env from .env.example"
		cp "$ROOT/.env.example" "$ROOT/.env"
	fi
}

write_unit() {
	log "Writing $UNIT (runs as $RUN_USER)"
	sudo tee "$UNIT" >/dev/null <<UNIT_EOF
[Unit]
Description=Plachataa Web - Seed-VC voice conversion
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$ROOT
EnvironmentFile=-$ROOT/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$ROOT/.venv/bin/python -m server.main --listen --log-file data/logs/server.log
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
# Model loading can take a while on first start.
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
UNIT_EOF
}

cmd_install() {
	need_systemd
	[ -x "$ROOT/.venv/bin/python" ] || die "no .venv; run ./install.sh first"
	ensure_env_file
	write_unit
	sudo systemctl daemon-reload
	sudo systemctl enable --now "$NAME"
	open_firewall "$(port_from_env)"
	sleep 2
	sudo systemctl --no-pager --lines=5 status "$NAME" || true
	log "Service installed. It listens on 0.0.0.0:$(port_from_env) for your reverse proxy."
	log "Logs: ./service.sh logs   (also data/logs/server.log)"
}

cmd_uninstall() {
	need_systemd
	if [ -f "$UNIT" ]; then
		log "Removing service $NAME"
		sudo systemctl disable --now "$NAME" >/dev/null 2>&1 || true
		sudo rm -f "$UNIT"
		sudo systemctl daemon-reload
	else
		log "Service not installed"
	fi
	close_firewall "$(port_from_env)"
}

main() {
	case "${1:-}" in
	install) cmd_install ;;
	uninstall) cmd_uninstall ;;
	start|stop|restart) need_systemd; sudo systemctl "$1" "$NAME" ;;
	status) need_systemd; systemctl --no-pager status "$NAME" ;;
	logs) need_systemd; journalctl -u "$NAME" -f -n 100 ;;
	*) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
	esac
}

main "$@"
