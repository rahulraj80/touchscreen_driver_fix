#!/usr/bin/env bash
# Read-only health check for the touchscreen-driver-fix plan.
#
#   scripts/doctor.sh            # human readable
#   scripts/doctor.sh --quiet    # only problems
#
# Checks that the device is where the plan thinks it is, that capture access
# works for the current user, and that the HID-BPF carrier is available.
# Changes nothing. Exit 0 = all required checks pass, 1 = a required check
# failed, 2 = device not present.

set -uo pipefail

readonly VID="2808"
readonly PID="5112"
readonly TPAD_VID="04F3"
readonly TPAD_PID="301A"
readonly QUIET="${1:-}"

fails=0
warns=0

pass() { [[ "$QUIET" == "--quiet" ]] || printf 'PASS  %s\n' "$*"; }
warn() { warns=$((warns + 1)); printf 'WARN  %s\n' "$*"; }
fail() { fails=$((fails + 1)); printf 'FAIL  %s\n' "$*"; }
info() { [[ "$QUIET" == "--quiet" ]] || printf '      %s\n' "$*"; }
head_() { [[ "$QUIET" == "--quiet" ]] || printf '\n== %s ==\n' "$*"; }

find_hid() {
    local vid="$1" pid="$2" d name
    for d in /sys/bus/hid/devices/*; do
        [[ -e "$d" ]] || continue
        name=$(basename "$d")
        [[ "$name" =~ ^[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}\.[0-9]+$ ]] || continue
        if [[ "${name:5:4}" == "$vid" && "${name:10:4}" == "$pid" ]]; then
            printf '%s\n' "$d"; return 0
        fi
    done
    return 1
}

head_ "device"
if ! DEV=$(find_hid "$VID" "$PID"); then
    echo "FAIL  HID device ${VID}:${PID} not present (is the digitizer disabled?)"
    exit 2
fi
pass "HID device $(basename "$DEV")"
info "sysfs: ${DEV}"

if [[ -e "$DEV/driver" ]]; then
    DRV=$(basename "$(readlink -f "$DEV/driver")")
    pass "bound to ${DRV}"
    [[ "$DRV" == "hid-multitouch" ]] || warn "driver is ${DRV}, plan assumes hid-multitouch"
else
    warn "device is detached (touchscreen currently disabled)"
fi

# The bus directory entry is a symlink; resolve it, then glob the known
# layout (sysfs symlink loops make `find -L` unusable here).
DEV_REAL=$(readlink -f "$DEV")
EV=""
for e in "$DEV_REAL"/input/input*/event*; do
    [[ -e "$e" ]] && { EV="$e"; break; }
done
if [[ -n "$EV" ]]; then
    EVD="/dev/input/$(basename "$EV")"
    pass "event node ${EVD}"
    info "name: $(cat "$(dirname "$EV")/name" 2>/dev/null || echo '?')"
else
    EVD=""
    fail "no event node under ${DEV_REAL}"
fi

HW=""
for h in /sys/class/hidraw/hidraw*; do
    [[ -e "$h" ]] || continue
    if [[ "$(readlink -f "$h/device")" == "$(readlink -f "$DEV")" ]]; then
        HW="/dev/$(basename "$h")"; break
    fi
done
if [[ -n "$HW" ]]; then
    pass "hidraw node ${HW}"
else
    fail "no hidraw node for this device (CONFIG_HIDRAW?)"
fi

head_ "capture access (as $(id -un))"
for n in "$HW" "$EVD"; do
    [[ -n "$n" && -e "$n" ]] || continue
    if [[ -r "$n" ]]; then
        pass "readable: ${n}"
    else
        fail "not readable: ${n} — run: sudo scripts/install-access.sh"
    fi
    acl=$(getfacl -p "$n" 2>/dev/null | sed -n 's/^user:\([^:]*\):.*/\1/p' | grep -v '^root$' | paste -sd, -)
    info "acl users on ${n}: ${acl:-none}"
done

head_ "separation from the touchpad"
if TPAD=$(find_hid "$TPAD_VID" "$TPAD_PID"); then
    pass "touchpad is a separate HID device $(basename "$TPAD")"
    info "it must never be grabbed, filtered or ACL-widened by this project"
else
    warn "touchpad ${TPAD_VID}:${TPAD_PID} not found (undocked/disabled?)"
fi

head_ "filter carrier (HID-BPF)"
CFG="/boot/config-$(uname -r)"
if [[ -r "$CFG" ]]; then
    for opt in CONFIG_HID_BPF CONFIG_DEBUG_INFO_BTF CONFIG_HIDRAW; do
        if grep -q "^${opt}=y" "$CFG"; then pass "${opt}=y"; else fail "${opt} not =y"; fi
    done
    grep -q "^CONFIG_UHID=" "$CFG" && pass "CONFIG_UHID present" || warn "CONFIG_UHID absent (proxy harness needs it)"
else
    warn "no ${CFG} — cannot verify kernel options"
fi

[[ -e /sys/kernel/btf/hid_multitouch ]] && pass "module BTF for hid_multitouch" \
    || warn "no module BTF for hid_multitouch"
[[ -e /sys/kernel/btf/vmlinux ]] && pass "vmlinux BTF" || fail "no vmlinux BTF"
command -v udev-hid-bpf >/dev/null && pass "udev-hid-bpf $(udev-hid-bpf --version 2>/dev/null | head -1)" \
    || fail "udev-hid-bpf not installed"
[[ -f /usr/lib/udev/rules.d/81-hid-bpf.rules ]] && pass "hid-bpf udev rule present" \
    || warn "hid-bpf udev rule missing"

if command -v mokutil >/dev/null; then
    sb=$(mokutil --sb-state 2>&1 | head -1)
    case "$sb" in
        *"SecureBoot disabled"*) pass "secure boot disabled" ;;
        *"SecureBoot enabled"*) warn "secure boot enabled — HID-BPF needs signing" ;;
        *) info "secure boot: ${sb}" ;;
    esac
fi

head_ "tooling"
command -v evtest >/dev/null && pass "evtest" || warn "evtest missing (dnf install evtest)"
python3 -c 'import evdev' 2>/dev/null && pass "python3-evdev" || warn "python3-evdev missing (pip install evdev)"
command -v clang >/dev/null && pass "clang" || warn "clang missing (needed for the BPF object)"
command -v bpftool >/dev/null && pass "bpftool" || warn "bpftool missing (needed to inspect the BPF object)"

head_ "summary"
printf '      %d failed, %d warnings\n' "$fails" "$warns"
[[ "$fails" -eq 0 ]] || exit 1
