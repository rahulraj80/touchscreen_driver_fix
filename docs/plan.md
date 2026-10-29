# Plan — phantom-touch filter for the UX360CA touchscreen

Status: **active** (2026-10-29); decisions settled — userspace filter first,
phantom-only labelling. Hardware facts: [hardware.md](hardware.md). Recording
contract: [recording-protocol.md](recording-protocol.md).

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
| **B. Userspace evdev proxy** (EVIOCGRAB + uinput) | low | seconds, no reboot | yes (systemd unit) | Full flexibility, testable offline, but adds a daemon and a second input device that libinput must classify; extra latency; dies => must fail open. **Dev harness only** — not shipped. |
| **C. HID-BPF at the HID device** | medium | reload in seconds, no reboot | yes, and across kernel upgrades (BTF-portable) | Filters *before* `hid-multitouch` allocates MT slots: the cleanest place, no fork of a generic driver, no module signing, auto-attach via udev. **Recommended carrier.** |
| **D. Patch `hid-multitouch`** | high | rebuild + reload + reboot | no (akmods/dkms) | Needed only if C cannot express a rule (e.g. MT-slot semantics changes). Carries a generic-driver fork forever. |

**Decision (settled 2026-10-29, ADR `filter-at-the-hid-device` in the
knowledge repo): build the logic once, ship it as C.** Phase B is the
development harness (fast, offline-replayable, gives per-decision logs) and is
switched off once C is attached — it is not a permanent fallback. Both consume
the *same* declarative rule table so behaviour can be verified identical on a
recorded corpus.

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
Written and committed; **one root run still to do** (no passwordless sudo here).

- `scripts/touchscreen-toggle.sh status|on|off` — binds/unbinds by *sysfs path*
  and VID/PID (`0018:2808:5112`), never by event node number, and never the
  touchpad. `off` is the same mechanism as the desktop's "disable touchscreen".
- `udev/90-touchscreen-phantom-capture.rules` + `scripts/install-access.sh` —
  scoped `TAG+="uaccess"` ACL on exactly `/dev/hidraw*` and the touchscreen
  `/dev/input/event*`; `--remove` undoes it. No `input` group membership.
- `scripts/doctor.sh` — read-only check: device present and bound, event and
  hidraw nodes found, nodes readable by the current user, touchpad confirmed
  separate, `CONFIG_HID_BPF`/BTF/`udev-hid-bpf` present, tooling installed.
  Exits non-zero while access is missing (currently: 2 failed, 4 warnings —
  `evtest`, `python3-evdev`, `clang`, `bpftool` absent).
- Tooling to install: `evtest python3-evdev clang bpftool hidrd`
  (`hid-tools` is not packaged on F44; the capture rig reads hidraw directly).
- Still to do after the root run: a 10-minute idle baseline and the first
  "what does it look like" report.

Exit: can disable/enable the digitizer without reboot; capture runs as `rahul`;
first phantom frames captured. (`doctor.sh` exit 0 is the gate.)

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

### P3 — Userspace filter (ship first)

- `phantom/decide.py` — the pure, dependency-free decision function.
- `phantom/daemon.py` — EVIOCGRAB proxy on a uinput clone, with:
  - `--shadow` (pass everything, log every decision — measure with no effect),
  - `--config config/filter.toml`,
  - fail-open on any error, and `phantomctl off` to release the grab.
- Installed as a **`systemd --user`** unit, not a system daemon: it needs the
  seat user's udev ACL and nothing more.
- `tests/` — corpus-driven unit tests: each recorded phantom must be dropped,
  each recorded real touch must be kept. Adding a corpus file adds a test.

Exit: scored on the phantom-only corpora for suppression (≥95 %), then on a
short real-touch session for false rejection (≤2 %); added latency measured
(< 5 ms); shadow log matches offline replay exactly. **This is the first
deliverable that changes daily use.**

### P4 — HID-BPF promotion (only if the userspace filter is not enough)

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
- Packaging: the shipped artifact is the `bpf.o` + one hwdb line (plus the
  `phantomctl` helper and the ghost-map snapshots). **No service ships** — the
  proxy runs only while measuring, and its systemd unit stays a dev convenience.

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
docs/recording-protocol.md   phantom-only pilot protocol + labelling rule
docs/guide.md           tiered guide (30s / 2min / 5min / 15min / 1h)
tools/capture.py        dual-source recorder
tools/analyze.py        heatmap + hotspot + geometry analysis
tools/target_drill.py   dead-zone / ground-truth drill
tools/replay.py         corpus -> decisions (offline)
phantom/decide.py       pure decision function (shared by all harnesses)
phantom/daemon.py       userspace uinput proxy (dev harness only, not shipped)
bpf/ftsc1000_filter.bpf.c
config/filter.toml      rectangles, thresholds, guard
scripts/touchscreen-toggle.sh   on/off/status, VID:PID matched
scripts/install-access.sh       installs the scoped udev ACL (--remove undoes)
scripts/doctor.sh               read-only readiness check (exit 0 = ready)
scripts/capture.sh              start/stop/status/verify/mark/install capture
systemd/user/phantom-touch-capture.service   userspace capture unit
udev/90-touchscreen-phantom-capture.rules
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

P0 ½d · P1 1d · P2 2d · P3 3d · P4 5d · P5 2d, plus P6 upkeep. P0-P3 produce a
usable daily-driver mitigation; P4 is only pursued if userspace is not enough.
Corpus collection runs in the background throughout, so wall-clock (not effort)
dominates.

## 10. Decisions (settled 2026-10-29)

1. **Userspace filter first; HID-BPF is the promotion path.** The first
   *shippable* artifact is the userspace evdev/uinput filter (P3), installed as
   a `systemd --user` unit. HID-BPF (P4) is only attempted once the rules are
   proven on recorded corpora and the userspace latency/fail-open behaviour is
   measured. It is no longer "BPF or nothing": if the userspace filter is good
   enough, it stays.
2. **Dead zones are not recovered.** Only phantom contacts are suppressed — no
   snapping, no expanding hit areas near dead boundaries. Revisit only after
   the ghost map is known and if the blank strips turn out to be painful.
3. **Capture access via a scoped udev ACL rule** on exactly `/dev/hidraw0` and
   `/dev/input/event4` (seat user), not `input` group membership.
4. **Severity today: occasional.** No stopgap filter, no blanket region mask.
   Measurement can take its time; the ghost map has to be trustworthy before
   anything is enabled.
5. **Labelling: phantom-only sessions.** During a recording the screen is not
   touched, so every contact is a phantom by construction. Accidental touches
   are noted by wall-clock time and reconciled at the end into exclusion
   windows. No drill and no real-touch corpus are required to begin;
   false-rejection measurement is deferred until suppression is ready to be
   validated.
6. **Long windows, not short sessions.** The recorder runs as an unattended
   user service for days, rotating and pruning. Phantom rarity makes the wall
   clock, not the script, the scarce resource.

The consequence of (1)+(2): the filter's whole job is "never let a damaged-node
contact become an event", and every rule that only exists to *improve* dead
areas is out of scope.

Deferred (not decided, revisit with data): the optional typing guard from §4 —
an ~80 ms suppression window after keyboard input. It reduces phantoms while
typing but is a mitigation, not a diagnosis, so it stays out of the first
ruleset.
