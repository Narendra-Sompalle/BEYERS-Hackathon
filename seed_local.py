"""
Local seeder — reads mock_data/ from disk and pushes to CloudWatch Logs + Metrics.
No S3 or Lambda required. Run with: python seed_local.py
"""
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple

import boto3

REGION = "us-east-1"
MOCK_DATA_DIR = Path(__file__).parent / "mock_data"
SERVICES = ["checkout-service", "payment-service", "inventory-service"]
LOG_GROUP_TEMPLATE = "/bayer/{service}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Time-shift logic ───────────────────────────────────────────────────────────
# Mock data timestamps are from 2026-02-06 13:45-14:35 UTC. If the current UTC
# time is before those, CloudWatch rejects them (>2h future). We compute an
# offset so the LATEST mock timestamp maps to (now_utc - 10 minutes).

def _compute_time_offset_ms() -> int:
    """Return milliseconds to ADD to every mock timestamp so they land in the past."""
    # Find the latest timestamp across all mock files
    latest_ms = 0
    for service in SERVICES:
        # logs
        log_dir = MOCK_DATA_DIR / "logs" / service
        if log_dir.exists():
            for f in log_dir.glob("*.json"):
                with open(f) as fh:
                    raw = json.load(fh)
                records = raw if isinstance(raw, list) else (raw.get("logs") or raw.get("entries") or raw.get("records") or [])
                for entry in records:
                    if isinstance(entry, dict):
                        ts = _parse_ts_millis(entry.get("timestamp"))
                        if ts and ts > latest_ms:
                            latest_ms = ts
        # metrics
        ts_file = MOCK_DATA_DIR / "metrics" / service / "timeseries.json"
        if ts_file.exists():
            with open(ts_file) as fh:
                data = json.load(fh)
            for metric in (data.get("metrics") or []):
                for pt in (metric.get("datapoints") or []):
                    ts = _parse_ts_millis(pt.get("timestamp"))
                    if ts and ts > latest_ms:
                        latest_ms = ts

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    target_latest_ms = now_ms - (10 * 60 * 1000)  # 10 min ago
    offset = target_latest_ms - latest_ms
    logger.info("Time offset: %+d ms (%+.1f minutes)", offset, offset / 60000)
    return offset

TIME_OFFSET_MS = None  # computed lazily in main


# ── Timestamp helpers ──────────────────────────────────────────────────────────

def _parse_ts_millis(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e14:
            s = ts / 1e9
        elif ts > 1e12:
            s = ts / 1000.0
        else:
            s = ts
        return int(s * 1000)
    if isinstance(value, str):
        n = value.strip()
        if not n:
            return None
        if n.endswith("Z"):
            n = n[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(n)
        except ValueError:
            try:
                parsed = datetime.fromtimestamp(float(n), tz=timezone.utc)
            except ValueError:
                return None
        return int(parsed.timestamp() * 1000)
    return None


def _parse_ts_datetime(value: Any) -> datetime | None:
    ms = _parse_ts_millis(value)
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


# ── Log seeding ────────────────────────────────────────────────────────────────

def _ensure_log_group(client, name, cache):
    if name in cache:
        return False
    try:
        client.create_log_group(logGroupName=name)
        logger.info("Created log group: %s", name)
        cache.add(name)
        return True
    except client.exceptions.ResourceAlreadyExistsException:
        cache.add(name)
        return False


def _ensure_log_stream(client, group, stream, cache):
    key = (group, stream)
    if key in cache:
        return
    try:
        client.create_log_stream(logGroupName=group, logStreamName=stream)
    except client.exceptions.ResourceAlreadyExistsException:
        pass
    cache.add(key)


def _get_sequence_token(client, group, stream):
    resp = client.describe_log_streams(logGroupName=group, logStreamNamePrefix=stream, limit=1)
    streams = resp.get("logStreams", [])
    return streams[0].get("uploadSequenceToken") if streams else None


def seed_logs():
    logs_client = boto3.client("logs", region_name=REGION)
    group_cache: set[str] = set()
    stream_cache: set[Tuple[str, str]] = set()
    entries_by_stream: dict[Tuple[str, str], list] = defaultdict(list)
    groups_created = 0
    events_pushed = 0

    for service in SERVICES:
        log_dir = MOCK_DATA_DIR / "logs" / service
        if not log_dir.exists():
            logger.warning("No log directory: %s", log_dir)
            continue
        for json_file in sorted(log_dir.glob("*.json")):
            with open(json_file) as f:
                raw = json.load(f)
            records = raw if isinstance(raw, list) else (raw.get("logs") or raw.get("entries") or raw.get("records") or [])
            for entry in records:
                if not isinstance(entry, dict):
                    continue
                instance_id = entry.get("instance_id") or entry.get("instance") or entry.get("host") or "unknown"
                ts = _parse_ts_millis(entry.get("timestamp"))
                if ts is None:
                    continue
                ts += TIME_OFFSET_MS  # shift to valid window
                group = LOG_GROUP_TEMPLATE.format(service=service)
                stream = str(instance_id)
                entries_by_stream[(group, stream)].append({"timestamp": ts, "message": json.dumps(entry, default=str)})

    for (group, stream), events in entries_by_stream.items():
        events.sort(key=lambda e: e["timestamp"])
        if _ensure_log_group(logs_client, group, group_cache):
            groups_created += 1
        _ensure_log_stream(logs_client, group, stream, stream_cache)
        # Push in batches of 10000 (CW limit)
        for i in range(0, len(events), 10000):
            batch = events[i:i + 10000]
            token = _get_sequence_token(logs_client, group, stream)
            payload = {"logGroupName": group, "logStreamName": stream, "logEvents": batch}
            if token:
                payload["sequenceToken"] = token
            try:
                logs_client.put_log_events(**payload)
            except logs_client.exceptions.InvalidSequenceTokenException:
                token = _get_sequence_token(logs_client, group, stream)
                if token:
                    payload["sequenceToken"] = token
                logs_client.put_log_events(**payload)
            events_pushed += len(batch)
        logger.info("Pushed %d events → %s / %s", len(events), group, stream)

    return groups_created, events_pushed


# ── Metric seeding ─────────────────────────────────────────────────────────────

def seed_metrics():
    cw = boto3.client("cloudwatch", region_name=REGION)
    services_seeded = 0
    datapoints_pushed = 0

    for service in SERVICES:
        ts_file = MOCK_DATA_DIR / "metrics" / service / "timeseries.json"
        if not ts_file.exists():
            logger.warning("No metrics file: %s", ts_file)
            continue
        with open(ts_file) as f:
            data = json.load(f)
        metrics = data.get("metrics") or data.get("data") or []
        if not metrics:
            continue
        seeded = False
        for metric in metrics:
            namespace = metric.get("namespace", "BEYERS")
            # AWS/* namespaces are reserved — remap to custom namespace
            if namespace.startswith("AWS/") or namespace == "CWAgent":
                namespace = f"Bayer/{service.replace('-', '_').title().replace('_', '')}"
            metric_name = metric.get("metric_name") or metric.get("name")
            if not metric_name:
                continue
            datapoints = metric.get("datapoints") or metric.get("timeseries") or []
            batch = []
            for point in datapoints:
                if not isinstance(point, dict):
                    continue
                ts = _parse_ts_datetime(point.get("timestamp"))
                val = point.get("value")
                if ts is None or val is None:
                    continue
                # Shift timestamp to valid window
                ts = ts + timedelta(milliseconds=TIME_OFFSET_MS)
                entry: Dict[str, Any] = {
                    "MetricName": metric_name,
                    "Dimensions": [
                        {"Name": "ServiceName", "Value": service},
                        {"Name": "Environment", "Value": "production"},
                    ],
                    "Timestamp": ts,
                    "Value": float(val),
                }
                unit = metric.get("unit")
                if unit:
                    entry["Unit"] = unit
                batch.append(entry)
                if len(batch) == 20:
                    cw.put_metric_data(Namespace=namespace, MetricData=batch)
                    datapoints_pushed += 20
                    batch = []
                    seeded = True
            if batch:
                cw.put_metric_data(Namespace=namespace, MetricData=batch)
                datapoints_pushed += len(batch)
                seeded = True
            logger.info("Pushed %s → %s/%s", metric_name, namespace, service)
        if seeded:
            services_seeded += 1

    return services_seeded, datapoints_pushed


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info("=== Starting local CloudWatch seeder ===")
    logger.info("Region: %s | Mock data: %s", REGION, MOCK_DATA_DIR)

    TIME_OFFSET_MS = _compute_time_offset_ms()

    logger.info("\n── Seeding CloudWatch Logs ──")
    groups, log_events = seed_logs()
    logger.info("Logs done: %d groups created, %d events pushed", groups, log_events)

    logger.info("\n── Seeding CloudWatch Metrics ──")
    svc_count, dp_count = seed_metrics()
    logger.info("Metrics done: %d services seeded, %d datapoints pushed", svc_count, dp_count)

    logger.info("\n=== Seeding complete ===")
