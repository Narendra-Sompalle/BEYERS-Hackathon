import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Tuple, Sequence

import boto3

SERVICES = ["checkout-service", "payment-service", "inventory-service"]
LOG_GROUP_TEMPLATE = "/bayer/{service}"

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def _parse_timestamp_to_millis(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e14:
            seconds = ts / 1e9
        elif ts > 1e12:
            seconds = ts / 1000.0
        else:
            seconds = ts
        return int(seconds * 1000)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            try:
                parsed = datetime.fromtimestamp(float(normalized), tz=timezone.utc)
            except ValueError:
                return None
        return int(parsed.timestamp() * 1000)
    return None


def _read_json_from_s3(s3_client: boto3.client, bucket: str, key: str) -> Sequence[Any]:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    payload = response["Body"].read()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse %s: %s", key, exc)
        return []
    if isinstance(data, dict):
        return data.get("logs") or data.get("entries") or data.get("records") or []
    if isinstance(data, list):
        return data
    return []


def _ensure_log_group(logs_client: boto3.client, group_name: str, cache: set[str]) -> bool:
    if group_name in cache:
        return False
    try:
        logs_client.create_log_group(logGroupName=group_name)
        cache.add(group_name)
        return True
    except logs_client.exceptions.ResourceAlreadyExistsException:
        cache.add(group_name)
        return False


def _ensure_log_stream(logs_client: boto3.client, group_name: str, stream_name: str, cache: set[Tuple[str, str]]) -> None:
    key = (group_name, stream_name)
    if key in cache:
        return
    try:
        logs_client.create_log_stream(logGroupName=group_name, logStreamName=stream_name)
    except logs_client.exceptions.ResourceAlreadyExistsException:
        pass
    cache.add(key)


def _describe_sequence_token(logs_client: boto3.client, group_name: str, stream_name: str) -> str | None:
    response = logs_client.describe_log_streams(
        logGroupName=group_name,
        logStreamNamePrefix=stream_name,
        limit=1,
    )
    streams = response.get("logStreams")
    if not streams:
        return None
    return streams[0].get("uploadSequenceToken")


def _push_events(
    logs_client: boto3.client,
    group_name: str,
    stream_name: str,
    events: Sequence[Dict[str, Any]],
    sequence_token: str | None,
) -> str | None:
    payload = {
        "logGroupName": group_name,
        "logStreamName": stream_name,
        "logEvents": events,
    }
    if sequence_token:
        payload["sequenceToken"] = sequence_token
    try:
        response = logs_client.put_log_events(**payload)
        return response.get("nextSequenceToken")
    except logs_client.exceptions.InvalidSequenceTokenException:
        token = _describe_sequence_token(logs_client, group_name, stream_name)
        if token:
            payload["sequenceToken"] = token
            response = logs_client.put_log_events(**payload)
            return response.get("nextSequenceToken")
        raise


def seed_logs(bucket: str, region: str = "us-east-1") -> Dict[str, int]:
    s3_client = boto3.client("s3", region_name=region)
    logs_client = boto3.client("logs", region_name=region)
    log_groups_created = 0
    events_pushed = 0
    log_group_cache: set[str] = set()
    stream_cache: set[Tuple[str, str]] = set()
    entries_by_stream: dict[Tuple[str, str], list[Dict[str, Any]]] = defaultdict(list)

    for service in SERVICES:
        prefix = f"logs/{service}/"
        paginator = s3_client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj.get("Key")
                if not key:
                    continue
                for entry in _read_json_from_s3(s3_client, bucket, key):
                    if not isinstance(entry, dict):
                        continue
                    instance_id = (
                        entry.get("instance_id")
                        or entry.get("instance")
                        or entry.get("host")
                        or entry.get("host_id")
                        or "unknown"
                    )
                    timestamp = _parse_timestamp_to_millis(entry.get("timestamp"))
                    if timestamp is None:
                        continue
                    message = json.dumps(entry, default=str)
                    group_name = LOG_GROUP_TEMPLATE.format(service=service)
                    stream_name = str(instance_id)
                    entries_by_stream[(group_name, stream_name)].append(
                        {"timestamp": timestamp, "message": message}
                    )
    for (group_name, stream_name), events in entries_by_stream.items():
        events.sort(key=lambda item: item["timestamp"])
        if _ensure_log_group(logs_client, group_name, log_group_cache):
            log_groups_created += 1
        _ensure_log_stream(logs_client, group_name, stream_name, stream_cache)
        sequence_token = _describe_sequence_token(logs_client, group_name, stream_name)
        next_token = _push_events(logs_client, group_name, stream_name, events, sequence_token)
        if next_token:
            logger.debug("Updated sequence token for %s/%s", group_name, stream_name)
        events_pushed += len(events)
    return {"log_groups_created": log_groups_created, "events_pushed": events_pushed}
