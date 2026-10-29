#!/usr/bin/env bash
# Install the scoped udev ACL rule for touchscreen capture access (P0).
#
# Run as root. Idempotent: re-running replaces the rule and reloads udev.
# It does NOT install packages and does NOT touch the touchpad.
#
#   sudo scripts/install-access.sh
#   scripts/doctor.sh          # verify as your normal user
#   sudo scripts/install-access.sh --remove   # undo

set -euo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SRC="${HERE}/../udev/90-touchscreen-phantom-capture.rules"
readonly DST="/etc/udev/rules.d/90-touchscreen-phantom-capture.rules"

[[ $EUID -eq 0 ]] || { echo "must run as root (sudo $0)" >&2; exit 2; }

if [[ "${1:-}" == "--remove" ]]; then
    rm -f "$DST"
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=hidraw --subsystem-match=input
    echo "removed ${DST} (ACL gone; log out and back in if it lingers)"
    exit 0
fi

[[ -f "$SRC" ]] || { echo "rule file missing: $SRC" >&2; exit 1; }

install -D -m 0644 "$SRC" "$DST"
udevadm control --reload-rules
udevadm trigger --subsystem-match=hidraw --subsystem-match=input
sleep 1

echo "installed ${DST}"
echo
echo "current ACLs on the digitizer nodes:"
for n in /dev/hidraw* /dev/input/event*; do
    [[ -e "$n" ]] || continue
    if getfacl -p "$n" 2>/dev/null | grep -q 'user:'; then
        echo "  ${n}:"
        getfacl -p "$n" 2>/dev/null | sed -n 's/^user:\(.*\)/    \1/p'
    fi
done
echo
echo "next: run scripts/doctor.sh as your normal user"
