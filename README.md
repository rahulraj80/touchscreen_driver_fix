# Semi private repo to fix random phantom touches on my ASUS UX360CA laptop

The touch digitizer on this UX360CA is damaged: some regions fire phantom
contacts, others are dead. The panel is not being replaced, so this repo
removes the phantoms in software and tracks the damage over time.

- **[docs/hardware.md](docs/hardware.md)** — verified device profile:
  `FTSC1000:00` / HID `2808:5112` / `hid-multitouch` / `/dev/input/event4`,
  report layout (4 slots, X 0-3200, Y 0-1800, Width/Height, ScanTime,
  ContactCount — **no pressure axis**), and what the platform supports.
- **[docs/plan.md](docs/plan.md)** — the plan: capture, characterise,
  prototype in userspace, ship as a HID-BPF filter, then keep monitoring.
- **[docs/guide.md](docs/guide.md)** — tiered guide (30 s / 2 min / 5 min /
  15 min / 1 h).

Nothing here is implemented yet; P0 in the plan is the safety net and capture
access.
