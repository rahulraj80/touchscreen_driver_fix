#!/usr/bin/env python3
"""End-to-end test for tools/capture.py — no device access, no root.

Feeds synthetic HID reports and evdev events into two FIFOs and drives the real
recorder through them, then asserts on what landed on disk. This proves the
plumbing (dual-source ordering, monotonic timestamps, markers via the control
FIFO, heartbeat, rotation, gzip, verify) without needing read access to the
damaged panel — which matters because that access needs one root step.

Run:  python3 tests/test_capture.py [-v]
Exit: 0 all pass, 1 failures.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CAPTURE = REPO / "tools" / "capture.py"

INPUT_EVENT = struct.Struct("qqHHi")  # timeval(16) + type(2) + code(2) + value(4)
EV_SYN, EV_ABS, EV_KEY = 0, 3, 1
ABS_MT_SLOT, ABS_MT_TRACKING_ID = 0x2F, 0x39
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
ABS_MT_TOUCH_MAJOR = 0x30

# A real 1-finger frame from this digitizer: slot 0, tracking id 1, x=100, y=200,
# touch major=8. Byte layout follows the report descriptor in docs/hardware.md.
HID_FRAME = bytes([0x01, 0x00, 0x64, 0x00, 0xC8, 0x00, 0x08, 0x00, 0x01, 0x00])

VERBOSE = "-v" in sys.argv
failures: list[str] = []
checks = 0

# The tests double as a standalone script (`python3 tests/test_capture.py`)
# and as pytest tests. Standalone `main()` makes its own temp dir and calls the
# functions directly; under pytest the same functions need the `tmp` argument
# supplied, so provide it as a fixture when pytest is available.
try:  # pragma: no cover - exercised only under pytest
    import pytest

    @pytest.fixture
    def tmp():
        d = Path(tempfile.mkdtemp(prefix="capture-test-"))
        try:
            yield d
        finally:
            shutil.rmtree(d, ignore_errors=True)
except ImportError:  # pragma: no cover
    pass


def check(name: str, cond: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if cond:
        if VERBOSE:
            print(f"  PASS  {name}")
    else:
        failures.append(f"{name}{': ' + detail if detail else ''}")
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))


def pump(path: Path, chunks: list[bytes], rate_hz: float, seconds: float) -> None:
    """Write chunks round-robin into a FIFO for `seconds`."""
    deadline = time.monotonic() + seconds
    interval = 1.0 / rate_hz
    i = 0
    try:
        with open(path, "wb", buffering=0) as fh:
            while time.monotonic() < deadline:
                fh.write(chunks[i % len(chunks)])
                i += 1
                time.sleep(interval)
    except (BrokenPipeError, OSError):
        # The recorder closed its end first — normal when it exits on --duration.
        return


def read_events(run_dir: Path) -> list[dict]:
    events: list[dict] = []
    for path in sorted(run_dir.glob("seg-*.jsonl*")):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                events.append(json.loads(line))
    return events


# Populated by test_run() so the standalone main() can report its summary while
# the test itself stays a plain (None-returning) pytest test.
last_run: dict = {}


def test_run(tmp: Path, duration: float = 3.0) -> None:
    hid_fifo = tmp / "hid0"
    ev_fifo = tmp / "ev0"
    out = tmp / "corpus"
    for fifo in (hid_fifo, ev_fifo):
        os.mkfifo(fifo, 0o600)

    proc = subprocess.Popen(
        [
            sys.executable, str(CAPTURE), "run",
            "--hidraw", str(hid_fifo),
            "--event", str(ev_fifo),
            "--keyboard", "off",
            "--out", str(out),
            "--label", "selftest",
            "--heartbeat", "0.5",
            "--rotate-bytes", "4096",
            "--no-compress",
            "--duration", str(duration),
            "--quiet",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    # Wait for the recorder to create its run directory and the control FIFO.
    run_dir = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        runs = sorted(out.glob("selftest-*")) if out.exists() else []
        if runs and (runs[-1] / "control").exists():
            run_dir = runs[-1]
            break
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    check("run directory created", run_dir is not None)
    if run_dir is None:
        proc.kill()
        out_s, err_s = proc.communicate()
        last_run.clear()
        last_run.update({"failed": True, "stderr": err_s[-2000:], "stdout": out_s[-2000:]})
        return

    ev_stream = b"".join(
        INPUT_EVENT.pack(0, 0, ty, code, val)
        for ty, code, val in [
            (EV_ABS, ABS_MT_SLOT, 0),
            (EV_ABS, ABS_MT_TRACKING_ID, 1),
            (EV_ABS, ABS_MT_POSITION_X, 100),
            (EV_ABS, ABS_MT_POSITION_Y, 200),
            (EV_ABS, ABS_MT_TOUCH_MAJOR, 8),
            (EV_SYN, 0, 0),
        ]
    )
    key_stream = b"".join(
        INPUT_EVENT.pack(0, 0, EV_KEY, code, 1) for code in (30, 31, 32)
    )

    writers = [
        threading.Thread(target=pump, args=(hid_fifo, [HID_FRAME], 60.0, duration + 0.4), daemon=True),
        threading.Thread(target=pump, args=(ev_fifo, [ev_stream], 60.0, duration + 0.4), daemon=True),
    ]
    for t in writers:
        t.start()

    # Marker through the control FIFO, exactly as an operator would type it.
    time.sleep(1.0)
    try:
        with open(run_dir / "control", "wb", buffering=0) as ctl:
            ctl.write(b"phantom-start near top-left corner\n")
            ctl.write(b"phantom-end\n")
    except OSError as exc:
        check("marker via control FIFO", False, str(exc))

    # SIGUSR1 asks for an immediate reopen; SIGTERM stops cleanly.
    time.sleep(1.0)
    try:
        proc.send_signal(signal.SIGUSR1)
    except OSError:
        pass

    try:
        out_s, err_s = proc.communicate(timeout=duration + 20)
    except subprocess.TimeoutExpired:
        proc.kill()
        out_s, err_s = proc.communicate()
        check("recorder exited on its own", False, "timeout")
    for t in writers:
        t.join(timeout=2)

    rc = proc.returncode
    events = read_events(run_dir)
    hid = [e for e in events if e.get("k") == "h"]
    ev = [e for e in events if e.get("k") == "e"]
    markers = [e for e in events if e.get("k") == "m"]
    beats = [e for e in events if e.get("k") == "b"]
    meta = json.loads((run_dir / "meta.json").read_text())
    index = [json.loads(l) for l in (run_dir / "index.jsonl").read_text().splitlines() if l.strip()]

    check("exit code 0", rc == 0, f"rc={rc} stderr={err_s[-300:]}")
    check("hidraw frames captured", len(hid) > 0, f"count={len(hid)}")
    check("evdev events captured", len(ev) > 0, f"count={len(ev)}")
    check("SYN_REPORT present", any(e.get("ty") == EV_SYN for e in ev))
    check("marker from control FIFO", any(m.get("n") == "phantom-start" for m in markers),
          f"markers={[m.get('n') for m in markers]}")
    check("marker text preserved",
          any("top-left" in str(m.get("x", "")) for m in markers))
    check("heartbeats written", len(beats) >= 2, f"count={len(beats)}")
    check("meta closed", bool(meta.get("stopped_utc")))
    check("meta has device paths", meta.get("devices", {}).get("hidraw") == str(hid_fifo))
    check("index lists segments", len(index) >= 1, f"count={len(index)}")
    check("rotation happened", len(index) > 1, f"segments={len(index)}")
    check("status.json written", (run_dir / "status.json").is_file())

    times = [e["t"] for e in events if "t" in e]
    check("timestamps monotonic", all(b >= a for a, b in zip(times, times[1:])))
    check("timestamps are nanosecond monotonic",
          times and all(t > 10**9 for t in times), f"first={times[0] if times else None}")

    hid_hex = hid[0]["d"] if hid else ""
    check("hid payload is valid hex", len(hid_hex) % 2 == 0 and all(c in "0123456789abcdef" for c in hid_hex))
    check("hid frame length preserved", len(hid_hex) // 2 == len(HID_FRAME))

    verify = subprocess.run(
        [sys.executable, str(CAPTURE), "verify", str(run_dir), "--json"],
        capture_output=True, text=True,
    )
    report = {}
    try:
        report = json.loads(verify.stdout)
    except json.JSONDecodeError:
        check("verify --json produced JSON", False, verify.stderr[-300:])
    check("verify exit 0", verify.returncode == 0,
          f"rc={verify.returncode} problems={report.get('problems')}")
    check("verify sees no problems", not report.get("problems"), str(report.get("problems")))
    check("verify counted both sources",
          report.get("hid_frames", 0) > 0 and report.get("ev_events", 0) > 0)

    status = subprocess.run(
        [sys.executable, str(CAPTURE), "status", "--run", str(run_dir), "--json"],
        capture_output=True, text=True,
    )
    check("status command works", status.returncode == 0, status.stderr[-300:])

    last_run.clear()
    last_run.update({
        "run_dir": str(run_dir),
        "hid": len(hid),
        "ev": len(ev),
        "markers": [m.get("n") for m in markers],
        "beats": len(beats),
        "segments": len(index),
    })


def test_gzip(tmp: Path, duration: float = 2.0) -> None:
    """Same recorder with compression on: rotated segments must end up .gz."""
    hid_fifo = tmp / "hidgz"
    ev_fifo = tmp / "evgz"
    out = tmp / "corpus-gz"
    for fifo in (hid_fifo, ev_fifo):
        os.mkfifo(fifo, 0o600)
    proc = subprocess.Popen(
        [
            sys.executable, str(CAPTURE), "run",
            "--hidraw", str(hid_fifo), "--event", str(ev_fifo),
            "--keyboard", "off", "--out", str(out), "--label", "gz",
            "--heartbeat", "0.5", "--rotate-bytes", "2048",
            "--duration", str(duration), "--quiet",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    run_dir = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        runs = sorted(out.glob("gz-*")) if out.exists() else []
        if runs and (runs[-1] / "control").exists():
            run_dir = runs[-1]
            break
        time.sleep(0.05)
    check("gzip run directory created", run_dir is not None)
    if run_dir is None:
        proc.kill()
        proc.communicate()
        return
    threads = [
        threading.Thread(target=pump, args=(hid_fifo, [HID_FRAME], 60.0, duration + 0.4), daemon=True),
        threading.Thread(target=pump, args=(ev_fifo, [INPUT_EVENT.pack(0, 0, EV_SYN, 0, 0)], 60.0, duration + 0.4), daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        proc.communicate(timeout=duration + 20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
    for t in threads:
        t.join(timeout=2)
    gz_files = sorted(run_dir.glob("seg-*.jsonl.gz"))
    plain_files = sorted(run_dir.glob("seg-*.jsonl"))
    check("compressed segments exist", len(gz_files) > 0, f"gz={len(gz_files)} plain={len(plain_files)}")
    total = len(read_events(run_dir))
    check("compressed run still parses", total > 0, f"events={total}")


def test_missing_device(tmp: Path) -> None:
    """A dead source must not kill the recorder; it must log and keep going."""
    ev_fifo = tmp / "evonly"
    os.mkfifo(ev_fifo, 0o600)
    out = tmp / "corpus-missing"
    proc = subprocess.Popen(
        [
            sys.executable, str(CAPTURE), "run",
            "--hidraw", str(tmp / "does-not-exist"),
            "--event", str(ev_fifo), "--keyboard", "off",
            "--out", str(out), "--label", "partial",
            "--heartbeat", "0.5", "--duration", "2", "--quiet",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    run_dir = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        runs = sorted(out.glob("partial-*")) if out.exists() else []
        if runs and (runs[-1] / "control").exists():
            run_dir = runs[-1]
            break
        time.sleep(0.05)
    check("partial run directory created", run_dir is not None)
    if run_dir is None:
        proc.kill()
        proc.communicate()
        return
    t = threading.Thread(target=pump, args=(ev_fifo, [INPUT_EVENT.pack(0, 0, EV_SYN, 0, 0)], 60.0, 2.4), daemon=True)
    t.start()
    try:
        out_s, err_s = proc.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        proc.kill()
        out_s, err_s = proc.communicate()
    t.join(timeout=2)
    events = read_events(run_dir)
    markers = [e.get("n") for e in events if e.get("k") == "m"]
    check("survived a missing hidraw source", proc.returncode == 0, f"rc={proc.returncode}")
    check("logged hidraw-unavailable marker", "hidraw-unavailable" in markers, str(markers))
    check("still captured evdev", any(e.get("k") == "e" for e in events))
    verify = subprocess.run([sys.executable, str(CAPTURE), "verify", str(run_dir), "--json"],
                            capture_output=True, text=True)
    report = json.loads(verify.stdout)
    check("verify flags the missing hidraw source",
          any("hidraw" in p for p in report.get("problems", [])), str(report.get("problems")))


def main() -> int:
    if not CAPTURE.is_file():
        print(f"missing {CAPTURE}", file=sys.stderr)
        return 1
    print("test_capture.py — synthetic dual-source end-to-end")

    tmp = Path(tempfile.mkdtemp(prefix="phantom-capture-test-"))
    keep = "--keep" in sys.argv
    try:
        print("\n[1] dual-source run, markers, rotation")
        test_run(tmp)
        if VERBOSE or not last_run.get("failed"):
            print(f"    {last_run}")
        print("\n[2] gzip compression path")
        test_gzip(tmp)
        print("\n[3] degraded start (one source missing)")
        test_missing_device(tmp)
    finally:
        if keep:
            print(f"\nartifacts kept in {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("\nFAILURES")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ok — recorder verified end to end without device access")
    return 0


if __name__ == "__main__":
    sys.exit(main())
