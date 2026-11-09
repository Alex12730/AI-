"""Run measured local ComfyUI generations and optionally admit the exact preset."""
from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from video_workstation.adapters.base import GenerationRequest
from video_workstation.adapters.local_command import LocalCommandAdapter
from video_workstation.adapters.comfyui import MODELS, ComfyClient
from video_workstation.config import Settings
from video_workstation.db import Database
from video_workstation.models import ModelProfile
from video_workstation.qc import probe_media
from video_workstation.services.models import record_benchmark
from video_workstation.thermal import read_thermal_status, thermal_dispatch_allowed


class MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_ulong),
        ("memory_load", ctypes.c_ulong),
        ("total_phys", ctypes.c_ulonglong),
        ("avail_phys", ctypes.c_ulonglong),
        ("total_page_file", ctypes.c_ulonglong),
        ("avail_page_file", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong),
        ("avail_virtual", ctypes.c_ulonglong),
        ("avail_extended_virtual", ctypes.c_ulonglong),
    ]


def system_ram_used_gb() -> float:
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("Unable to read system memory")
    return (status.total_phys - status.avail_phys) / 1024**3


def gpu_sample() -> tuple[float, int]:
    completed = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used,temperature.gpu", "--format=csv,noheader,nounits"],
        shell=False, check=True, capture_output=True, text=True, timeout=10,
    )
    memory, temperature = completed.stdout.strip().splitlines()[0].split(",")
    return float(memory.strip()) / 1024, int(temperature.strip())


def wait_for_thermal_dispatch(settings: Settings, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    announced = False
    while not thermal_dispatch_allowed(settings):
        if time.monotonic() >= deadline:
            raise RuntimeError("Temperature protection did not recover before the cooldown timeout")
        if not announced:
            status = read_thermal_status(settings.data_dir / "thermal-status.json")
            print(
                f"cooldown: waiting for automatic safe recovery "
                f"(level={status.get('level')}, temperature={status.get('temperature_c')})",
                flush=True,
            )
            announced = True
        time.sleep(5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--duration", type=float, choices=(5, 10, 15, 20), required=True)
    parser.add_argument("--aspect-ratio", choices=("16:9", "9:16"), required=True)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--cooldown-timeout", type=float, default=1800)
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--seed-base", type=int)
    args = parser.parse_args()
    if not 1 <= args.runs <= 10:
        parser.error("--runs must be between 1 and 10")
    if args.record and args.runs != 10:
        parser.error("--record requires exactly 10 runs")
    if args.cooldown_timeout <= 0:
        parser.error("--cooldown-timeout must be positive")
    seed_base = args.seed_base if args.seed_base is not None else secrets.randbelow(2**63 - args.runs)
    if not 0 <= seed_base < 2**64 - args.runs:
        parser.error("--seed-base is outside the supported range")

    settings = Settings()
    database = Database(settings)
    directory = settings.data_dir / "benchmark-runs" / args.model / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory.mkdir(parents=True, exist_ok=False)
    report_path = directory / "runs.json"
    results: list[dict] = []

    with database.session() as session:
        profile = session.scalar(select(ModelProfile).where(ModelProfile.slug == args.model))
        if profile is None or not profile.runtime_config_json.get("command"):
            raise ValueError("Model is not configured")
        command = profile.runtime_config_json["command"]

        for index in range(args.runs):
            wait_for_thermal_dispatch(settings, args.cooldown_timeout)
            comfy_url = command[command.index("--comfy-url") + 1]
            queue = ComfyClient(comfy_url).request("/queue")
            if queue.get("queue_running") or queue.get("queue_pending"):
                raise RuntimeError("ComfyUI must be idle before every benchmark run")

            stop = threading.Event()
            samples: list[tuple[float, float, int]] = []

            def sample_metrics() -> None:
                while not stop.is_set():
                    try:
                        vram, temperature = gpu_sample()
                        samples.append((system_ram_used_gb(), vram, temperature))
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
                    stop.wait(1)

            monitor = threading.Thread(target=sample_metrics, daemon=True)
            monitor.start()
            output = directory / f"run-{index + 1:02d}.mp4"
            request = GenerationRequest(
                task_id=f"benchmark-{args.model}-{index + 1}",
                prompt=("A red ball rolls slowly across a wooden table in a quiet room. "
                        "Natural room ambience, no dialogue, no speaking subject."),
                negative_prompt="" if args.model == MODELS[2] else "blurry, distorted, text, watermark",
                output_path=output,
                duration_seconds=args.duration,
                aspect_ratio=args.aspect_ratio,
                seed=seed_base + index,
            )
            started = time.monotonic()
            result = LocalCommandAdapter(command, args.model).execute(request)
            elapsed = time.monotonic() - started
            stop.set()
            monitor.join(timeout=5)
            qc = probe_media(output, expected_duration=args.duration, ffprobe_bin=args.ffprobe) if result.success else None
            success = bool(result.success and qc and qc.passed)
            row = {
                "success": success,
                "oom": result.error_class == "oom",
                "corrupt": bool(result.success and qc and not qc.passed),
                "elapsed_seconds": round(elapsed, 2),
                "vram_peak_gb": round(max((item[1] for item in samples), default=0), 2),
                "system_ram_peak_gb": round(max((item[0] for item in samples), default=0), 2),
                "temperature_c": max((item[2] for item in samples), default=0),
                "av_sync_applicable": False,
                "av_sync_reason": "ambient audio only; no speaking subject",
                "seed": request.seed,
                "output": str(output),
                "qc_errors": qc.errors if qc else [],
                "error_class": result.error_class,
                "error_message": result.error_message,
            }
            results.append(row)
            report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"run {index + 1}/{args.runs}: " + ("passed" if success else "failed"), flush=True)
            if not success:
                break

        if args.record:
            admitted = record_benchmark(profile, args.duration, args.aspect_ratio, results)
            print("admission: " + ("passed" if admitted else "failed"), flush=True)
            if not admitted:
                raise SystemExit(1)

    print(str(report_path), flush=True)


if __name__ == "__main__":
    main()
