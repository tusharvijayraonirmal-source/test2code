#!/usr/bin/env python3
# ai_code_review_engine/resource_monitor.py
"""
Dynamic RAM and hardware usage monitor for the AI code
review workflow.

A small background sampler runs continuously for the
whole job and records a live system RAM / CPU / load
timeline, so usage is captured dynamically instead of
only at hand-placed points. Steps mark themselves with
`--snapshot`, and the final report aggregates everything:
dynamic hardware detection, per-step peaks and deltas
from the sampled timeline, and total consumption.

Usage (from the repository root):

    python3 ai_code_review_engine/resource_monitor.py --start
    python3 ai_code_review_engine/resource_monitor.py --snapshot <step>
    python3 ai_code_review_engine/resource_monitor.py --stop
    python3 ai_code_review_engine/resource_monitor.py --final

Files written to the current directory:

    resource_usage_log.jsonl     samples + step markers
    resource_usage_summary.json  final total-consumption report
    resource_monitor.pid         sampler pid (removed on stop)

Standard library only, so no extra packages are
required on the runner.

The sampling interval can be tuned with the
RESOURCE_MONITOR_INTERVAL environment variable
(default 5 seconds).
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

LOG_FILE = "resource_usage_log.jsonl"
SUMMARY_FILE = "resource_usage_summary.json"
PID_FILE = "resource_monitor.pid"

# On common x86 runners HZ=100, so 1 jiffy = 10 ms.
JIFFY_MS = 10
DEFAULT_INTERVAL_SECONDS = 5


def _interval_seconds() -> int:

    try:
        value = int(
            os.getenv(
                "RESOURCE_MONITOR_INTERVAL",
                str(DEFAULT_INTERVAL_SECONDS),
            )
        )

        return max(1, value)

    except ValueError:
        return DEFAULT_INTERVAL_SECONDS


# ==================================================
# Read live metrics (dynamic detection)
# ==================================================

def _read_meminfo() -> dict:
    """Return /proc/meminfo values in kB."""

    info: dict = {}

    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:

            for line in f:

                key, _, rest = line.partition(":")

                if not rest.strip():
                    continue

                info[key.strip()] = int(rest.split()[0])

    except OSError:
        pass

    return info


def _cpu_time_ms() -> int:
    """
    Cumulative busy CPU time for all cores,
    read from the first "cpu " line of /proc/stat.
    """

    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:

            for line in f:

                if line.startswith("cpu "):

                    parts = [int(v) for v in line.split()[1:]]

                    # user + nice + system + iowait + irq
                    # + softirq + steal (excludes idle)
                    busy = sum(parts[:3]) + sum(parts[4:])

                    return busy * JIFFY_MS

    except OSError:
        pass

    return 0


def _load_average():
    try:
        return os.getloadavg()

    except OSError:
        return (0.0, 0.0, 0.0)


def _hardware_info() -> dict:

    model = "unknown"

    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:

            for line in f:

                if line.lower().startswith("model name"):

                    model = line.split(":", 1)[1].strip()

                    break

    except OSError:
        pass

    mem_total_mb = _read_meminfo().get("MemTotal", 0) // 1024

    disk = None

    try:
        usage = shutil.disk_usage(os.getcwd())

        disk = {
            "total_gb": round(usage.total / (1024 ** 3), 2),
            "used_gb": round(usage.used / (1024 ** 3), 2),
            "free_gb": round(usage.free / (1024 ** 3), 2),
        }

    except OSError:
        pass

    return {
        "cpu_model": model,
        "cpu_cores": os.cpu_count() or 0,
        "ram_total_mb": mem_total_mb,
        "disk": disk,
    }


# ==================================================
# Log file helpers
# ==================================================

def _read_records() -> list:

    records: list = []

    if not os.path.exists(LOG_FILE):
        return records

    with open(LOG_FILE, "r", encoding="utf-8") as f:

        for line in f:

            line = line.strip()

            if not line:
                continue

            try:
                records.append(json.loads(line))

            except json.JSONDecodeError:
                continue

    return records


def _append_record(record: dict) -> None:

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _read_sampler_pid():
    """Return the running sampler pid, or None."""

    if not os.path.exists(PID_FILE):
        return None

    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip() or 0)

    except (ValueError, OSError):
        return None


def _remove_pid_file() -> None:

    try:
        os.remove(PID_FILE)

    except OSError:
        pass


def _sampler_running(pid) -> bool:

    if not pid:
        return False

    try:
        os.kill(pid, 0)

        return True

    except ProcessLookupError:
        return False

    except PermissionError:
        return True


# ==================================================
# Start / stop the background sampler
# ==================================================

def stop_sampler(wait_seconds: float = 5.0) -> None:

    pid = _read_sampler_pid()

    _remove_pid_file()

    if pid and _sampler_running(pid):

        try:
            os.kill(pid, signal.SIGTERM)

        except ProcessLookupError:
            return

        deadline = time.time() + wait_seconds

        while time.time() < deadline:

            if not _sampler_running(pid):
                break

            time.sleep(0.1)


def start() -> None:

    # Stop a leftover sampler from a previous run and
    # reset the log so each job starts with a clean
    # timeline.
    stop_sampler()

    open(LOG_FILE, "w", encoding="utf-8").close()

    interval = _interval_seconds()

    sampler = subprocess.Popen(
        [
            sys.executable,
            os.path.abspath(__file__),
            "--sample-loop",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(sampler.pid))

    print("======================================")
    print("RESOURCE MONITOR STARTED")
    print("======================================")
    print(f"Sampling interval  : {interval} s")
    print(f"Sampler PID        : {sampler.pid}")
    print(f"Timeline log file  : {LOG_FILE}")
    print("Hardware (live)    : " + _hardware_summary())


def _hardware_summary() -> str:

    hardware = _hardware_info()

    parts = [
        f"{hardware['cpu_cores']} x {hardware['cpu_model']}",
        f"{hardware['ram_total_mb']} MB RAM",
    ]

    if hardware["disk"]:
        parts.append(
            f"{hardware['disk']['total_gb']} GB disk"
        )

    return ", ".join(parts)


# ==================================================
# Background sampler loop
# ==================================================

def sample_loop() -> None:

    interval = _interval_seconds()

    while True:

        try:
            mem = _read_meminfo()

            total_kb = mem.get("MemTotal", 0)
            available_kb = mem.get("MemAvailable", total_kb)

            load = _load_average()

            _append_record(
                {
                    "type": "sample",
                    "ts": time.time(),
                    "ram_total_mb": total_kb // 1024,
                    "ram_used_mb": (total_kb - available_kb) // 1024,
                    "ram_available_mb": available_kb // 1024,
                    "cpu_time_ms": _cpu_time_ms(),
                    "load_1m": round(load[0], 2),
                }
            )

        except Exception:
            pass

        time.sleep(interval)


# ==================================================
# Snapshot at a major step (marker on the timeline)
# ==================================================

def snapshot(step: str) -> None:

    records = _read_records()

    samples = [
        r for r in records if r.get("type") == "sample"
    ]

    start_ts = records[0]["ts"] if records else time.time()

    mem = _read_meminfo()

    total_kb = mem.get("MemTotal", 0)
    available_kb = mem.get("MemAvailable", total_kb)

    total_mb = total_kb // 1024
    available_mb = available_kb // 1024
    used_mb = (total_kb - available_kb) // 1024

    record = {
        "type": "marker",
        "ts": time.time(),
        "step": step,
        "elapsed_s": round(time.time() - start_ts, 2),
        "ram_used_mb": used_mb,
        "ram_available_mb": available_mb,
    }

    # Peak values observed in the sampled window since
    # the previous marker (dynamic capture, not just the
    # instantaneous value at the step end).
    if samples:
        last_ts = (
            records[-1]["ts"]
            if records[-1].get("type") == "marker"
            else samples[0]["ts"]
        )

        window = [
            s for s in samples if s["ts"] > last_ts
        ]

        if window:
            record["window_peak_ram_mb"] = max(
                s["ram_used_mb"] for s in window
            )

            record["window_cpu_delta_ms"] = (
                window[-1]["cpu_time_ms"]
                - window[0]["cpu_time_ms"]
            )

    _append_record(record)

    print()
    print("======================================")
    print(f"RESOURCE USAGE: {step}")
    print("======================================")
    print(f"Elapsed so far     : {record['elapsed_s']} s")
    print(f"RAM used (system)  : {used_mb} MB / {total_mb} MB")
    print(f"RAM available      : {available_mb} MB")

    if "window_peak_ram_mb" in record:
        print(
            f"Peak RAM in window : "
            f"{record['window_peak_ram_mb']} MB "
            f"(sampled dynamically)"
        )

    if "window_cpu_delta_ms" in record:
        print(
            f"CPU time in window : "
            f"{record['window_cpu_delta_ms'] / 1000:.1f} s "
            f"(all cores)"
        )

    if not _sampler_running(_read_sampler_pid()):
        print("NOTE: background sampler is not running; "
              "step data is instantaneous only.")

    print(f"Timeline log file  : {LOG_FILE}")


# ==================================================
# Final total consumption log
# ==================================================

def final() -> None:

    stop_sampler()

    records = _read_records()

    samples = [
        r for r in records if r.get("type") == "sample"
    ]

    markers = [
        r for r in records if r.get("type") == "marker"
    ]

    if not records:

        print(
            "WARNING: no resource data recorded. "
            "Nothing to summarize."
        )

        return

    hardware = _hardware_info()

    # ------------------------------------------
    # Timeline totals (dynamic aggregation)
    # ------------------------------------------

    first_ts = records[0]["ts"]
    last_ts = records[-1]["ts"]

    wall_time_s = round(last_ts - first_ts, 2)

    peak_ram_mb = max(
        r.get("ram_used_mb", 0) for r in samples
    ) if samples else max(
        r.get("ram_used_mb", 0) for r in markers
    )

    if samples:
        avg_ram_mb = int(
            sum(
                s["ram_used_mb"] for s in samples
            )
            / len(samples)
        )

        cpu_delta_ms = (
            samples[-1]["cpu_time_ms"]
            - samples[0]["cpu_time_ms"]
        )

        cores = max(hardware["cpu_cores"], 1)

        cpu_util_pct = (
            round(
                cpu_delta_ms
                / (cores * wall_time_s * 1000),
                1,
            )
            * 100
            if wall_time_s
            else 0.0
        )

    else:
        avg_ram_mb = peak_ram_mb
        cpu_delta_ms = 0
        cpu_util_pct = 0.0

    # ------------------------------------------
    # Per-step table from markers + sampled windows
    # ------------------------------------------

    steps = []

    for index, marker in enumerate(markers):

        window_start = (
            markers[index - 1]["ts"]
            if index
            else first_ts
        )

        window = [
            s
            for s in samples
            if window_start < s["ts"] <= marker["ts"]
        ]

        entry = {
            "step": marker["step"],
            "elapsed_s": marker.get("elapsed_s", 0),
            "ram_used_mb": marker.get("ram_used_mb", 0),
            "window_peak_ram_mb": marker.get(
                "window_peak_ram_mb",
                max(
                    (s["ram_used_mb"] for s in window),
                    default=marker.get("ram_used_mb", 0),
                ),
            ),
            "window_cpu_ms": marker.get(
                "window_cpu_delta_ms",
                (
                    window[-1]["cpu_time_ms"]
                    - window[0]["cpu_time_ms"]
                ) if window else 0,
            ),
        }

        steps.append(entry)

    summary = {
        "hardware": hardware,
        "totals": {
            "wall_time_s": wall_time_s,
            "total_cpu_s": round(cpu_delta_ms / 1000, 1),
            "avg_cpu_utilization_pct": cpu_util_pct,
            "peak_ram_used_mb": peak_ram_mb,
            "avg_ram_used_mb": avg_ram_mb,
            "ram_total_mb": hardware["ram_total_mb"],
        },
        "steps": steps,
        "samples_recorded": len(samples),
    }

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    # ------------------------------------------
    # Total consumption log
    # ------------------------------------------

    print()
    print("======================================")
    print("RESOURCE USAGE SUMMARY (TOTAL)")
    print("======================================")
    print("Hardware (auto-detected):")
    print(f"  CPU : {hardware['cpu_cores']} x {hardware['cpu_model']}")
    print(f"  RAM : {hardware['ram_total_mb']} MB total")

    if hardware["disk"]:
        print(
            f"  Disk: "
            f"{hardware['disk']['total_gb']} GB total, "
            f"{hardware['disk']['used_gb']} GB used, "
            f"{hardware['disk']['free_gb']} GB free"
        )

    print()
    print("Total consumption:")
    print(f"  Wall time            : {wall_time_s} s")
    print(f"  Total CPU time       : "
          f"{summary['totals']['total_cpu_s']} s "
          f"(all cores, avg {cpu_util_pct}% of capacity)")
    print(f"  Peak RAM used        : {peak_ram_mb} MB")
    print(f"  Average RAM used     : {avg_ram_mb} MB")
    print(
        f"  Samples recorded   : "
        f"{len(samples)} "
        f"(@ {_interval_seconds()} s interval)"
    )

    if steps:

        print()
        print("Per step:")
        print(
            f"  {'step':<24} {'elapsed':>10} "
            f"{'peak RAM':>10} {'CPU in window':>14}"
        )

        for s in steps:
            print(
                f"  {s['step']:<24} "
                f"{s['elapsed_s']:>9.1f}s "
                f"{s['window_peak_ram_mb']:>9d}m "
                f"{s['window_cpu_ms'] / 1000:>13.1f}s"
            )

    print()
    print(f"Summary saved to: {SUMMARY_FILE}")
    print(f"Timeline log file: {LOG_FILE}")


# ==================================================
# Entry point
# ==================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description=(
            "Dynamic RAM and hardware usage monitor "
            "for the AI code review workflow"
        )
    )

    group = parser.add_mutually_exclusive_group(required=True)

    group.add_argument(
        "--start",
        action="store_true",
        help="Start the background sampling daemon",
    )

    group.add_argument(
        "--stop",
        action="store_true",
        help="Stop the background sampling daemon",
    )

    group.add_argument(
        "--snapshot",
        metavar="STEP",
        help="Mark the named major step on the timeline",
    )

    group.add_argument(
        "--final",
        action="store_true",
        help=(
            "Stop the sampler and print/save the total "
            "consumption summary"
        ),
    )

    group.add_argument(
        "--sample-loop",
        action="store_true",
        help=(
            "Internal: continuous sampling loop "
            "(started detached by --start)"
        ),
    )

    args = parser.parse_args()

    if args.start:
        start()

    elif args.stop:
        stop_sampler()

        print("Resource monitor sampler stopped.")

    elif args.snapshot:
        snapshot(args.snapshot)

    elif args.final:
        final()

    elif args.sample_loop:
        sample_loop()


if __name__ == "__main__":
    main()
