# Guide — touchscreen-driver-fix

Tiered guide (G10): each tier is self-contained. Facts live in
[hardware.md](hardware.md); the roadmap in [plan.md](plan.md).

## 30 seconds — the pitch

Rahul's ASUS UX360CA convertible has a cracked touch digitizer that fires
phantom touches in some regions and ignores others. The panel is not being
replaced, so this repo removes the phantoms in software: capture raw HID
frames, learn where the damage is, and filter those contacts in userspace — a
`uinput`/evdev daemon that sits before `hid-multitouch` turns them into pointer
events. HID-BPF remains the promotion path if the userspace layer is not
enough. Every rule is measured against recorded corpora; nothing is tuned by
feel.

## 2 minutes — the presentation (six beats)

1. **Problem**: physical damage, two symptoms — ghost contacts and dead zones.
   Dead zones cannot be repaired; ghosts can be removed.
2. **Constraint**: the controller (`FTSC1000:00`, HID `2808:5112`, hid-multitouch)
   reports no pressure and no confidence bit. Only geometry, position, timing
   and contact lifecycle are available.
3. **Layer**: ship a userspace evdev/uinput filter first (`systemd --user`,
   shadow mode for measurement, fail-open, `phantomctl off` to release); only
   promote to HID-BPF if latency or robustness demands it.
4. **Method**: capture raw frames (`hidraw0`) + decoded events (`event4`) →
   activation heatmap → hotspot table → declarative rules → replay-tested.
5. **Evidence**: suppression and false-rejection are scored on stored corpora
   (idle capture, target drill), plus parity between the Python prototype and
   the BPF program.
6. **Safety**: target by VID/PID, fail open, one filter at a time, no writes to
   the controller, `phantomctl off` restores stock behaviour. Settled rulings:
   userspace filter ships first (HID-BPF only if needed), dead regions are not
   recovered, capture access comes from a scoped udev ACL, and labelling uses
   phantom-only sessions (no touching while recording).

## 5 minutes — the intro

```
  damaged panel
       |
  i2c-0 -> i2c_hid -> /dev/hidraw0 (raw frames)  <-- capture / filter here
                    -> hid-multitouch -> /dev/input/event4 -> libinput -> KWin
                              ^                          ^
                              |                          |
                 userspace filter (ship first)   HID-BPF (promotion path)
```

The product map:

| Area | Where | What it does |
|---|---|---|
| Facts | `docs/hardware.md` | device identity, report layout, platform capabilities |
| Roadmap | `docs/plan.md` | phases P0-P6, goals, risks, open decisions |
| Capture | `tools/capture.py` | hidraw + evdev recorder with context markers |
| Analysis | `tools/analyze.py` | heatmap, hotspots, geometry distributions |
| Ground truth | `tools/target_drill.py` | tap-the-grid drill (dead zones + real-touch stats) |
| Replay | `tools/replay.py` | corpus -> decisions, offline, deterministic |
| Logic | `phantom/decide.py` | pure scoring + hysteresis, shared by all harnesses |
| Harness | `phantom/daemon.py` | EVIOCGRAB + uinput proxy, shadow mode |
| Product | `bpf/ftsc1000_filter.bpf.c` | same rules at the HID device, auto-attached |
| Ops | `scripts/`, `phantomctl` | toggle, access setup, status, drift |

## 15 minutes — the deep dive

**Why not libinput or KDE settings?** They expose an affine calibration matrix;
it can shift/stretch the whole axis but cannot carve out interior ghost
regions. Only useful if the damage is a thin edge strip.

**Why not patch `hid-multitouch`?** It is a generic upstream driver shared by
hundreds of devices. A local patch means carrying a fork, rebuilding per
kernel (akmods/dkms), and re-validating on every update. HID-BPF sits at the
same decision point with none of that cost: the program is loaded by
`udev-hid-bpf` from an hwdb match on `b0018g0001v00002808p00005112`, runs in
the `.input_report` hook with `INSERT_HEAD` *before* hid-multitouch parses the
frame, and can rewrite the report buffer.

**Why keep a userspace proxy at all?** Iteration speed and observability. It
can log every decision for every frame with no effect (`--shadow`), which is how
the thresholds get chosen. It is a development harness only: once the BPF
program is attached the proxy is switched off and no daemon ships. The C/BPF
version is parity-tested against the same decision tables on the same recorded
corpus.

**The rule model** (all data-driven, one `config/filter.toml`):

- *ghost rectangles* — grid cells whose idle activation rate exceeds a
  threshold, derived by `analyze.py`, never hand-drawn;
- *geometry gate* — Width/Height outside the real-finger distribution for that
  screen region;
- *lifecycle gate* — single-scan contacts, impossible ContactCount, ContactID
  reuse within a scan;
- *repeat gate* — the same coordinate recurring across seconds with tiny
  variance;
- *topology gate* — footprint straddling a dead/ghost boundary;
- *typing guard* (optional) — short suppression window after keyboard input.

Decision = weighted vote with per-ContactID hysteresis. Hysteresis is what
keeps a real finger alive once accepted, even if it wanders into a ghost cell.

**Reading a recorded run.** `python3 tools/analyze.py --latest` prints every
contact with raw digitizer coordinates and 1920x1080 pixel coordinates, groups
them into sessions by a 2 s gap, and renders a text density map. Scaling uses
the descriptor's 3200x1800 logical maxima (uniform 0.6), never the 0x0fff
sentinel. Use `--csv`/`--json` to hand a run to replay or to a test fixture.

**Measurement design.** Two corpora, both replayable: `idle` (nobody touching —
every contact is a phantom, gives the suppression number) and `drill` (labelled
real taps on a grid — gives false rejection and the dead-zone map). Every rule
change is replayed before it is enabled; the drill is rerun after enabling.

**Failure modes to keep in mind.** Frame mutation can generate spurious
release/acquire pairs, so ghosts must be rejected on their first frame rather
than deleted later. Overlapping ghost/live regions must degrade gracefully
(score, not hard mask). A stale map silently eats real touches, hence the drift
report and the "never auto-apply a retune" rule.

## 1 hour — the functional tour

1. Read `docs/hardware.md` and re-run its `Re-verify` block; note the two
   devices on `i2c-0` and why only one of them is ever touched.
2. Read `docs/plan.md` §3 (layer choice) and §4 (signals); for each signal,
   say which corpus can disprove it.
3. Run `scripts/touchscreen-toggle.sh off` then `on`; confirm the touchpad is
   unaffected both times.
4. Capture 5 idle minutes; run `analyze.py`; read the hotspot table and explain
   every hotspot by its geometry fingerprint.
5. Run `target_drill.py`; compare the dead-zone map to the ghost map; note where
   they are adjacent and where they overlap.
6. Replay the corpus in shadow mode; open the per-decision log; find one phantom
   accepted and one real touch rejected; trace the vote.
7. Add a corpus file as a test; break a threshold; watch the test fail; restore.
8. Build the BPF object, `udev-hid-bpf inspect` it, install it, reboot, and
   verify with `udev-hid-bpf list-bpf-programs` + `phantomctl status`.
9. Run the parity diff between proxy and BPF decisions on the same corpus.
10. Walk the drift report in `analysis/history/` and explain what would trigger
    a retune proposal.
