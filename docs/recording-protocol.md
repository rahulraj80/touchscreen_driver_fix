# Pilot protocol — phantom-only recording

Status: **active** (2026-10-29). This is the operational contract for the
first data-collection window. It implements the four decisions taken after the
plan review: userspace first, phantom-only, long unattended capture, and the
labelling rule below.

## The rule

**During a recording session the screen is not touched. Every contact in the
session is a phantom.**

That is the whole labelling scheme. It gives a corpus with 100 % precision
labels for suppression (G1) without any drill, and it is the only labelling the
phantom filter actually needs.

If the screen is touched accidentally:

1. Note the wall-clock time to the second (phone, another machine, or
   `scripts/capture.sh mark "accidental 14:32:07"` — the marker goes into the
   log with a monotonic timestamp and is the more precise option).
2. At the end of the session, list those times. They become exclusion windows
   in `labels.json`; contacts inside them are treated as *real* and never
   counted as phantoms.

No marker is required for normal operation. A session with zero accidental
touches needs no reconciliation at all.

## Why this shape

- Phantom touches are rare, so the unit of work is a **long background
  session**, not a short scripted one.
- Not touching means we do not need a target drill or a dead-zone map to start
  suppressing. The dead-zone map is deferred until real-touch validation is
  needed (G2).
- Because labels come from wall-clock notes, every heartbeat carries a UTC
  epoch timestamp (`"u"`) alongside the monotonic `t`, so a noted time maps
  back to an exact event range even across suspend (the `sk` skew in the same
  line detects that).

## What the recording looks like

```sh
# once, as root: scoped ACL so the seat user can read hidraw + event nodes
sudo scripts/install-access.sh
scripts/doctor.sh                     # must exit 0

# start a long session (user unit, survives lock/suspend; restart on crash)
scripts/capture.sh install            # systemd --user, enable --now

# ... use the laptop normally, do not touch the screen ...

scripts/capture.sh status             # heartbeat: rates, markers, uptime
scripts/capture.sh verify             # integrity + measured MB/hour
scripts/capture.sh stop               # or:
systemctl --user stop phantom-touch-capture
```

Data lands in `corpus/<label>-<host>-<UTC>/` (git-ignored):

- `seg-NNNNN.jsonl.gz` — ordered events (`h` raw HID, `e` evdev, `m` marker,
  `b` heartbeat with UTC).
- `meta.json` — devices, args, start/stop, totals.
- `status.json` — live heartbeat state.
- `control` — FIFO for markers (`scripts/capture.sh mark "note"`).

## What gets measured from a phantom-only session

| Metric | Why |
|---|---|
| contacts/hour | severity, and how long a window must be |
| coordinate heatmap | where the damage fires (ghost map) |
| Width/Height distribution | degenerate geometry is a cheap gate |
| single-scan fraction | lifecycle gate |
| repeat-position variance | fixed-spot ghosts |
| ContactCount vs active slots | consistency gate |
| burst structure | how many frames a phantom lasts |

No false-rejection number can be produced from this corpus — by construction
there are no real touches. That is accepted for the phantom-only phase; a
separate short real-touch session is required before enabling suppression, and
it is deferred.

## Guardrails that still apply

- Never capture with the touchpad matched; the udev rule and discovery match
  only `0018:2808:5112`.
- Capture is a passive reader: it never grabs the evdev node and never writes
  to the controller.
- `corpus/` stays out of git.
- One filter at a time once filtering starts.
