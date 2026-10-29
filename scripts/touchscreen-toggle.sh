#!/usr/bin/env bash
# Enable/disable ONLY the damaged touch digitizer on the UX360CA.
#
# The digitizer is HID 0018:2808:5112 (FTSC1000:00). The touchpad is a
# different device (0018:04F3:301A) on the same i2c bus and is never touched
# by this script. Matching is by VID/PID on the sysfs path, never by event
# node number, because node numbers move between boots.
#
# Usage:
#   touchscreen-toggle.sh status
#   sudo touchscreen-toggle.sh off      # stock "disable touchscreen" behaviour
#   sudo touchscreen-toggle.sh on       # back to normal
#
# Exit codes: 0 ok, 1 device not found, 2 bad usage, 3 driver unavailable.

set -euo pipefail

readonly VID="2808"
readonly PID="5112"
readonly HIDROOT="/sys/bus/hid/devices"
readonly DRIVERROOT="/sys/bus/hid/drivers"

usage() {
    cat <<'USAGE'
Enable/disable ONLY the damaged touch digitizer on the UX360CA.

The digitizer is HID 0018:2808:5112 (FTSC1000:00). The touchpad is a different
device (0018:04F3:301A) on the same i2c bus and is never touched by this
script. Matching is by VID/PID on the sysfs path, never by event node number,
because node numbers move between boots.

Usage:
  touchscreen-toggle.sh status
  sudo touchscreen-toggle.sh off      # disable the touchscreen (stock behaviour)
  sudo touchscreen-toggle.sh on       # back to normal

Exit codes: 0 ok, 1 device not found, 2 bad usage, 3 driver unavailable.
USAGE
}

find_device() {
    local d name
    for d in "$HIDROOT"/*; do
        [[ -e "$d" ]] || continue
        name=$(basename "$d")
        [[ "$name" =~ ^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}\.[0-9]+$ ]] || continue
        if [[ "${name:5:4}" == "$VID" && "${name:10:4}" == "$PID" ]]; then
            printf '%s\n' "$d"
            return 0
        fi
    done
    return 1
}

current_driver() {
    local dev="$1"
    [[ -e "$dev/driver" ]] || return 1
    basename "$(readlink -f "$dev/driver")"
}

# Print the event node and hidraw node that belong to this HID device, so the
# operator can see what is (not) happening. Read-only.
show_nodes() {
    local dev="$1" ev hw real
    # The bus directory entry is a symlink; find must walk the real path.
    real=$(readlink -f "$dev")
    # sysfs is full of symlink loops, so glob the known layout instead of
    # walking with find: <hid-device>/input/inputN/eventN.
    ev=""
    for e in "$real"/input/input*/event*; do
        [[ -e "$e" ]] && { ev="$e"; break; }
    done
    hw=$(for h in /sys/class/hidraw/hidraw*; do
             [[ -e "$h" ]] || continue
             if [[ "$(readlink -f "$h/device")" == "$real" ]]; then
                 basename "$h"; break
             fi
         done)
    if [[ -n "$ev" ]]; then
        printf '  event node: /dev/input/%s\n' "$(basename "$ev")"
    else
        printf '  event node: <none>\n'
    fi
    printf '  hidraw    : %s\n' "${hw:+"/dev/$hw"}"
}

cmd_status() {
    local dev drv
    if ! dev=$(find_device); then
        echo "device ${VID}:${PID} not present" >&2
        return 1
    fi
    echo "device: $(basename "$dev")"
    if drv=$(current_driver "$dev"); then
        echo "state : attached to ${drv}"
    else
        echo "state : detached (touchscreen disabled)"
    fi
    show_nodes "$dev"
}

cmd_off() {
    local dev drv name
    dev=$(find_device) || { echo "device ${VID}:${PID} not present" >&2; return 1; }
    if ! drv=$(current_driver "$dev"); then
        echo "already detached"; return 0
    fi
    name=$(basename "$dev")
    printf '%s\n' "$name" > "$DRIVERROOT/$drv/unbind"
    echo "unbound ${name} from ${drv}"
}

cmd_on() {
    local dev name candidate
    dev=$(find_device) || { echo "device ${VID}:${PID} not present" >&2; return 1; }
    if current_driver "$dev" >/dev/null; then
        echo "already attached"; return 0
    fi
    name=$(basename "$dev")
    for candidate in hid-multitouch hid-generic; do
        if [[ -e "$DRIVERROOT/$candidate" ]]; then
            printf '%s\n' "$name" > "$DRIVERROOT/$candidate/bind"
            echo "bound ${name} to ${candidate}"
            return 0
        fi
    done
    echo "no usable driver found in ${DRIVERROOT}" >&2
    return 3
}

[[ $# -eq 1 ]] || { usage >&2; exit 2; }

case "$1" in
    status) cmd_status ;;
    off)    cmd_off ;;
    on)     cmd_on ;;
    -h|--help|help) usage ;;
    *)      usage >&2; exit 2 ;;
esac
