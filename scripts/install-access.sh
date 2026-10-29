#!/usr/bin/env bash
# Install the scoped udev ACL rule for touchscreen capture access.
#
# Run as root. Idempotent: re-running replaces the rule and reloads udev.
# It does NOT install packages and does NOT touch the touchpad.
#
#   sudo scripts/install-access.sh
#   scripts/doctor.sh                  # verify as your normal user
#   sudo scripts/install-access.sh --remove   # undo
#
# The rule is numbered 72- on purpose: systemd's 73-seat-late.rules is what
# actually applies the uaccess ACL, so the tag must be added before it runs.

set -euo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SRC="${HERE}/../udev/72-touchscreen-phantom-capture.rules"
readonly DST="/etc/udev/rules.d/72-touchscreen-phantom-capture.rules"
readonly OLD="/etc/udev/rules.d/90-touchscreen-phantom-capture.rules"

[[ $EUID -eq 0 ]] || { echo "must run as root (sudo $0)" >&2; exit 2; }

if [[ "${1:-}" == "--remove" ]]; then
    rm -f "$DST" "$OLD"
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=hidraw --subsystem-match=input
    echo "removed ${DST}${OLD:+ and ${OLD}} (ACL gone; log out and back in if it lingers)"
    exit 0
fi

[[ -f "$SRC" ]] || { echo "rule file missing: $SRC" >&2; exit 1; }

# Remove the older, broken 90- rule if a previous install left it behind.
rm -f "$OLD"
install -D -m 0644 "$SRC" "$DST"
udevadm control --reload-rules
udevadm trigger --action=change --subsystem-match=hidraw --subsystem-match=input
udevadm settle

echo "installed ${DST}"
echo
echo "udev tags (uaccess must be present):"
for n in /dev/hidraw0 /dev/input/event4; do
    [[ -e "$n" ]] || continue
    printf '  %-20s %s\n' "$n" "$(udevadm info "$n" 2>/dev/null | sed -n 's/^E: TAGS=//p')"
done
echo
echo "current ACLs:"
for n in /dev/hidraw* /dev/input/event*; do
    [[ -e "$n" ]] || continue
    if getfacl -p "$n" 2>/dev/null | grep -q '^user:[^:]'; then
        echo "  ${n}:"
        getfacl -p "$n" 2>/dev/null | sed -n 's/^user:\(.*\)/    \1/p'
    fi
done
echo
echo "if uaccess is missing, the rule did not match this device:"
echo "  udevadm info --attribute-walk /dev/hidraw0 | grep -E 'KERNELS|modalias'"
echo "next: run scripts/doctor.sh as your normal user"
