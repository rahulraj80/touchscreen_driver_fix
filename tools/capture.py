#!/usr/bin/env python3
"""Phantom-touch capture rig — dual-source, continuous, self-rotating recorder.

Records the FTSC1000 digitizer (`2808:5112`) from two places at once:

  * the raw HID reports   (/dev/hidraw0)  -> kind "h"  — ground truth bytes
  * the decoded evdev feed (/dev/input/event4) -> kind "e" — what the desktop sees

and interleaves context markers (lid, keyboard activity, suspend/resume,
"I am touching" hotkey, free-form notes) so phantom frames can be separated
from real fingers offline.

Design constraints that shaped this file
---------------------------------------
* Phantom touches are RARE. The recorder must survive hours of idle time in a
  single run, so it rotates to gzip segments on size or age, never at a fixed
  "session" length, and never loses a run to a transient device error.
* One process, one thread, one event ordering. Both device fds and the control
  FIFO are serviced by one selector loop; every event is written in the order
  it was observed. Timestamps are CLOCK_MONOTONIC taken at read time, so the
  two sources are directly comparable and replay is deterministic.
* Storage scales with *events*, not with time. An idle digitizer emits nothing,
  so hours of idle cost nothing. `verify` prints the measured MB/hour so the
  disk budget can be sized from data instead of a guess.
* Capture is a passive reader. It never writes to the HID device, never grabs
  the evdev node, and never touches the touchpad.

Layout of one run (git-ignored, finger-position data)::

    corpus/<label>-<host>-<UTC>/
        meta.json          run metadata, devices, args; rewritten at stop
        index.jsonl        one line per segment (file, time span, counts)
        seg-00000.jsonl    events, in order; older segments end up .jsonl.gz
        status.json        heartbeat state, rewritten every --heartbeat seconds
        control            FIFO for markers (echo touch-start > control)
        verify.txt         written by `capture.py verify`

Event schema (SCHEMA 1) — one JSON object per line, key order stable::

    {"t": 123456789, "k": "h", "d": "01a2..."}            # hidraw read()
    {"t": 123456790, "k": "e", "ty": 3, "c": 57, "v": 12} # evdev input_event
    {"t": 123456791, "k": "m", "n": "touch-start"}        # marker
    {"t": 123456792, "k": "b", "sk": 0, "lid": "open", "hb": 3, "u": 1761...}  # heartbeat, "u" = UTC epoch ns

`t` is CLOCK_MONOTONIC nanoseconds; `sk` is BOOTTIME-MONOTONIC skew, which
changes only across a suspend — that is how suspend gaps are detected.
NOTE: hidraw "h" lines are one per read(); the driver returns one report per
read for this device. A coalesced read would show as an unusually long `d`.

Subcommands
-----------
run       record until SIGTERM/SIGINT or --duration elapses
devices   print the identity-matched nodes (no privileges needed)
verify    validate a run directory and print measured rates
status    print the latest heartbeat (or --run DIR)
selftest  end-to-end proof against two synthetic FIFOs

Exit codes: 0 ok, 1 fatal, 2 usage, 3 verify found problems.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import errno
import gzip
import json
import os
import re
import selectors
import shutil
import signal
import socket
import stat
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
VERSION = "1.0.0"
DEFAULT_VIDPID = "2808:5112"
DEFAULT_HEARTBEAT = 10.0
DEFAULT_ROTATE_BYTES = 64 * 1024 * 1024
DEFAULT_ROTATE_SECONDS = 900.0
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_KEEP_RUNS = 8
# Hard floor on the output filesystem. An unattended run must never be the
# reason a laptop cannot boot; at 25 GB free this leaves room for everything
# else and stops the recorder cleanly instead of filling the disk.
DEFAULT_MIN_FREE_BYTES = 4 * 1024 * 1024 * 1024
# How often the free-space floor is evaluated (seconds).
FREE_SPACE_CHECK_SECONDS = 60.0

# struct input_event { struct timeval time; __u16 type, code; __s32 value; }
# 64-bit: 8 + 8 + 2 + 2 + 4 = 24 bytes. The kernel's own timestamp is ignored
# on purpose: we stamp with CLOCK_MONOTONIC at read time so both sources agree.
INPUT_EVENT = struct.Struct("qqHHi")

EV_SYN, EV_KEY, EV_ABS, EV_MSC, EV_SW = 0, 1, 3, 4, 5

# Legend only — helps a human reading raw JSONL with jq. Codes stay numeric in
# the stream so replay stays dumb and fast.
ABS_LEGEND = {
    0x2F: "ABS_MT_SLOT",
    0x30: "ABS_MT_TOUCH_MAJOR",
    0x31: "ABS_MT_TOUCH_MINOR",
    0x32: "ABS_MT_WIDTH_MAJOR",
    0x33: "ABS_MT_WIDTH_MINOR",
    0x34: "ABS_MT_ORIENTATION",
    0x35: "ABS_MT_POSITION_X",
    0x36: "ABS_MT_POSITION_Y",
    0x37: "ABS_MT_TOOL_TYPE",
    0x39: "ABS_MT_TRACKING_ID",
    0x3A: "ABS_MT_PRESSURE",
    0x3B: "ABS_MT_DISTANCE",
    0x3C: "ABS_MT_TOOL_X",
    0x3D: "ABS_MT_TOOL_Y",
}
EV_LEGEND = {0: "EV_SYN", 1: "EV_KEY", 2: "EV_REL", 3: "EV_ABS", 4: "EV_MSC", 5: "EV_SW"}

LID_STATE = Path("/proc/acpi/button/lid/LID/state")


# --------------------------------------------------------------------------- #
# clock helpers
# --------------------------------------------------------------------------- #
def mono_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def boot_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_BOOTTIME)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def lid_state() -> str:
    try:
        text = LID_STATE.read_text().strip()
    except OSError:
        return "unknown"
    return text.rsplit(None, 1)[-1] if text else "unknown"


# --------------------------------------------------------------------------- #
# device discovery — identity first, node names never trusted
# --------------------------------------------------------------------------- #
def _uevent(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line:
                key, _, val = line.partition("=")
                out[key] = val
    except OSError:
        pass
    return out


def _realpath(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return ""


def discover(vidpid: str = DEFAULT_VIDPID) -> dict:
    """Locate the digitizer by VID:PID. Returns paths + what was verified."""
    vid, _, pid = vidpid.partition(":")
    vid = vid.strip().lower().zfill(4)
    pid = pid.strip().lower().zfill(4)
    want = f"{vid}:{pid}"
    result: dict = {
        "vidpid": f"{vid}:{pid}",
        "hidraw": None,
        "event": None,
        "hid_sysfs": None,
        "name": None,
        "driver": None,
        "errors": [],
    }

    hid_dir = None
    for dev in sorted(Path("/sys/bus/hid/devices").glob("*")):
        ev = _uevent(dev / "uevent")
        # HID_ID=0018:00002808:00005112  -> bus:vid:pid, each field zero-padded
        fields = ev.get("HID_ID", "").lower().split(":")
        if len(fields) == 3 and fields[1].zfill(8).endswith(vid) and fields[2].zfill(8).endswith(pid):
            hid_dir = dev
            result["name"] = ev.get("HID_NAME")
            result["hid_sysfs"] = str(dev)
            result["hid_bus"] = fields[0]
            break
    if hid_dir is None:
        result["errors"].append(f"no HID device matching {want} under /sys/bus/hid/devices")
        return result

    driver_link = hid_dir / "driver"
    if driver_link.exists():
        result["driver"] = os.path.basename(_realpath(str(driver_link)))

    hid_path = _realpath(str(hid_dir))
    for raw in sorted(Path("/sys/class/hidraw").glob("hidraw*")):
        if _realpath(str(raw / "device")).startswith(hid_path):
            result["hidraw"] = f"/dev/{raw.name}"
            break

    # Pick the evdev node under this HID device. A touchscreen usually exposes
    # one node; if the driver also made a "Mouse" alias node, prefer the one
    # whose ABS capability includes ABS_MT_SLOT (0x2f -> bit 47).
    candidates = []
    for node in sorted(Path("/sys/class/input").glob("event*")):
        if not _realpath(str(node / "device")).startswith(hid_path):
            continue
        name = ""
        try:
            name = (node / "device" / "name").read_text().strip()
        except OSError:
            pass
        mt = False
        try:
            abs_mask = (node / "device" / "capabilities" / "abs").read_text().strip()
            words = [int(w, 16) for w in abs_mask.split()][::-1]
            bit = 0x2F
            idx, off = divmod(bit, 64)
            mt = idx < len(words) and bool(words[idx] >> off & 1)
        except (OSError, ValueError):
            pass
        candidates.append((node.name, name, mt))
    candidates.sort(key=lambda c: (c[2], "mouse" not in c[1].lower()), reverse=True)
    if candidates:
        result["event"] = f"/dev/input/{candidates[0][0]}"
        result["event_name"] = candidates[0][1]
        result["event_is_mt"] = candidates[0][2]
    else:
        result["errors"].append(f"HID device {hid_dir.name} exposes no evdev node")
    return result


def find_keyboard() -> str | None:
    """Best-effort: the AT keyboard node, for typing-context markers."""
    for node in sorted(Path("/sys/class/input").glob("event*")):
        try:
            name = (node / "device" / "name").read_text().strip()
        except OSError:
            continue
        if "AT Translated Set 2 keyboard" in name:
            return f"/dev/input/{node.name}"
    return None


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #
class Source:
    """One non-blocking fd feeding lines into the log."""

    def __init__(self, kind: str, path: str, key_only: bool = False):
        self.kind = kind  # "hid" | "ev"
        self.path = path
        self.key_only = key_only
        self.fd = -1
        self.buf = bytearray()
        self.next_open = 0.0
        self.backoff = 0.2
        self.opens = 0
        self.eofs = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_seen: int | None = None

    # -- lifecycle ----------------------------------------------------------
    def open(self) -> str | None:
        """Open the fd. Returns an error string on failure."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            self._failed(exc)
            return f"{self.path}: {exc.strerror}"
        try:
            st = os.fstat(fd)
        except OSError:
            st = None
        if st is not None and stat.S_ISREG(st.st_mode):
            os.close(fd)
            self._failed(OSError(errno.EINVAL, "is a regular file, not a device"))
            return f"{self.path}: regular file"
        self.fd = fd
        self.opens += 1
        self.backoff = 0.2
        self.next_open = 0.0
        self.buf.clear()
        return None

    def _failed(self, exc: OSError) -> None:
        self.errors += 1
        self.last_error = f"{exc.strerror or exc}"
        self.next_open = time.monotonic() + self.backoff
        self.backoff = min(self.backoff * 2, 5.0)

    def close(self) -> None:
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = -1

    @property
    def open_(self) -> bool:
        return self.fd >= 0


# --------------------------------------------------------------------------- #
# writer: rotating segments + compressor thread
# --------------------------------------------------------------------------- #
class Log:
    def __init__(
        self,
        run_dir: Path,
        compress: bool = True,
        rotate_bytes: int = DEFAULT_ROTATE_BYTES,
        rotate_seconds: float = DEFAULT_ROTATE_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        keep_runs: int = DEFAULT_KEEP_RUNS,
        quiet: bool = False,
    ):
        self.dir = run_dir
        self.compress = compress
        self.rotate_bytes = max(rotate_bytes, 1)
        self.rotate_seconds = max(rotate_seconds, 1.0)
        self.max_bytes = max_bytes
        self.keep_runs = keep_runs
        self.quiet = quiet

        self.seg_index = -1
        self.fh = None
        self.seg_path: Path | None = None
        self.seg_opened_mono = 0.0
        self.seg_events = 0
        self.seg_bytes = 0

        self.events = 0
        self.bytes = 0
        self.pruned_segments = 0
        self._pending: list[concurrent.futures.Future] = []
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gz")

    # -- helpers ------------------------------------------------------------
    def _segment_name(self, idx: int, gz: bool) -> str:
        return f"seg-{idx:05d}.jsonl" + (".gz" if gz else "")

    def free_bytes(self) -> int:
        """Free bytes on the filesystem holding the run directory (-1 = unknown)."""
        try:
            st = os.statvfs(self.dir)
        except OSError:
            return -1
        return st.f_bavail * st.f_frsize

    def prune_for_space(self, min_free: int) -> tuple[int, bool]:
        """Free space until ``min_free`` is met or nothing is left to prune.

        Order: oldest closed segments of this run, then whole older runs
        (oldest first, never this one). Returns (freed_bytes, still_short).
        Pruning never touches the open segment, so the stream stays valid.
        """
        if min_free <= 0:
            return 0, False
        free = self.free_bytes()
        if free < 0 or free >= min_free:
            return 0, False

        freed = 0
        active = self.seg_path.name if self.seg_path else None
        for path in sorted(self.dir.glob("seg-*.jsonl*")):
            if self.free_bytes() >= min_free:
                break
            if active and path.name == active:
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                continue
            freed += size
            self.pruned_segments += 1

        if self.free_bytes() >= min_free:
            return freed, False

        # Still short: retire whole older runs, oldest first.
        run_dir = self.dir
        siblings = []
        for cand in run_dir.parent.iterdir():
            if not cand.is_dir() or cand.resolve() == run_dir.resolve():
                continue
            if not (cand / "meta.json").is_file():
                continue
            try:
                siblings.append((cand.stat().st_mtime, cand))
            except OSError:
                continue
        for _mtime, cand in sorted(siblings):
            if self.free_bytes() >= min_free:
                break
            try:
                size = sum(p.stat().st_size for p in cand.rglob("*") if p.is_file())
                shutil.rmtree(cand)
            except OSError:
                continue
            freed += size
            self.pruned_segments += 1

        return freed, self.free_bytes() < min_free

    def _open_segment(self) -> None:
        self.seg_index += 1
        self.seg_path = self.dir / self._segment_name(self.seg_index, gz=False)
        self.fh = open(self.seg_path, "wb", buffering=1 << 16)
        self.seg_opened_mono = time.monotonic()
        self.seg_events = 0
        self.seg_bytes = 0

    def _close_segment(self) -> dict:
        assert self.fh is not None and self.seg_path is not None
        self.fh.flush()
        try:
            os.fsync(self.fh.fileno())
        except OSError:
            pass
        self.fh.close()
        info = {
            "seg": self.seg_index,
            "file": self.seg_path.name,
            "events": self.seg_events,
            "bytes": self.seg_bytes,
            "closed_utc": utc_now_iso(),
        }
        plain = self.seg_path
        self.fh = None
        if self.compress and self.seg_bytes > 0:
            info["file"] = self._segment_name(self.seg_index, gz=True)
            self._pending.append(self.pool.submit(self._gzip, plain))
            # bounded backlog: compression must never outrun the disk cap logic
            while len([f for f in self._pending if not f.done()]) > 4:
                time.sleep(0.02)
        with open(self.dir / "index.jsonl", "a", encoding="utf-8") as idx:
            idx.write(json.dumps(info, separators=(",", ":")) + "\n")
        return info

    @staticmethod
    def _gzip(path: Path) -> None:
        try:
            raw = path.read_bytes()
            with gzip.open(str(path) + ".gz", "wb", compresslevel=6) as out:
                out.write(raw)
            path.unlink()
        except OSError as exc:
            print(f"[capture] gzip failed for {path}: {exc}", file=sys.stderr)

    # -- public -------------------------------------------------------------
    def rotate(self, reason: str) -> None:
        if self.fh is not None:
            # The marker must land in the segment being closed, so write it
            # BEFORE _close_segment() nulls the handle.
            self._marker({"t": mono_ns(), "k": "m", "n": "segment-close", "x": reason, "seg": self.seg_index})
            self._close_segment()
        self._open_segment()
        self._marker({"t": mono_ns(), "k": "m", "n": "segment-open", "seg": self.seg_index})

    def _marker(self, obj: dict) -> None:
        self.write(obj)

    def write(self, obj: dict) -> None:
        assert self.fh is not None
        line = json.dumps(obj, separators=(",", ":")) + "\n"
        data = line.encode()
        self.fh.write(data)
        self.events += 1
        self.seg_events += 1
        self.seg_bytes += len(data)
        self.bytes += len(data)

    def maybe_rotate(self) -> str | None:
        if self.fh is None:
            return None
        if self.seg_bytes >= self.rotate_bytes:
            self.rotate("size")
            return "size"
        if time.monotonic() - self.seg_opened_mono >= self.rotate_seconds:
            self.rotate("age")
            return "age"
        return None

    def run_bytes(self) -> int:
        total = 0
        for path in self.dir.glob("seg-*.jsonl*"):
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def prune_if_needed(self) -> bool:
        """Drop oldest closed segments when the run exceeds --max-bytes."""
        if self.max_bytes <= 0:
            return False
        total = self.run_bytes()
        if total <= self.max_bytes:
            return False
        dropped = 0
        active = {self.seg_path.name} if self.seg_path else set()
        for path in sorted(self.dir.glob("seg-*.jsonl*")):
            if total <= int(self.max_bytes * 0.9):
                break
            if path.name in active:
                continue
            try:
                size = path.stat().st_size
                path.unlink()
            except OSError:
                continue
            total -= size
            dropped += 1
            self.pruned_segments += 1
        if dropped:
            self._marker(
                {"t": mono_ns(), "k": "m", "n": "prune", "x": f"dropped {dropped} oldest segment(s)", "kept_bytes": total}
            )
        return bool(dropped)

    def close(self) -> None:
        if self.fh is not None:
            # _close_segment() flushes, fsyncs and closes; mark the stop first.
            self._marker({"t": mono_ns(), "k": "m", "n": "stop", "seg": self.seg_index})
            self._close_segment()
        self.pool.shutdown(wait=True)

    # -- metadata -----------------------------------------------------------
    def write_status(self, payload: dict) -> None:
        tmp = self.dir / "status.json.tmp"
        try:
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.dir / "status.json")
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# run directory management
# --------------------------------------------------------------------------- #
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def make_run_dir(out: Path, label: str, keep_runs: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = _SAFE.sub("-", label).strip("-") or "run"
    out.mkdir(parents=True, exist_ok=True)
    base = f"{safe}-{socket.gethostname()}-{stamp}"
    run = out / base
    n = 1
    while run.exists():
        n += 1
        run = out / f"{base}-{n}"
    run.mkdir(mode=0o700)
    _prune_old_runs(out, keep_runs, keep=run)
    return run


def _prune_old_runs(out: Path, keep_runs: int, keep: Path | None = None) -> list[str]:
    if keep_runs <= 0:
        return []
    runs = []
    for child in out.iterdir():
        if not child.is_dir() or child == keep:
            continue
        meta = child / "meta.json"
        if not meta.is_file():
            continue
        try:
            started = json.loads(meta.read_text(encoding="utf-8")).get("started_utc", "")
        except (OSError, json.JSONDecodeError):
            started = ""
        runs.append((started, child))
    runs.sort(key=lambda item: item[0])
    removed = []
    while len(runs) > keep_runs - (1 if keep else 0):
        _, child = runs.pop(0)
        for path in sorted(child.rglob("*"), reverse=True):
            try:
                path.unlink() if path.is_file() else path.rmdir()
            except OSError:
                pass
        try:
            child.rmdir()
            removed.append(child.name)
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------- #
# the recorder
# --------------------------------------------------------------------------- #
class Recorder:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.out = Path(args.out).expanduser().resolve()
        self.label = args.label
        self.heartbeat = max(args.heartbeat, 1.0)
        self.duration = args.duration or 0.0

        self.stop_reason: str | None = None
        self.rotate_now = False
        self.reopen_now = False

        self.sources: list[Source] = []
        self.sel = selectors.DefaultSelector()
        self.log: Log | None = None
        self.run_dir: Path | None = None
        self.control: Path | None = None
        self.control_fh = None
        self.control_buf = bytearray()

        self.started_mono = time.monotonic()
        self.started_utc = utc_now_iso()
        self.last_skew: int | None = None
        self.suspend_count = 0
        self.suspend_gap_ns = 0
        self.lid = lid_state()
        self.marker_count = 0
        self.last_marker: str | None = None
        self.hid_frames = 0
        self.ev_events = 0
        self.key_events = 0
        self.beats = 0
        self.rotations = 0
        self.prev_snapshot = (0.0, 0, 0, 0)  # mono, bytes, hid, ev
        self.rates = {"bytes_per_min": 0.0, "hid_frames_per_min": 0.0, "ev_events_per_min": 0.0}
        self.next_beat = time.monotonic() + self.heartbeat
        self.deadline = self.started_mono + self.duration if self.duration else 0.0
        self.min_free_bytes = getattr(args, "min_free_bytes", DEFAULT_MIN_FREE_BYTES)
        self.next_space_check = 0.0
        self.space_pruned_bytes = 0
        self.low_disk = False
        self.hidraw_note_written = False
        self._ctl_sock: tuple[socket.socket, socket.socket] | None = None

    # -- setup --------------------------------------------------------------
    def _install_signals(self) -> None:
        rd, wr = socket.socketpair()
        rd.setblocking(False)
        wr.setblocking(False)
        self._ctl_sock = (rd, wr)

        def handler(signum, _frame):
            try:
                wr.send(bytes([signum & 0xFF]))
            except OSError:
                pass

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1):
            signal.signal(sig, handler)
        self.sel.register(rd, selectors.EVENT_READ, "ctl")

    def _open_control_fifo(self) -> None:
        assert self.run_dir is not None
        self.control = self.run_dir / "control"
        try:
            os.mkfifo(self.control, 0o600)
        except FileExistsError:
            pass
        except OSError as exc:
            print(f"[capture] cannot create control FIFO: {exc}", file=sys.stderr)
            return
        try:
            # read-write so the FIFO never reports EOF when no writer is attached
            fd = os.open(self.control, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            print(f"[capture] cannot open control FIFO: {exc}", file=sys.stderr)
            return
        self.sel.register(fd, selectors.EVENT_READ, "fifo")

    def _open_sources(self) -> None:
        args = self.args
        hid_path = args.hidraw
        ev_path = args.event
        self.devices: dict = {"vidpid": args.vidpid, "hidraw": hid_path, "event": ev_path}
        if not hid_path or not ev_path:
            found = discover(args.vidpid)
            self.devices.update({k: v for k, v in found.items() if k not in ("errors",)})
            self.devices["errors"] = found.get("errors", [])
            hid_path = hid_path or found.get("hidraw")
            ev_path = ev_path or found.get("event")
            self.devices["hidraw"] = hid_path
            self.devices["event"] = ev_path
            if found.get("errors"):
                for msg in found["errors"]:
                    print(f"[capture] discovery: {msg}", file=sys.stderr)

        if hid_path:
            self.sources.append(Source("hid", hid_path))
        if ev_path:
            self.sources.append(Source("ev", ev_path))

        if args.keyboard != "off":
            kb = args.keyboard if args.keyboard != "auto" else find_keyboard()
            if kb:
                self.sources.append(Source("ev", kb, key_only=True))
                self.devices["keyboard"] = kb
            elif args.keyboard != "auto":
                print(f"[capture] keyboard node not found: {args.keyboard}", file=sys.stderr)

        for src in self.sources:
            err = src.open()
            if err:
                print(f"[capture] cannot open {src.kind} source {err}", file=sys.stderr)
                continue
            self.sel.register(src.fd, selectors.EVENT_READ, src)

        if not any(s.open_ for s in self.sources):
            raise SystemExit("[capture] no readable source; run scripts/install-access.sh and scripts/doctor.sh")

    # -- reading ------------------------------------------------------------
    def _emit(self, obj: dict) -> None:
        assert self.log is not None
        self.log.write(obj)

    def _marker(self, name: str, text: str | None = None, **extra) -> None:
        obj = {"t": mono_ns(), "k": "m", "n": name}
        if text:
            obj["x"] = text
        obj.update(extra)
        try:
            self._emit(obj)
        except OSError as exc:
            print(f"[capture] write failed: {exc}", file=sys.stderr)
            self.stop_reason = "write-error"
            return
        self.marker_count += 1
        self.last_marker = name if not text else f"{name}: {text}"
        if not self.args.quiet:
            print(f"[capture] marker {self.last_marker}", file=sys.stderr)

    def _read_hid(self, src: Source) -> None:
        while True:
            try:
                data = os.read(src.fd, 8192)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EINTR):
                    return
                src._failed(exc)
                self.sel.unregister(src.fd)
                src.close()
                self._marker("device-gone", f"hidraw: {exc.strerror}", path=src.path)
                return
            if not data:
                src.eofs += 1
                src.next_open = time.monotonic() + src.backoff
                src.backoff = min(src.backoff * 2, 5.0)
                self.sel.unregister(src.fd)
                src.close()
                return
            t = mono_ns()
            src.last_seen = t
            self.hid_frames += 1
            try:
                self._emit({"t": t, "k": "h", "d": data.hex()})
            except OSError as exc:
                print(f"[capture] write failed: {exc}", file=sys.stderr)
                self.stop_reason = "write-error"
                return
            if len(data) > 128 and not self.hidraw_note_written:
                self.hidraw_note_written = True
                self._marker("hidraw-long-read", f"{len(data)} bytes in one read (coalesced reports?)")

    def _read_ev(self, src: Source) -> None:
        while True:
            try:
                chunk = os.read(src.fd, 4096)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EINTR):
                    return
                src._failed(exc)
                self.sel.unregister(src.fd)
                src.close()
                self._marker("device-gone", f"evdev: {exc.strerror}", path=src.path)
                return
            if not chunk:
                src.eofs += 1
                src.next_open = time.monotonic() + src.backoff
                src.backoff = min(src.backoff * 2, 5.0)
                self.sel.unregister(src.fd)
                src.close()
                return
            t = mono_ns()
            src.last_seen = t
            src.buf.extend(chunk)
            size = INPUT_EVENT.size
            off = 0
            try:
                while len(src.buf) - off >= size:
                    _sec, _usec, ty, code, value = INPUT_EVENT.unpack_from(src.buf, off)
                    off += size
                    if src.key_only:
                        if ty != EV_KEY:
                            continue
                        self.key_events += 1
                    else:
                        self.ev_events += 1
                    self._emit({"t": t, "k": "e", "ty": ty, "c": code, "v": value})
            except OSError as exc:
                print(f"[capture] write failed: {exc}", file=sys.stderr)
                self.stop_reason = "write-error"
                return
            finally:
                del src.buf[:off]
            if len(src.buf) > size * 64:
                # a driver handing us a runaway stream would grow this forever
                src.buf.clear()

    def _read_control(self, fd: int) -> None:
        try:
            chunk = os.read(fd, 4096)
        except (BlockingIOError, OSError):
            return
        if not chunk:
            return
        self.control_buf.extend(chunk)
        while b"\n" in self.control_buf:
            raw, _, rest = bytes(self.control_buf).partition(b"\n")
            self.control_buf = bytearray(rest)
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    obj = {"text": line}
                name = str(obj.pop("n", obj.pop("marker", "note")))
                self._marker(name, **{k: v for k, v in obj.items() if k != "t"})
                continue
            name, _, text = line.partition(" ")
            self._marker(name, text.strip() or None)

    # -- heartbeat ----------------------------------------------------------
    def _enforce_free_space(self) -> None:
        """Keep the output filesystem above its hard floor.

        An unattended multi-day run must never be the reason the laptop cannot
        boot. When free space drops under the floor we first drop our own oldest
        runs (never the current one), and if that is not enough we stop cleanly
        instead of filling the disk.
        """
        assert self.log is not None
        free = shutil.disk_usage(str(self.run_dir)).free
        if free >= self.min_free_bytes:
            if self.low_disk:
                self.low_disk = False
                self._marker("disk-recovered", free_bytes=free)
            return
        if not self.low_disk:
            self.low_disk = True
            self._marker("disk-low", free_bytes=free, floor_bytes=self.min_free_bytes)
        freed = 0
        for old in self._old_runs():
            if free + freed >= self.min_free_bytes:
                break
            try:
                size = dir_bytes(old)
                shutil.rmtree(old)
                freed += size
                self.space_pruned_bytes += size
                self._marker("space-prune", run=str(old), freed_bytes=size)
            except OSError:
                continue
        if free + freed < self.min_free_bytes:
            self.stop_reason = "disk-floor"
            self._marker(
                "disk-stop",
                free_bytes=free,
                freed_bytes=freed,
                floor_bytes=self.min_free_bytes,
            )

    def _beat(self) -> None:
        assert self.log is not None
        now = time.monotonic()
        mono = mono_ns()
        skew = boot_ns() - mono
        if self.last_skew is not None:
            delta = skew - self.last_skew
            if abs(delta) > 1_000_000_000:
                self.suspend_count += 1
                self.suspend_gap_ns += delta
                self._marker("suspend-gap", "clock skew jumped; the machine was suspended", gap_ms=delta // 1_000_000)
        self.last_skew = skew

        lid = lid_state()
        if lid != self.lid:
            self.lid = lid
            self._marker("lid", state=lid)

        prev_mono, prev_bytes, prev_hid, prev_ev = self.prev_snapshot
        span = max(now - prev_mono, 0.001)
        scale = 60.0 / span
        self.rates = {
            "bytes_per_min": round((self.log.bytes - prev_bytes) * scale, 1),
            "hid_frames_per_min": round((self.hid_frames - prev_hid) * scale, 1),
            "ev_events_per_min": round((self.ev_events - prev_ev) * scale, 1),
        }
        self.prev_snapshot = (now, self.log.bytes, self.hid_frames, self.ev_events)
        self.beats += 1

        try:
            # "u" is the wall clock (UTC epoch ns) sampled in the same beat as the
            # monotonic "t", so any event can be mapped back to a human time even
            # when the only annotation is "I touched the screen at 14:32".
            self._emit({"t": mono, "k": "b", "sk": skew, "lid": lid, "hb": self.beats, "u": time.time_ns()})
        except OSError as exc:
            print(f"[capture] write failed: {exc}", file=sys.stderr)
            self.stop_reason = "write-error"
            return

        status = {
            "version": VERSION,
            "schema": SCHEMA,
            "pid": os.getpid(),
            "label": self.label,
            "run_dir": str(self.run_dir),
            "started_utc": self.started_utc,
            "now_utc": utc_now_iso(),
            "uptime_s": round(now - self.started_mono, 1),
            "segment": self.log.seg_index,
            "events": self.log.events,
            "bytes": self.log.bytes,
            "segment_bytes": self.log.seg_bytes,
            "run_bytes": self.log.run_bytes(),
            "rotations": self.rotations,
            "pruned_segments": self.log.pruned_segments,
            "hid_frames": self.hid_frames,
            "ev_events": self.ev_events,
            "key_events": self.key_events,
            "markers": self.marker_count,
            "last_marker": self.last_marker,
            "suspend_count": self.suspend_count,
            "suspend_gap_s": round(self.suspend_gap_ns / 1e9, 1),
            "rates": self.rates,
            "sources": {
                s.path: {
                    "kind": s.kind,
                    "open": s.open_,
                    "opens": s.opens,
                    "eofs": s.eofs,
                    "errors": s.errors,
                    "last_error": s.last_error,
                    "backoff_s": round(s.backoff, 2),
                }
                for s in self.sources
            },
            "stop_reason": self.stop_reason,
        }
        self.log.write_status(status)

    # -- main loop ----------------------------------------------------------
    def _reopen(self) -> None:
        for src in self.sources:
            if src.open_:
                continue
            err = src.open()
            if err:
                continue
            self.sel.register(src.fd, selectors.EVENT_READ, src)
            self._marker("device-reopen", path=src.path, opens=src.opens)

    def _shutdown(self) -> None:
        self.sel.close()
        for src in self.sources:
            src.close()
        if self.control_fh:
            try:
                self.control_fh.close()
            except OSError:
                pass

    def run(self) -> int:
        args = self.args
        self.run_dir = make_run_dir(self.out, self.label, args.keep_runs)
        removed = _prune_old_runs(self.out, args.keep_runs, keep=self.run_dir)
        if removed:
            print(f"[capture] pruned old runs: {', '.join(removed)}", file=sys.stderr)

        self.log = Log(
            self.run_dir,
            compress=not args.no_compress,
            rotate_bytes=args.rotate_bytes,
            rotate_seconds=args.rotate_seconds,
            max_bytes=args.max_bytes,
            keep_runs=args.keep_runs,
            quiet=args.quiet,
        )
        self._install_signals()
        self._open_sources()

        meta = {
            "schema": SCHEMA,
            "version": VERSION,
            "label": self.label,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "started_utc": self.started_utc,
            "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
            "devices": self.devices,
            "python": sys.version.split()[0],
            "kernel": os.uname().release,
            "clock": "CLOCK_MONOTONIC nanoseconds, stamped at read() time",
            "legend": {
                "k": {"h": "hidraw read", "e": "evdev input_event", "m": "marker", "b": "heartbeat"},
                "ev_types": {str(k): v for k, v in EV_LEGEND.items()},
                "abs_codes": {hex(k): v for k, v in ABS_LEGEND.items()},
            },
            "notes": [
                "h lines are one per read(); hid-multitouch returns one report per read",
                "e lines are raw input_event fields; the kernel timestamp is dropped on purpose",
                "sk in b lines is BOOTTIME-MONOTONIC; a jump means the machine suspended",
            ],
        }
        (self.run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        self._open_control_fifo()
        self.log.rotate("start")

        hid_src = next((s for s in self.sources if s.kind == "hid"), None)
        ev_src = next((s for s in self.sources if s.kind == "ev" and not s.key_only), None)
        if hid_src and not hid_src.open_:
            self._marker("hidraw-unavailable", hid_src.last_error or "open failed", path=hid_src.path)
            print(
                "[capture] WARNING: hidraw unavailable — recording evdev only. "
                "Raw reports are the ground truth; run scripts/install-access.sh.",
                file=sys.stderr,
            )
        if ev_src and not ev_src.open_:
            self._marker("evdev-unavailable", ev_src.last_error or "open failed", path=ev_src.path)

        print(f"[capture] run      {self.run_dir}", file=sys.stderr)
        for src in self.sources:
            state = "open" if src.open_ else "CLOSED"
            print(f"[capture] source   {src.kind:<4} {src.path} ({state})", file=sys.stderr)
        print(f"[capture] markers  echo 'touch-start' > {self.run_dir / 'control'}", file=sys.stderr)

        try:
            while self.stop_reason is None:
                now = time.monotonic()
                if self.deadline and now >= self.deadline:
                    self.stop_reason = "duration"
                    break
                if self.rotate_now:
                    self.rotate_now = False
                    self.log.rotate("sighup")
                    self.rotations += 1
                if self.reopen_now:
                    self.reopen_now = False
                    self._reopen()

                timeouts = [self.next_beat]
                if self.deadline:
                    timeouts.append(self.deadline)
                for src in self.sources:
                    if not src.open_:
                        timeouts.append(src.next_open)
                timeout = max(min(timeouts) - now, 0.0)
                try:
                    events = self.sel.select(timeout)
                except InterruptedError:
                    events = []

                for key, _mask in events:
                    data = key.data
                    if data == "ctl":
                        try:
                            raw = key.fileobj.recv(64)
                        except OSError:
                            raw = b""
                        for byte in raw:
                            if byte in (signal.SIGTERM & 0xFF, signal.SIGINT & 0xFF):
                                self.stop_reason = "signal"
                            elif byte == (signal.SIGHUP & 0xFF):
                                self.rotate_now = True
                            elif byte == (signal.SIGUSR1 & 0xFF):
                                self.reopen_now = True
                        if self.stop_reason:
                            break
                    elif data == "fifo":
                        self._read_control(key.fd)
                    else:
                        src = data
                        if src.kind == "hid":
                            self._read_hid(src)
                        else:
                            self._read_ev(src)

                if self.stop_reason:
                    break

                now = time.monotonic()
                for src in self.sources:
                    if not src.open_ and now >= src.next_open:
                        self._reopen()
                if now >= self.next_beat:
                    self._beat()
                    self.next_beat = now + self.heartbeat
                rotated = self.log.maybe_rotate()
                if rotated:
                    self.rotations += 1
                self.log.prune_if_needed()
                if now >= self.next_space_check:
                    self._enforce_free_space()
                    self.next_space_check = now + 30.0
        except KeyboardInterrupt:
            self.stop_reason = "keyboard-interrupt"
        finally:
            try:
                self.log.prune_if_needed()
            except OSError:
                pass
            self._marker("stop", self.stop_reason or "unknown")
            uptime = time.monotonic() - self.started_mono
            end_status = {
                "version": VERSION,
                "schema": SCHEMA,
                "pid": os.getpid(),
                "label": self.label,
                "run_dir": str(self.run_dir),
                "started_utc": self.started_utc,
                "now_utc": utc_now_iso(),
                "uptime_s": round(uptime, 1),
                "segment": self.log.seg_index,
                "events": self.log.events,
                "bytes": self.log.bytes,
                "rotations": self.rotations,
                "pruned_segments": self.log.pruned_segments,
                "hid_frames": self.hid_frames,
                "ev_events": self.ev_events,
                "key_events": self.key_events,
                "markers": self.marker_count,
                "last_marker": self.last_marker,
                "suspend_count": self.suspend_count,
                "rates": self.rates,
                "stop_reason": self.stop_reason,
                "finished": True,
            }
            self.log.write_status(end_status)
            meta.update(
                {
                    "stopped_utc": end_status["now_utc"],
                    "uptime_s": end_status["uptime_s"],
                    "stop_reason": self.stop_reason,
                    "totals": {
                        "events": self.log.events,
                        "bytes": self.log.bytes,
                        "segments": self.log.seg_index + 1,
                        "hid_frames": self.hid_frames,
                        "ev_events": self.ev_events,
                        "key_events": self.key_events,
                        "markers": self.marker_count,
                        "rotations": self.rotations,
                        "pruned_segments": self.log.pruned_segments,
                        "suspend_count": self.suspend_count,
                        "suspend_gap_s": round(self.suspend_gap_ns / 1e9, 1),
                    },
                }
            )
            try:
                self.log.close()
            except OSError:
                pass
            (self.run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
            self._shutdown()

        summary = {
            "run_dir": str(self.run_dir),
            "stop_reason": self.stop_reason,
            "uptime_s": round(uptime, 1),
            "events": self.log.events,
            "bytes": self.log.bytes,
            "hid_frames": self.hid_frames,
            "ev_events": self.ev_events,
            "markers": self.marker_count,
            "rotations": self.rotations,
        }
        print(json.dumps(summary, indent=2))
        return 0


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _iter_segments(run_dir: Path):
    idx_path = run_dir / "index.jsonl"
    names: list[tuple[int, str]] = []
    if idx_path.is_file():
        for line in idx_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                info = json.loads(line)
            except json.JSONDecodeError:
                continue
            names.append((info.get("seg", -1), info.get("file", "")))
    if not names:
        for path in sorted(run_dir.glob("seg-*.jsonl*")):
            m = re.match(r"seg-(\d+)\.jsonl(\.gz)?$", path.name)
            if m:
                names.append((int(m.group(1)), path.name))
    seen = set()
    for seg, name in sorted(names):
        path = run_dir / name
        if not path.is_file() or path.name in seen:
            continue
        seen.add(path.name)
        yield seg, path


def verify(run_dir: Path, as_json: bool = False) -> int:
    problems: list[str] = []
    run_dir = run_dir.expanduser().resolve()
    meta_path = run_dir / "meta.json"
    if not meta_path.is_file():
        print(f"verify: {run_dir} has no meta.json", file=sys.stderr)
        return 3
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"verify: meta.json is not valid JSON: {exc}", file=sys.stderr)
        return 3

    kinds: dict[str, int] = {}
    lines = 0
    bad_json = 0
    bad_hex = 0
    non_monotonic = 0
    markers: list[dict] = []
    first_t = last_t = None
    prev_t = None
    big_gaps: list[tuple[int, int]] = []
    seg_report = []

    for seg, path in _iter_segments(run_dir):
        opener = gzip.open if path.suffix == ".gz" else open
        seg_lines = 0
        seg_first = seg_last = None
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    seg_lines += 1
                    lines += 1
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        bad_json += 1
                        continue
                    kind = obj.get("k", "?")
                    kinds[kind] = kinds.get(kind, 0) + 1
                    t = obj.get("t")
                    if not isinstance(t, int):
                        bad_json += 1
                        continue
                    if kind == "h":
                        data = obj.get("d", "")
                        if not isinstance(data, str) or len(data) % 2 or not re.fullmatch(r"[0-9a-f]*", data):
                            bad_hex += 1
                    if kind == "m":
                        markers.append(obj)
                    if seg_first is None:
                        seg_first = t
                    seg_last = t
                    if first_t is None:
                        first_t = t
                    if prev_t is not None:
                        if t < prev_t:
                            non_monotonic += 1
                        elif t - prev_t > 2_000_000_000:
                            big_gaps.append((prev_t, t))
                    prev_t = t
                    last_t = t
        except OSError as exc:
            problems.append(f"cannot read {path.name}: {exc}")
            continue
        seg_report.append(
            {
                "seg": seg,
                "file": path.name,
                "events": seg_lines,
                "bytes": path.stat().st_size,
                "span_s": round((seg_last - seg_first) / 1e9, 1) if seg_first and seg_last else 0.0,
            }
        )

    uptime = float(meta.get("uptime_s") or 0.0)
    span_s = (last_t - first_t) / 1e9 if first_t is not None and last_t is not None else 0.0
    disk_bytes = sum(p.stat().st_size for p in run_dir.glob("seg-*.jsonl*") if p.is_file())
    per_hour = (disk_bytes / span_s * 3600) if span_s > 0 else 0.0

    if bad_json:
        problems.append(f"{bad_json} lines are not valid JSON events")
    if bad_hex:
        problems.append(f"{bad_hex} hidraw lines have an invalid hex payload")
    if non_monotonic:
        problems.append(f"{non_monotonic} timestamps go backwards")
    if kinds.get("h", 0) == 0 and meta.get("devices", {}).get("hidraw"):
        problems.append("no hidraw frames recorded (was the ACL installed?)")
    if kinds.get("e", 0) == 0:
        problems.append("no evdev events recorded")
    if lines == 0:
        problems.append("no events at all")

    ev_syn = 0
    # SYN_REPORT count needs a second pass; cheap enough and it is the 1:1 check
    for _seg, path in _iter_segments(run_dir):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"k":"e"' in line and '"ty":0' in line and '"c":0' in line:
                        ev_syn += 1
        except OSError:
            pass

    report = {
        "run_dir": str(run_dir),
        "label": meta.get("label"),
        "started_utc": meta.get("started_utc"),
        "stopped_utc": meta.get("stopped_utc"),
        "stop_reason": meta.get("stop_reason"),
        "uptime_s": uptime,
        "data_span_s": round(span_s, 1),
        "lines": lines,
        "kinds": kinds,
        "hid_frames": kinds.get("h", 0),
        "ev_events": kinds.get("e", 0),
        "ev_syn_reports": ev_syn,
        "markers": len(markers),
        "marker_names": sorted({m.get("n", "?") for m in markers}),
        "gaps_over_2s": len(big_gaps),
        "disk_bytes": disk_bytes,
        "projected_bytes_per_hour": round(per_hour),
        "segments": seg_report,
        "problems": problems,
    }

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"run        {run_dir}")
        print(f"label      {report['label']}   {report['started_utc']} -> {report['stopped_utc']} ({report['stop_reason']})")
        print(f"data span  {report['data_span_s']:.1f} s of events over {uptime:.1f} s uptime")
        print(f"events     {lines} ({', '.join(f'{k}={v}' for k, v in sorted(kinds.items()))})")
        print(f"frames     hidraw={kinds.get('h', 0)}  evdev-events={kinds.get('e', 0)}  SYN_REPORT={ev_syn}")
        print(f"markers    {len(markers)} {report['marker_names']}")
        print(f"disk       {disk_bytes / 1e6:.2f} MB  ->  {per_hour / 1e6:.1f} MB/hour at this activity level")
        print(f"segments   {len(seg_report)}")
        for seg in seg_report:
            print(f"  {seg['file']:<26} {seg['events']:>9} events  {seg['bytes'] / 1e6:>7.2f} MB  {seg['span_s']:>9.1f} s")
        if big_gaps:
            print(f"gaps       {len(big_gaps)} interval(s) over 2 s:")
            for a, b in big_gaps[:10]:
                print(f"  {(b - a) / 1e9:.1f} s gap after t={a}")
        if problems:
            print("PROBLEMS")
            for p in problems:
                print(f"  - {p}")
        else:
            print("ok         every event line parsed, timestamps monotonic")

    text = [json.dumps(report, indent=2)] if as_json else []
    try:
        (run_dir / "verify.txt").write_text("\n".join(text) + "\n" if text else "", encoding="utf-8")
    except OSError:
        pass
    return 0 if not problems else 3


# --------------------------------------------------------------------------- #
# selftest — end to end against synthetic FIFOs
# --------------------------------------------------------------------------- #
def selftest(duration: float = 2.0, keep: bool = False) -> int:
    """Feed synthetic hidraw + evdev streams through the real recorder."""
    import shutil
    import subprocess
    import tempfile
    import threading

    tmp = Path(tempfile.mkdtemp(prefix="phantom-selftest-", dir="/tmp"))
    hid_fifo = tmp / "hid0"
    ev_fifo = tmp / "ev0"
    out = tmp / "corpus"
    os.mkfifo(hid_fifo, 0o600)
    os.mkfifo(ev_fifo, 0o600)

    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "run",
            "--hidraw",
            str(hid_fifo),
            "--event",
            str(ev_fifo),
            "--keyboard",
            "off",
            "--out",
            str(out),
            "--label",
            "selftest",
            "--no-compress",
            "--heartbeat",
            "1",
            "--duration",
            str(duration),
            "--quiet",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # a real HID multitouch frame: slot0 tracking id 1, x 100, y 200, w 8, cnt 1
    frame = bytes([0x01, 0x00, 0x64, 0x00, 0xC8, 0x00, 0x08, 0x00, 0x01, 0x00])
    ev = INPUT_EVENT.pack
    seq = [
        (EV_ABS, 0x2F, 0),
        (EV_ABS, 0x39, 1),
        (EV_ABS, 0x35, 100),
        (EV_ABS, 0x36, 200),
        (EV_ABS, 0x32, 8),
        (EV_SYN, 0, 0),
    ]

    def pump(path: Path, payload: bytes, rate: float, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        with open(path, "wb", buffering=0) as fh:
            while time.monotonic() < deadline:
                fh.write(payload)
                time.sleep(1.0 / rate)

    def wait_fifo_reader(path: Path) -> None:
        # the recorder opens the FIFO with O_RDWR|O_NONBLOCK, so a plain open
        # for writing is enough once the run directory exists
        for _ in range(200):
            try:
                with open(path, "wb", buffering=0):
                    return
            except OSError:
                time.sleep(0.02)

    time.sleep(0.4)
    hid_payload = frame * 4
    ev_payload = b"".join(ev(0, 0, ty, code, value) for ty, code, value in seq)
    t1 = threading.Thread(target=pump, args=(hid_fifo, hid_payload, 40.0, duration + 0.2), daemon=True)
    t2 = threading.Thread(target=pump, args=(ev_fifo, ev_payload, 40.0, duration + 0.2), daemon=True)
    # writers block in open() until a reader appears; the recorder's run loop
    # opens both sources before we get here, so start them and join
    t1.start()
    t2.start()
    try:
        rc = proc.wait(timeout=duration + 20)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -9
    t1.join(timeout=2)
    t2.join(timeout=2)
    out_s, err_s = proc.communicate()

    runs = sorted(out.glob("selftest-*"))
    ok = rc == 0 and bool(runs)
    detail: dict = {"recorder_rc": rc, "runs": [r.name for r in runs]}
    if runs:
        report = _verify_json(runs[-1])
        detail["verify"] = report
        checks = {
            "events_recorded": report.get("lines", 0) > 0,
            "hid_frames": report.get("hid_frames", 0) > 0,
            "ev_events": report.get("ev_events", 0) > 0,
            "syn_reports": report.get("ev_syn_reports", 0) > 0,
            "timestamps_monotonic": "timestamps go backwards" not in " ".join(report.get("problems", [])),
            "no_problems": not report.get("problems"),
            "meta_closed": bool(report.get("stopped_utc")),
        }
        detail["checks"] = checks
        ok = ok and all(checks.values())
    else:
        detail["stderr"] = err_s[-2000:]
    detail["stdout"] = out_s[-800:]
    detail["stderr_tail"] = err_s[-1200:]
    detail["ok"] = ok
    print(json.dumps(detail, indent=2))
    if keep:
        print(f"selftest artifacts kept in {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


def _verify_json(run_dir: Path) -> dict:
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        verify(run_dir, as_json=True)
    try:
        return json.loads(buf.getvalue())
    except json.JSONDecodeError:
        return {"lines": 0, "problems": ["verify produced no JSON"]}


# --------------------------------------------------------------------------- #
# status / devices
# --------------------------------------------------------------------------- #
def _latest_run(out: Path) -> Path | None:
    runs = []
    for child in out.glob("*"):
        meta = child / "meta.json"
        if not meta.is_file():
            continue
        try:
            started = json.loads(meta.read_text(encoding="utf-8")).get("started_utc", "")
        except (OSError, json.JSONDecodeError):
            started = ""
        runs.append((started, child))
    runs.sort(key=lambda item: item[0])
    return runs[-1][1] if runs else None


def show_status(run_dir: Path | None, out: Path, as_json: bool) -> int:
    run_dir = run_dir or _latest_run(out)
    if run_dir is None:
        print(f"status: no runs under {out}", file=sys.stderr)
        return 3
    path = Path(run_dir) / "status.json"
    if not path.is_file():
        print(f"status: {path} missing", file=sys.stderr)
        return 3
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"status: {path} is not valid JSON: {exc}", file=sys.stderr)
        return 3
    age = time.time() - path.stat().st_mtime
    st["status_age_s"] = round(age, 1)
    st["alive"] = age < max(3 * float(st.get("rates") and 1 or 1) * 10, 30) and not st.get("finished")
    if as_json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"run        {st.get('run_dir')}")
    print(f"label      {st.get('label')}  pid {st.get('pid')}  uptime {st.get('uptime_s')} s")
    print(f"segment    {st.get('segment')}  events {st.get('events')}  run bytes {st.get('run_bytes')}")
    print(f"rates      {st.get('rates')}")
    print(f"frames     hidraw={st.get('hid_frames')} evdev={st.get('ev_events')} markers={st.get('markers')}")
    print(f"marker     {st.get('last_marker')}")
    print(f"status age {st['status_age_s']} s" + ("  (finished)" if st.get("finished") else ""))
    return 0


def cmd_devices(vidpid: str, as_json: bool) -> int:
    found = discover(vidpid)
    found["keyboard"] = find_keyboard()
    for key in ("hidraw", "event"):
        path = found.get(key)
        if path:
            st = os.stat(path)
            found[f"{key}_mode"] = oct(st.st_mode & 0o777)
            found[f"{key}_readable"] = os.access(path, os.R_OK)
    if as_json:
        print(json.dumps(found, indent=2))
        return 0 if found.get("hidraw") and found.get("event") else 3
    print(f"target     {found['vidpid']}  name={found.get('name')}  driver={found.get('driver')}")
    print(f"hidraw     {found.get('hidraw')}  mode={found.get('hidraw_mode')}  readable={found.get('hidraw_readable')}")
    print(f"evdev      {found.get('event')}  name={found.get('event_name')}  mt={found.get('event_is_mt')} "
          f"mode={found.get('event_mode')}  readable={found.get('event_readable')}")
    print(f"keyboard   {found.get('keyboard')}")
    for msg in found.get("errors", []):
        print(f"error      {msg}")
    return 0 if found.get("hidraw") and found.get("event") else 3


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    repo = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(
        prog="capture.py",
        description="Dual-source phantom-touch recorder (hidraw + evdev), continuous and rotating.",
    )
    p.add_argument("--version", action="version", version=f"capture.py {VERSION} (schema {SCHEMA})")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="record until signalled or --duration elapses")
    r.add_argument("--label", default="run", help="run label used in the directory name (default: run)")
    r.add_argument("--out", default=str(repo / "corpus"), help="corpus directory (default: <repo>/corpus)")
    r.add_argument("--vidpid", default=DEFAULT_VIDPID, help=f"digitizer VID:PID (default {DEFAULT_VIDPID})")
    r.add_argument("--hidraw", default=None, help="override the hidraw node (skips discovery)")
    r.add_argument("--event", default=None, help="override the evdev node (skips discovery)")
    r.add_argument("--keyboard", default="auto", help="'auto' | 'off' | an event node for typing markers")
    r.add_argument("--duration", type=float, default=0.0, help="stop after N seconds (0 = run until signalled)")
    r.add_argument("--heartbeat", type=float, default=DEFAULT_HEARTBEAT, help="heartbeat/status interval in seconds")
    r.add_argument("--rotate-bytes", type=int, default=DEFAULT_ROTATE_BYTES, help="rotate a segment at N bytes")
    r.add_argument("--rotate-seconds", type=float, default=DEFAULT_ROTATE_SECONDS, help="rotate a segment after N s")
    r.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="prune oldest segments past N bytes (0 = never)")
    r.add_argument("--keep-runs", type=int, default=DEFAULT_KEEP_RUNS, help="keep at most N run directories (0 = all)")
    r.add_argument("--no-compress", action="store_true", help="keep rotated segments uncompressed")
    r.add_argument("--quiet", action="store_true", help="do not echo markers to stderr")

    d = sub.add_parser("devices", help="print the identity-matched device nodes")
    d.add_argument("--vidpid", default=DEFAULT_VIDPID)
    d.add_argument("--json", action="store_true")

    v = sub.add_parser("verify", help="validate a run directory and print measured rates")
    v.add_argument("run_dir", nargs="?", default="latest", help="run directory or 'latest'")
    v.add_argument("--out", default=str(repo / "corpus"), help="corpus directory used for 'latest'")
    v.add_argument("--json", action="store_true")

    s = sub.add_parser("status", help="print the latest heartbeat")
    s.add_argument("--run", default=None, help="run directory (default: newest under --out)")
    s.add_argument("--out", default=str(repo / "corpus"))
    s.add_argument("--json", action="store_true")

    st = sub.add_parser("selftest", help="end-to-end proof against synthetic FIFOs")
    st.add_argument("--duration", type=float, default=2.0)
    st.add_argument("--keep", action="store_true", help="keep the temporary artifacts")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "run":
        try:
            return Recorder(args).run()
        except SystemExit as exc:
            print(exc.code if isinstance(exc.code, str) else f"exit {exc.code}", file=sys.stderr)
            return 1
    if args.cmd == "devices":
        return cmd_devices(args.vidpid, args.json)
    if args.cmd == "verify":
        run_dir = _latest_run(Path(args.out)) if args.run_dir == "latest" else Path(args.run_dir)
        if run_dir is None:
            print(f"verify: no runs under {args.out}", file=sys.stderr)
            return 3
        return verify(run_dir, args.json)
    if args.cmd == "status":
        return show_status(Path(args.run) if args.run else None, Path(args.out), args.json)
    if args.cmd == "selftest":
        return selftest(args.duration, args.keep)
    return 2


if __name__ == "__main__":
    sys.exit(main())
