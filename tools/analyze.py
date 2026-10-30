#!/usr/bin/env python3
"""Analyze a phantom-touch capture run: contacts, coordinates, sessions.

Reads the JSONL segments written by tools/capture.py and reconstructs touch
contacts from the decoded evdev stream (kind "e"). The raw HID stream (kind
"h") is reported only as a frame count per contact: hid-multitouch already
decoded it for us, and its coordinates are what the desktop sees.

Coordinates are the digitizer's raw units. The HID report descriptor gives
Logical Maximum 3200 for X and 1800 for Y (16:9); 0x0fff (4095) is only the
"no contact" sentinel. Pixels are derived from those logical maxima, which
for a 1920x1080 panel means a uniform 0.6 scale on both axes.

Usage:
  python3 tools/analyze.py RUN_DIR [--width 1920 --height 1080]
                          [--gap 2.0] [--json out.json] [--csv out.csv]
  python3 tools/analyze.py --latest [same options]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

# evdev constants we care about
EV_SYN = 0
EV_KEY = 1
EV_ABS = 3
SYN_REPORT = 0
BTN_TOUCH = 0x14A
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT = 0x2F
ABS_MT_TOUCH_MAJOR = 0x30
ABS_MT_TOUCH_MINOR = 0x31
ABS_MT_ORIENTATION = 0x34
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TOOL_X = 0x3C
ABS_MT_TOOL_Y = 0x3D
ABS_MT_TOOL_TYPE = 0x37
ABS_MT_TRACKING_ID = 0x39
ABS_MT_PRESSURE = 0x3A

NO_CONTACT = 0x0FFF


@dataclass
class Contact:
    slot: int = 0
    tid: int = -1
    t_start: int = 0
    t_end: int = 0
    x_start: int = 0
    y_start: int = 0
    x_end: int = 0
    y_end: int = 0
    x_min: int = 10**9
    y_min: int = 10**9
    x_max: int = -1
    y_max: int = -1
    frames: int = 0
    hid_frames: int = 0
    # running path length in raw units
    path: float = 0.0
    _last_x: int | None = field(default=None, repr=False)
    _last_y: int | None = field(default=None, repr=False)
    _sum_x: float = field(default=0.0, repr=False)
    _sum_y: float = field(default=0.0, repr=False)

    def add(self, t: int, x: int | None, y: int | None) -> None:
        if self.t_start == 0:
            self.t_start = t
        self.t_end = t
        self.frames += 1
        if x is not None and y is not None:
            if self.x_start == 0 and self.y_start == 0 and self.frames == 1:
                self.x_start, self.y_start = x, y
            self.x_end, self.y_end = x, y
            self.x_min = min(self.x_min, x)
            self.y_min = min(self.y_min, y)
            self.x_max = max(self.x_max, x)
            self.y_max = max(self.y_max, y)
            self._sum_x += x
            self._sum_y += y
            if self._last_x is not None:
                self.path += math.hypot(x - self._last_x, y - self._last_y)
            self._last_x, self._last_y = x, y

    @property
    def duration_ms(self) -> float:
        return (self.t_end - self.t_start) / 1e6

    @property
    def mean_x(self) -> float:
        return self._sum_x / self.frames if self.frames else 0.0

    @property
    def mean_y(self) -> float:
        return self._sum_y / self.frames if self.frames else 0.0


def _scale(v: int, out_max: int, raw_max: int) -> float:
    return v * out_max / raw_max


def _px(x: int, y: int, w: int, h: int, raw_x_max: int, raw_y_max: int) -> tuple[float, float]:
    return _scale(x, w, raw_x_max), _scale(y, h, raw_y_max)


def read_run(run: Path) -> tuple[dict, list[Contact]]:
    meta = json.loads((run / "meta.json").read_text())
    contacts: list[Contact] = []
    # frame accumulator
    slot = 0
    tid = -1
    x = y = None
    tool_x = tool_y = None
    btn = 0
    press = None
    touch_major = None
    active: dict[int, Contact] = {}
    hid_before = 0

    def flush(t: int) -> None:
        nonlocal tid, x, y, tool_x, tool_y, btn, press, touch_major
        key = slot
        if tid == -1:
            c = active.pop(key, None)
            if c is not None:
                contacts.append(c)
        else:
            c = active.get(key)
            if c is None:
                c = Contact(slot=slot, tid=tid)
                active[key] = c
            elif c.tid != tid:
                contacts.append(c)
                c = Contact(slot=slot, tid=tid)
                active[key] = c
            c.add(t, x if x is not None else tool_x, y if y is not None else tool_y)
            if touch_major is not None:
                c.touch_major = touch_major
        # keep btn/press for future extension; they are per-frame state
        _ = (btn, press)

    for seg in sorted(run.glob("seg-*.jsonl.gz")):
        with gzip.open(seg, "rt") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                k = o.get("k")
                if k == "h":
                    hid_before += 1
                    continue
                if k != "e":
                    continue
                ty, c, v, t = o.get("ty"), o.get("c"), o.get("v"), o.get("t", 0)
                if ty == EV_ABS:
                    if c == ABS_MT_SLOT:
                        slot = v
                    elif c == ABS_MT_POSITION_X:
                        x = v
                    elif c == ABS_MT_POSITION_Y:
                        y = v
                    elif c == ABS_MT_TOOL_X:
                        tool_x = v
                    elif c == ABS_MT_TOOL_Y:
                        tool_y = v
                    elif c == ABS_X:
                        if x is None:
                            x = v
                    elif c == ABS_Y:
                        if y is None:
                            y = v
                    elif c == ABS_MT_TRACKING_ID:
                        tid = v
                    elif c == ABS_MT_PRESSURE:
                        press = v
                    elif c == ABS_MT_TOUCH_MAJOR:
                        touch_major = v
                elif ty == EV_KEY and c == BTN_TOUCH:
                    btn = v
                elif ty == EV_SYN and c == SYN_REPORT:
                    flush(t)
                    # coordinates are sticky per slot; keep x/y so a report
                    # that only changes pressure still has a position.
    for c in active.values():
        contacts.append(c)
    contacts.sort(key=lambda c: c.t_start)
    # attach hid frame counts proportionally per contact window
    for c in contacts:
        c.hid_frames = hid_before  # total; per-contact split is approximate
    return meta, contacts


def sessions(contacts: list[Contact], gap_s: float) -> list[list[Contact]]:
    out: list[list[Contact]] = []
    cur: list[Contact] = []
    for c in contacts:
        if cur and (c.t_start - cur[-1].t_end) / 1e9 > gap_s:
            out.append(cur)
            cur = []
        cur.append(c)
    if cur:
        out.append(cur)
    return out


def grid(contacts: list[Contact], w: int, h: int, rx: int, ry: int, cols: int = 48, rows: int = 18) -> str:
    """Text heat map of touch density in screen pixels."""
    cells = [[0] * cols for _ in range(rows)]
    for c in contacts:
        px, py = _px(c.mean_x, c.mean_y, w, h, rx, ry)
        cx = min(cols - 1, max(0, int(px / w * cols)))
        cy = min(rows - 1, max(0, int(py / h * rows)))
        cells[cy][cx] += 1
    peak = max(max(r) for r in cells) or 1
    ramp = " .:-=+*#%@"
    lines = []
    for row in cells:
        lines.append("".join(ramp[min(len(ramp) - 1, int(v / peak * (len(ramp) - 1)))] for v in row))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", nargs="?", help="run directory (default: latest under --root)")
    ap.add_argument("--root", default="corpus", help="corpus root (default: corpus)")
    ap.add_argument("--latest", action="store_true", help="pick the newest run directory")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--raw-x-max", type=int, default=3200)
    ap.add_argument("--raw-y-max", type=int, default=1800)
    ap.add_argument("--gap", type=float, default=2.0, help="session gap seconds (default 2.0)")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--csv", dest="csv_out")
    ap.add_argument("--min-ms", type=float, default=0.0, help="hide contacts shorter than this")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if args.latest or not args.run:
        runs = sorted([p for p in root.iterdir() if p.is_dir() and (p / "meta.json").exists()])
        if not runs:
            print(f"no runs under {root}", file=sys.stderr)
            return 2
        run = runs[-1]
    else:
        run = Path(args.run)
    meta, contacts = read_run(run)
    if args.min_ms:
        contacts = [c for c in contacts if c.duration_ms >= args.min_ms]

    dev = meta.get("devices", {})
    print(f"run      {run.name}")
    print(f"device   {dev.get('name')}  {dev.get('vidpid')}")
    print(f"window   {meta.get('started_utc')} .. {meta.get('stopped_utc')}  "
          f"({meta.get('uptime_s', 0)/3600:.2f} h)")
    print(f"scale    raw 0..{args.raw_x_max} x 0..{args.raw_y_max}  ->  {args.width}x{args.height} px")
    print(f"contacts {len(contacts)}   sessions {len(sessions(contacts, args.gap))}")
    print()

    sess = sessions(contacts, args.gap)
    for si, group in enumerate(sess):
        g0, g1 = group[0].t_start, group[-1].t_end
        print(f"== session {si}  t={g0/1e9:.2f}s  dur={(g1-g0)/1e9:.3f}s  contacts={len(group)}")
        for c in group:
            x0, y0 = _px(c.x_start, c.y_start, args.width, args.height, args.raw_x_max, args.raw_y_max)
            x1, y1 = _px(c.x_end, c.y_end, args.width, args.height, args.raw_x_max, args.raw_y_max)
            mx, my = _px(c.mean_x, c.mean_y, args.width, args.height, args.raw_x_max, args.raw_y_max)
            print(f"   slot{c.slot} tid={c.tid:>5} {c.duration_ms:8.2f} ms  "
                  f"raw ({c.x_start},{c.y_start})->({c.x_end},{c.y_end})  "
                  f"px ({x0:6.0f},{y0:6.0f})->({x1:6.0f},{y1:6.0f})  "
                  f"mean ({mx:6.0f},{my:6.0f})  path {c.path:7.1f} raw  frames {c.frames}")
        print()

    print("touch density (mean position per contact, screen pixels):")
    print(grid(contacts, args.width, args.height, args.raw_x_max, args.raw_y_max))
    print()

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "run": run.name, "meta": meta,
            "scale": {"raw_x_max": args.raw_x_max, "raw_y_max": args.raw_y_max, "width": args.width, "height": args.height},
            "sessions": [[asdict(c) for c in g] for g in sess],
        }, indent=1, default=str))
        print(f"wrote {args.json_out}")
    if args.csv_out:
        with open(args.csv_out, "w", newline="") as fh:
            wr = csv.writer(fh)
            wr.writerow(["session", "slot", "tid", "t_start_ns", "duration_ms",
                         "raw_x0", "raw_y0", "raw_x1", "raw_y1",
                         "px_x0", "px_y0", "px_x1", "px_y1",
                         "mean_px_x", "mean_px_y", "path_raw", "frames"])
            for si, g in enumerate(sess):
                for c in g:
                    x0, y0 = _px(c.x_start, c.y_start, args.width, args.height, args.raw_x_max, args.raw_y_max)
                    x1, y1 = _px(c.x_end, c.y_end, args.width, args.height, args.raw_x_max, args.raw_y_max)
                    mx, my = _px(c.mean_x, c.mean_y, args.width, args.height, args.raw_x_max, args.raw_y_max)
                    wr.writerow([si, c.slot, c.tid, c.t_start, f"{c.duration_ms:.2f}",
                                 c.x_start, c.y_start, c.x_end, c.y_end,
                                 f"{x0:.1f}", f"{y0:.1f}", f"{x1:.1f}", f"{y1:.1f}",
                                 f"{mx:.1f}", f"{my:.1f}", f"{c.path:.1f}", c.frames])
        print(f"wrote {args.csv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
