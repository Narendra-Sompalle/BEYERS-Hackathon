import os
from typing import Any, Dict

from seeder.seed_logs import seed_logs
from seeder.seed_metrics import seed_metrics


def lambda_handler(event: Any, context: Any) -> Dict[str, Any]:
    bucket = os.environ.get("MOCK_DATA_BUCKET")
    if not bucket:
        raise EnvironmentError("MOCK_DATA_BUCKET environment variable is not set")
    region = os.environ.get("AWS_REGION", "us-east-1")
    logs_result = seed_logs(bucket=bucket, region=region)
    metrics_result = seed_metrics(bucket=bucket, region=region)
    return {
        "status": "success",
        "bucket": bucket,
        "region": region,
        "logs": logs_result,
        "metrics": metrics_result,
    }
