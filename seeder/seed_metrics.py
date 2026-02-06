import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
from botocore.exceptions import ClientError

SERVICES = ["checkout-service", "payment-service", "inventory-service"]
METRICS_PREFIX = "metrics/{service}/timeseries.json"

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def _parse_timestamp_to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1e14:
            seconds = timestamp / 1e9
        elif timestamp > 1e12:
            seconds = timestamp / 1000.0
        else:
            seconds = timestamp
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            try:
                return datetime.fromtimestamp(float(normalized), tz=timezone.utc)
            except ValueError:
                return None
    return None


def _read_timeseries_from_s3(s3_client: boto3.client, bucket: str, key: str) -> Dict[str, Any]:
    response = s3_client.get_object(Bucket=bucket, Key=key)
    payload = response["Body"].read()
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        logger.warning("Could not parse metrics payload %s: %s", key, exc)
        return {}


def seed_metrics(bucket: str, region: str = "us-east-1") -> Dict[str, int]:
    s3_client = boto3.client("s3", region_name=region)
    cloudwatch = boto3.client("cloudwatch", region_name=region)
    services_seeded = 0
    datapoints_pushed = 0
    for service in SERVICES:
        key = METRICS_PREFIX.format(service=service)
        try:
            payload = _read_timeseries_from_s3(s3_client, bucket, key)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                logger.info("Metrics file missing for %s at %s", service, key)
                continue
            raise
        metrics = payload.get("metrics") or payload.get("data") or []
        if not metrics:
            continue
        service_seeded = False
        for metric in metrics:
            namespace = metric.get("namespace") or "BEYERS"
            metric_name = metric.get("metric_name") or metric.get("name")
            if not metric_name:
                continue
            datapoints = metric.get("datapoints") or metric.get("timeseries") or []
            batch: list[Dict[str, Any]] = []
            for point in datapoints:
                if not isinstance(point, dict):
                    continue
                timestamp = _parse_timestamp_to_datetime(point.get("timestamp"))
                if not timestamp:
                    continue
                value = point.get("value")
                if value is None:
                    continue
                payload_entry: Dict[str, Any] = {
                    "MetricName": metric_name,
                    "Dimensions": [
                        {"Name": "ServiceName", "Value": service},
                        {"Name": "Environment", "Value": "production"},
                    ],
                    "Timestamp": timestamp,
                    "Value": value,
                }
                unit = metric.get("unit")
                if unit:
                    payload_entry["Unit"] = unit
                batch.append(payload_entry)
                datapoints_pushed += 1
                if len(batch) == 20:
                    cloudwatch.put_metric_data(Namespace=namespace, MetricData=batch)
                    batch = []
                service_seeded = True
            if batch:
                cloudwatch.put_metric_data(Namespace=namespace, MetricData=batch)
        if service_seeded:
            services_seeded += 1
    return {"services_seeded": services_seeded, "datapoints_pushed": datapoints_pushed}
