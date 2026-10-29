# Plan — phantom-touch filter for the UX360CA touchscreen

Status: **draft for review** (2026-10-29). Hardware facts: [hardware.md](hardware.md).

The digitizer is physically damaged: some regions inject contacts nobody asked
for, other regions are dead. The panel will not be replaced, so the fix is a
**software filter that removes phantom contacts while costing as few real
touches as possible** — plus instrumentation that keeps tracking the damage as
it spreads.

## 1. Goal and success criteria

| # | Goal | Measure |
|---|---|---|
| G1 | Phantom touches stop reaching applications | ≥ 95 % suppression on an idle capture corpus (no human touching) |
| G2 | Real touches still work, including near damage | ≤ 2 % false rejection on a labelled "target drill" corpus; per-target hit rate must not regress in live areas |
| G3 | Nothing else on the machine changes | touchpad, pen, keyboard, other HID devices byte-identical behaviour |
| G4 | The fix survives reboots and kernel upgrades unattended | filter attaches at device bind; no manual step; fail-open if it cannot load |
| G5 | Damage is tracked over time | ghost-map snapshots + drift report; new hotspots flagged, never silently masked |
| G6 | Reversible in one action | `phantomctl off` / rebind restores stock behaviour in seconds |

Non-goals: repairing dead zones (impossible in software — at most a cosmetic
nearest-live-neighbour snap), firmware upgrades on the controller (brick risk),
replacing the panel.

## 2. What we already know (see hardware.md)

- `FTSC1000:00 2808:5112` on `i2c-0` -> `i2c_hid` -> `hid-multitouch` ->
  `/dev/input/event4`, raw reports on `/dev/hidraw0`.
- 4 slots: TipSwitch, ContactID, X (0..3200), Y (0..1800), Width, Height,
  plus per-frame ScanTime and ContactCount.
- **No pressure, no confidence bit.** Geometry + position + timing +
  lifecycle are the only discriminators. Everything below is built around that.
- HID-BPF is available and Fedora already ships the auto-attach machinery
  (`udev-hid-bpf`, udev rule, hwdb namespace). Secure Boot is off, uinput is
  available. KDE runs Wayland/libinput.

## 3. Where the filter can live

| Layer | Effort | Iteration | Survives updates | Notes |
|---|---|---|---|---|
| **A. libinput / KDE settings** | minutes | n/a | yes | Only an affine calibration matrix (stretch/shift axes). Cannot remove interior ghosts. Useful *only* if the damage is an edge strip. |
| **B. Userspace evdev proxy** (EVIOCGRAB + uinput) | low | seconds, no reboot | yes (systemd unit) | Full flexibility, testable offline, but adds a daemon and a second input device that libinput must classify; extra latency; dies => must fail open. |
| **C. HID-BPF at the HID device** | medium | reload in seconds, no reboot | yes, and across kernel upgrades (BTF-portable) | Filters *before* `hid-multitouch` allocates MT slots: the cleanest place, no fork of a generic driver, no module signing, auto-attach via udev. **Recommended carrier.** |
| **D. Patch `hid-multitouch`** | high | rebuild + reload + reboot | no (akmods/dkms) | Needed only if C cannot express a rule (e.g. MT-slot semantics changes). Carries a generic-driver fork forever. |

**Decision: build the logic once, ship it as C.** Phase B is the development
harness (fast, offline-replayable, gives per-decision logs); Phase C is the
product. Both consume the *same* declarative rule table so behaviour can be
verified identical on a recorded corpus.

HID-BPF facts confirmed on this box: `CONFIG_HID_BPF=y`, `hid_bpf_ops`
struct_ops in BTF, `/sys/kernel/btf/hid_multitouch` module BTF,
`udev-hid-bpf 2.2.0` installed with the `hid-bpf:` hwdb namespace. The
`.input_report` hook with `INSERT_HEAD` runs before hid-multitouch parses the
frame, and the report buffer is writable (`hid_bpf_get_data`).

## 4. Signals the filter can use (no pressure axis)

| Signal | Rationale | Confidence |
|---|---|---|
| **Spatial ghost map** | Damaged ITO nodes fire at fixed coordinates. Learned from idle captures as a per-cell activation rate; suppress contacts whose area lies inside a hot cell. | high |
| **Contact geometry** | Real fingertip Width/Height cluster in a narrow band; broken nodes often report degenerate or saturated values (0, 255, or far off the human distribution for that spot). | high |
| **Repeat-position variance** | Ghosts recur at nearly identical coordinates; real fingers never repeat to the same LSB. Very strong over a few seconds. | high |
| **Lifecycle** | Contacts that exist for a single scan, or that (dis)appear without neighbours, are suspicious. Real taps are ≥ 3-5 scans at the measured report rate. | medium |
| **Count consistency** | `ContactCount` vs number of active slots; impossible jumps in contact count within one scan. | medium |
| **Motion plausibility** | Same ContactID teleporting > N mm between scans; or two contacts occupying the same coordinates. | medium |
| **Dead-zone adjacency** | A contact whose footprint straddles the boundary of a known dead region and a known ghost region is almost certainly an artefact of the same damage. | high |
| **Typing guard** | Suppress everything for ~80 ms after a keyboard event (classic "palm on damaged strip while typing"). Optional, off by default — it is a mitigation, not a diagnosis. | medium |

Rules are expressed as data (rectangles + thresholds + per-rule score), combined
by a small voting/scoring function with a hysteresis state machine per
ContactID, so the same table drives the Python prototype, the replay tests and
the BPF program.

## 5. Phases

### P0 — Safety net and access (½ day)

- `scripts/touchscreen-toggle.sh on|off` (bind/unbind by *sysfs path*, never by
  node name) + a KDE-friendly hotkey. Always able to go back to stock.
- Unprivileged capture: small root-run setup that adds `rahul` to `input` and
  installs a udev rule for `/dev/hidraw0` (`TAG+="uaccess"` or group `input`).
- Install `evtest`, `python3-evdev`, `hidrd`, `clang`, `bpftool`.
- Record a 10-minute idle baseline and a first "what does it look like" report.

Exit: can disable/enable the digitizer without reboot; capture runs as `rahul`;
first phantom frames captured.

### P1 — Capture rig (1 day)

- `tools/capture.py` — dual-source recorder:
  - `/dev/hidraw0`: every raw report frame (hex + monotonic ns) — the ground truth;
  - `/dev/input/event4`: decoded ABS_MT stream with `CLOCK_MONOTONIC`;
  - context markers: lid state, keyboard activity, suspend/resume, "I am
    touching / not touching" hotkey.
  Output: `corpus/<label>-<timestamp>/{hid.evdev.jsonl,frames.bin,meta.json}`.
- `tools/replay.py` — feed a corpus through a decision function offline.

Exit: 30 min of capture is a few MB, evdev and hidraw frames align 1:1, replay
is deterministic.

### P2 — Characterisation: the ghost map (1-2 days)

- `tools/analyze.py`: activation heatmap of idle frames on a coarse grid
  (50x50 units ≈ 4.5 mm), hotspot extraction (clusters, not hand-drawn boxes),
  Width/Height distributions for real vs phantom contacts, single-scan
  fraction, repeat-position variance per hotspot, ContactCount anomalies.
- `tools/target_drill.py`: fullscreen grid of targets; the user taps each one
  N times. Produces (a) the **dead-zone map**, (b) the real-touch geometry
  distribution, (c) the ground-truth corpus for G2.
- Output `analysis/ghost-map.json` + `analysis/report.md` with numbers.

Exit: three independent idle captures reproduce the same hotspots; every
phantom event is attributable to a hotspot or to a named geometry/timing rule.

### P3 — Userspace prototype (2-3 days)

- `phantom/decide.py` — the pure, dependency-free decision function.
- `phantom/daemon.py` — EVIOCGRAB proxy on a uinput clone, with:
  - `--shadow` (pass everything, log every decision — measure with no effect),
  - `--config config/filter.toml`,
  - fail-open on any error, and `phantomctl off` to release the grab.
- `tests/` — corpus-driven unit tests: each recorded phantom must be dropped,
  each recorded real touch must be kept. Adding a corpus file adds a test.

Exit: scored on the corpora — ≥95 % suppression, ≤2 % false rejection, added
latency measured (< 5 ms), shadow log matches offline replay exactly.

### P4 — HID-BPF implementation (3-5 days)

- `bpf/ftsc1000_filter.bpf.c` — `hid_bpf_ops` `.input_report` with
  `INSERT_HEAD`: read the frame, evaluate the same rules (ghost rectangles
  compiled in from `ghost-map.json`, plus the lifecycle state machine),
  and either drop the frame or clear the offending slot's TipSwitch/ContactID
  and fix `ContactCount`. Never mutate a slot that the user is actually
  holding — hysteresis is mandatory.
- `udev-hid-bpf install` + hwdb entry `hid-bpf:hid:b0018g0001v00002808p00005112`
  so it attaches at bind automatically.
- Parity test: replay the corpus through the BPF program (userspace harness
  with the same C code, or `bpf_prog_test_run` if the attach type allows) and
  diff against the Python decisions.
- Disable the P3 daemon when the BPF program is active (never both).

Exit: `udev-hid-bpf list-bpf-programs` shows the program after reboot; metrics
equal P3 on the same corpus; no userspace process needed; kernel upgrade does
not break it.

P4b (optional research, read-only): the controller exposes a 256-byte vendor
feature page (`0xFF00`/`0xC5`) and a 63-byte vendor input report. If that is a
FocalTech test/MP interface it may report per-node capacitance, which would let
us detect the damaged node directly instead of inferring it. **Read-only
probes only, on a bus we can power-cycle; never issue firmware or calibration
writes.**

### P5 — Hardening and packaging (2 days)

- Negative tests: touchpad (`04F3:301A`), pen, other HID devices unaffected.
- Fail-open everywhere: if the program is missing/refuses to load, the
  touchscreen behaves exactly as stock.
- `phantomctl` status/doctor: is the filter attached? which ruleset? last
  drift check? recent phantom rate?
- Packaging: RPM or `install.sh` + systemd unit only for the optional proxy;
  the BPF path needs no service.

### P6 — Long-term tracking (ongoing, cheap)

- On idle (or weekly), auto-capture 5 minutes -> append a ghost-map snapshot to
  `analysis/history/`.
- Drift report: new hotspots, growing hot area, rising single-scan fraction.
  Calibration drift == damage spreading; the threshold table should be
  re-derived, but **never auto-applied** (a silent retune can start eating real
  touches).
- Keep a short "damage diary" in the wiki: date, hotspot count, area covered.

## 6. Repo layout

```
docs/hardware.md        verified device facts + re-verify commands
docs/plan.md            this plan
docs/guide.md           tiered guide (30s / 2min / 5min / 15min / 1h)
tools/capture.py        dual-source recorder
tools/analyze.py        heatmap + hotspot + geometry analysis
tools/target_drill.py   dead-zone / ground-truth drill
tools/replay.py         corpus -> decisions (offline)
phantom/decide.py       pure decision function (shared by all harnesses)
phantom/daemon.py       userspace uinput proxy (harness + fallback)
bpf/ftsc1000_filter.bpf.c
config/filter.toml      rectangles, thresholds, guard
scripts/touchscreen-toggle.sh
scripts/install-access.sh   adds input group + hidraw udev rule
analysis/               ghost-map.json, report.md, history/
corpus/                 captures (git-ignored — finger-position data)
```

## 7. Guardrails

1. **Target by identity, never by node name.** Everything matches
   `0018:2808:5112` / the `i2c-FTSC1000:00` sysfs path. The touchpad is
   `0018:04F3:301A` on the same bus and must never be grabbed or filtered.
2. **Fail open.** A missing filter is stock behaviour. A filter that fails
   closed bricks the only pointing device on a convertible.
3. **One filter at a time.** Proxy and BPF both active = double filtering.
4. **No writes to the controller** during exploration (no firmware, no
   calibration, no vendor feature writes) unless we can power-cycle it.
5. **Captures stay local.** They record finger positions — git-ignore `corpus/`,
   commit only derived maps and aggregate stats.
6. **Tunables live in one file** and every change is measured against the stored
   corpora before it is enabled.

## 8. Risks and unknowns

- *Ghost and real touch overlap.* If a hotspot sits where the user actually
  taps, suppression and usability fight. Mitigation: score-based rules, not a
  hard rectangle; a per-region aggressiveness knob; the drill corpus decides.
- *Frame mutation side effects.* Clearing a slot mid-stream can make
  `hid-multitouch` emit an unintended release/re-acquire. Safer to never let a
  ghost acquire a slot (decide on the first frame a contact appears).
- *Damage is mechanical and spreading.* Static maps go stale -> P6 exists for
  this reason.
- *The 7.2.13 kernel boot* must be re-verified (kernel-devel is only installed
  for 7.0.12; BPF needs no module build, which is another argument for C).
- *Latency* on a 4-core laptop: BPF costs microseconds; the userspace proxy
  costs a wakeup per report (measure it, but expect it to be the bottleneck).

## 9. Effort

P0 ½d · P1 1d · P2 2d · P3 3d · P4 5d · P5 2d, plus P6 upkeep. P0-P3 already
produce a usable daily-driver mitigation; P4 makes it disappear from userspace.

## 10. Open decisions

1. Permanent form: HID-BPF only (recommended), or keep the userspace proxy as
   the shipping form?
2. Dead zones: purely suppress, or also snap/expand contacts near dead
   boundaries?
3. Typing guard: wanted (fewer phantoms while typing) or too aggressive?
4. How much of the screen is usable today — i.e. is this "make it pleasant
   again" or "it is currently unusable"?
