# Semi private repo to fix random phantom touches on my ASUS UX360CA laptop

The touch digitizer on this UX360CA is damaged: some regions fire phantom
contacts, others are dead. The panel is not being replaced, so this repo
removes the phantoms in software and tracks the damage over time.

- **[docs/hardware.md](docs/hardware.md)** — verified device profile:
  `FTSC1000:00` / HID `2808:5112` / `hid-multitouch` / `/dev/input/event4`,
  report layout (4 slots, X 0-3200, Y 0-1800, Width/Height, ScanTime,
  ContactCount — **no pressure axis**), and what the platform supports.
- **[docs/plan.md](docs/plan.md)** — the plan: capture, characterise, ship a
  userspace filter first, then promote to HID-BPF only if needed, and keep
  monitoring.
- **[docs/recording-protocol.md](docs/recording-protocol.md)** — the
  phantom-only pilot: the screen is not touched during a session, so every
  contact is a phantom; accidental touches are reconciled by wall-clock time.
- **[docs/guide.md](docs/guide.md)** — tiered guide (30 s / 2 min / 5 min /
  15 min / 1 h).

Nothing here filters touches yet. The P0 access layer, the capture rig and the
phantom-only recording protocol are in place; characterisation needs a corpus.

## P0 — run once (needs root)

```sh
sudo scripts/install-access.sh        # scoped udev ACL on hidraw0 + event4 only
sudo dnf install -y evtest python3-evdev clang bpftool hidrd
scripts/doctor.sh                     # verify as your normal user
```

`scripts/touchscreen-toggle.sh status|on|off` enables/disables the digitizer
only (HID `0018:2808:5112`, matched by VID/PID on the sysfs path — never by
event node number, and never the touchpad). `sudo scripts/install-access.sh
--remove` undoes the ACL.

The scripts change nothing about how the touchscreen behaves: they only grant
the seat user read access to the two nodes the capture tools need.
