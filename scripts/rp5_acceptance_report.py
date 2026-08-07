#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, TextIO


TEMPERATURE_RE = re.compile(r"temp=([0-9]+(?:\.[0-9]+)?)'C")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the RP5 balanced-efficiency window.")
    parser.add_argument("files", nargs="+", help="Trend JSONL files, or - for standard input")
    parser.add_argument("--since", required=True, type=parse_timestamp)
    parser.add_argument("--hours", type=float, default=72.0)
    parser.add_argument("--interval-minutes", type=float, default=5.0)
    parser.add_argument("--minimum-coverage", type=float, default=0.9)
    parser.add_argument("--max-average-cpu-pct", type=float, default=2.0)
    parser.add_argument("--max-median-temperature", type=float, default=56.0)
    parser.add_argument("--max-p95-temperature", type=float, default=60.0)
    parser.add_argument("--max-peak-temperature", type=float, default=65.0)
    parser.add_argument("--max-endpoint-p95-ms", type=float, default=28.0)
    args = parser.parse_args()

    samples = load_samples(args.files)
    report = evaluate(
        samples,
        since=args.since,
        hours=args.hours,
        interval_minutes=args.interval_minutes,
        minimum_coverage=args.minimum_coverage,
        limits={
            "average_cpu_pct": args.max_average_cpu_pct,
            "median_temperature_c": args.max_median_temperature,
            "p95_temperature_c": args.max_p95_temperature,
            "peak_temperature_c": args.max_peak_temperature,
            "endpoint_p95_ms": args.max_endpoint_p95_ms,
        },
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["complete"]:
        return 2
    return 0 if report["passed"] else 1


def parse_timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset")
    return timestamp


def load_samples(files: Iterable[str]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for name in files:
        if name == "-":
            samples.extend(read_jsonl(sys.stdin, "stdin"))
            continue
        with Path(name).open(encoding="utf-8") as stream:
            samples.extend(read_jsonl(stream, name))
    return samples


def read_jsonl(stream: TextIO, source: str) -> list[dict[str, Any]]:
    samples = []
    for line_number, line in enumerate(stream, 1):
        if not line.strip():
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid JSON in {source}:{line_number}: {exc}") from exc
        if not isinstance(sample, dict):
            raise SystemExit(f"invalid sample in {source}:{line_number}: expected an object")
        samples.append(sample)
    return samples


def evaluate(
    samples: Iterable[dict[str, Any]],
    *,
    since: datetime,
    hours: float,
    interval_minutes: float,
    minimum_coverage: float,
    limits: dict[str, float],
) -> dict[str, Any]:
    until = since + timedelta(hours=hours)
    selected = []
    for sample in samples:
        raw_timestamp = sample.get("timestamp")
        if not isinstance(raw_timestamp, str):
            continue
        timestamp = datetime.fromisoformat(raw_timestamp)
        if since <= timestamp <= until:
            selected.append((timestamp, sample))
    selected.sort(key=lambda item: item[0])

    expected_samples = max(1, math.ceil(hours * 60 / interval_minutes))
    minimum_samples = math.ceil(expected_samples * minimum_coverage)
    coverage_ok = len(selected) >= minimum_samples
    end_reached = bool(selected) and selected[-1][0] >= until - timedelta(minutes=interval_minutes)
    complete = coverage_ok and end_reached

    cpu_values: list[float] = []
    temperatures: list[float] = []
    throttled_samples = 0
    endpoint_latencies: dict[str, list[float]] = defaultdict(list)
    endpoint_failures: dict[str, int] = defaultdict(int)

    for _, sample in selected:
        cpu_pct = ((sample.get("k3sNode") or {}).get("cpuPct"))
        if isinstance(cpu_pct, str) and cpu_pct.endswith("%"):
            try:
                cpu_values.append(float(cpu_pct[:-1]))
            except ValueError:
                pass

        temperature = ((sample.get("hardware") or {}).get("temperature"))
        if isinstance(temperature, str) and (match := TEMPERATURE_RE.fullmatch(temperature)):
            temperatures.append(float(match.group(1)))

        throttled = ((sample.get("hardware") or {}).get("throttled"))
        if isinstance(throttled, str):
            try:
                throttled_samples += int(throttled.split("=", 1)[-1], 16) != 0
            except ValueError:
                throttled_samples += 1

        for name, result in (sample.get("endpoints") or {}).items():
            if not isinstance(result, dict):
                endpoint_failures[str(name)] += 1
                continue
            status = result.get("status")
            latency = result.get("latencyMs")
            if not isinstance(status, int) or not 200 <= status < 300:
                endpoint_failures[str(name)] += 1
            elif isinstance(latency, (int, float)):
                endpoint_latencies[str(name)].append(float(latency))

    endpoint_names = sorted(set(endpoint_latencies) | set(endpoint_failures))
    endpoint_metrics = {
        name: {
            "failures": endpoint_failures[name],
            "p95_latency_ms": percentile(endpoint_latencies[name], 0.95),
            "samples": len(endpoint_latencies[name]),
        }
        for name in endpoint_names
    }

    metrics = {
        "average_cpu_pct": mean(cpu_values),
        "median_temperature_c": percentile(temperatures, 0.5),
        "p95_temperature_c": percentile(temperatures, 0.95),
        "peak_temperature_c": max(temperatures) if temperatures else None,
        "throttled_samples": throttled_samples,
        "endpoints": endpoint_metrics,
    }
    checks = {
        "average_cpu": value_at_most(metrics["average_cpu_pct"], limits["average_cpu_pct"]),
        "median_temperature": value_at_most(
            metrics["median_temperature_c"], limits["median_temperature_c"]
        ),
        "p95_temperature": value_at_most(
            metrics["p95_temperature_c"], limits["p95_temperature_c"]
        ),
        "peak_temperature": value_at_most(
            metrics["peak_temperature_c"], limits["peak_temperature_c"]
        ),
        "no_throttling": throttled_samples == 0,
        "endpoint_latency": bool(endpoint_metrics)
        and all(
            result["p95_latency_ms"] is not None
            and result["p95_latency_ms"] <= limits["endpoint_p95_ms"]
            for result in endpoint_metrics.values()
        ),
        "no_endpoint_failures": bool(endpoint_metrics)
        and all(result["failures"] == 0 for result in endpoint_metrics.values()),
    }
    return {
        "checks": checks,
        "complete": complete,
        "limits": limits,
        "metrics": metrics,
        "passed": complete and all(checks.values()),
        "window": {
            "coverage_pct": round(100 * len(selected) / expected_samples, 2),
            "expected_samples": expected_samples,
            "first_sample": selected[0][0].isoformat() if selected else None,
            "last_sample": selected[-1][0].isoformat() if selected else None,
            "minimum_samples": minimum_samples,
            "sample_count": len(selected),
            "since": since.isoformat(),
            "until": until.isoformat(),
        },
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * fraction) - 1]


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def value_at_most(value: float | None, maximum: float) -> bool:
    return value is not None and value <= maximum


if __name__ == "__main__":
    raise SystemExit(main())
