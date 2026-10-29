#!/usr/bin/env bash
# Start / stop / inspect phantom-touch capture without root.
#
# Assumes scripts/install-access.sh has been run once (sudo) so the seat user
# holds the udev "uaccess" ACL on the digitizer's hidraw + event nodes.
#
#   scripts/capture.sh start          # background capture, survives logout? no - user unit does
#   scripts/capture.sh stop
#   scripts/capture.sh status
#   scripts/capture.sh verify [run]   # validate the newest (or named) run
#   scripts/capture.sh mark "note"    # drop a marker into the running capture
#   scripts/capture.sh install        # enable the systemd --user unit (persistent)
#
# "start" is the quick manual way; "install" makes it survive reboots/logins.
set -euo pipefail

readonly REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly CORPUS="${REPO}/corpus"
readonly PIDFILE="${CORPUS}/.capture.pid"
readonly PY="${PYTHON:-/usr/bin/python3}"

newest_run() {
    # shellcheck disable=SC2012
    ls -1dt "${CORPUS}"/*/ 2>/dev/null | head -1 | sed 's:/$::'
}

case "${1:-}" in
start)
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "already running (pid $(cat "$PIDFILE"))"; exit 0
    fi
    mkdir -p "$CORPUS"
    "$PY" "${REPO}/tools/capture.py" run --label phantom --out "$CORPUS" \
        --heartbeat 5 --duration 0 >>"${CORPUS}/.capture.log" 2>&1 &
    echo $! >"$PIDFILE"
    echo "started pid $(cat "$PIDFILE"); log: ${CORPUS}/.capture.log"
    ;;
stop)
    if [[ -f "$PIDFILE" ]]; then
        pid="$(cat "$PIDFILE")"
        kill "$pid" 2>/dev/null || true
        rm -f "$PIDFILE"
        echo "stopped pid ${pid}"
    else
        echo "not running"
    fi
    ;;
status)
    "$PY" "${REPO}/tools/capture.py" status --out "$CORPUS" || true
    ;;
verify)
    "$PY" "${REPO}/tools/capture.py" verify "${2:-latest}" --out "$CORPUS"
    ;;
mark)
    run="$(newest_run)"
    [[ -n "$run" && -p "${run}/control" ]] || { echo "no live run with a control FIFO" >&2; exit 1; }
    printf '%s\n' "${2:-note}" >"${run}/control"
    echo "marker -> ${run}/control: ${2:-note}"
    ;;
install)
    mkdir -p "${HOME}/.config/systemd/user"
    ln -sf "${REPO}/systemd/user/phantom-touch-capture.service" \
        "${HOME}/.config/systemd/user/phantom-touch-capture.service"
    systemctl --user daemon-reload
    systemctl --user enable --now phantom-touch-capture.service
    echo "enabled systemd --user unit; check: systemctl --user status phantom-touch-capture"
    ;;
*)
    sed -n '2,16p' "$0"
    exit 2
    ;;
esac
