import os
import asyncio
import time
import boto3
from dotenv import load_dotenv

from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.models.lite_llm import LiteLlm

from app.tools.cloudwatch_logs import query_logs_insights
from app.tools.stack_parser import extract_stack_traces
from app.tools.envelope import build_response_envelope
import datetime

load_dotenv()

# --- Tool: diagnose_service_errors (from main — live CW query) ---


def diagnose_service_errors(service: str, lookback_minutes: int = 15) -> str:
    """
    SINGLE TOOL CALL:
    1. Calculates timestamps.
    2. Queries CloudWatch Logs for ERRORs.
    3. Returns ONLY the last 100 characters of the findings.
    """
    print(f"[LOGS_AGENT|TOOL] diagnose_service_errors: service={service}, lookback={lookback_minutes}min", flush=True)
    logs_client = boto3.client("logs", region_name=os.getenv("AWS_REGION", "us-east-1"))

    end_time = int(time.time())
    start_time = end_time - (lookback_minutes * 60)

    log_group = f"/bayer/{service}"
    query = "fields @timestamp, @message | filter @message like /ERROR/ | sort @timestamp desc | limit 3"

    try:
        start_res = logs_client.start_query(
            logGroupName=log_group,
            startTime=start_time,
            endTime=end_time,
            queryString=query,
        )
        query_id = start_res["queryId"]

        raw_logs = []
        for _ in range(10):
            response = logs_client.get_query_results(queryId=query_id)
            if response["status"] == "Complete":
                raw_logs = response.get("results", [])
                break
            time.sleep(1)

        if not raw_logs:
            return "RESULT: No ERROR logs found in the last 15 minutes."

        combined_text = ""
        for res in raw_logs:
            msg = next(
                (item["value"] for item in res if item["field"] == "@message"), ""
            )
            combined_text += f" | {msg}"

        snippet = combined_text[-100:].strip()
        return f"LAST_100_CHARS_OF_LOGS: {snippet}"

    except Exception as e:
        return f"ERROR: Could not fetch logs. {str(e)}"


# --- Tool: analyze_logs (structured Logs Insights query) ---


def analyze_logs(service: str, time_window: dict, filter_pattern: str = None) -> dict:
    """Fetches logs, summarizes errors, and extracts stack traces."""
    print(f"[LOGS_AGENT|TOOL] analyze_logs: service={service}, window={time_window.get('start')} to {time_window.get('end')}", flush=True)
    start_time = datetime.datetime.now(datetime.timezone.utc)

    try:
        logs = query_logs_insights(service, time_window, filter_pattern)
        print(f"[LOGS_AGENT|TOOL] CloudWatch Logs Insights returned {len(logs)} entries", flush=True)
    except Exception as e:
        print(f"[LOGS_AGENT|TOOL] ERROR querying logs: {e}", flush=True)
        return build_response_envelope(
            agent_name="logs_agent",
            incident_id=time_window.get("incident_id", "INC-UNKNOWN"),
            findings=[],
            start_time=start_time,
            error=str(e),
        )

    error_summary = {}
    sample_entries = []

    for entry in logs:
        error_code = entry.get("error_code", "UNKNOWN_ERROR")
        if error_code not in error_summary:
            error_summary[error_code] = {
                "count": 0,
                "first_seen": entry.get("@timestamp"),
                "last_seen": entry.get("@timestamp"),
            }
        error_summary[error_code]["count"] += 1
        error_summary[error_code]["last_seen"] = entry.get("@timestamp")

        if len(sample_entries) < 3:
            parsed_stack = extract_stack_traces(entry)
            if parsed_stack:
                entry["parsed_stack_trace"] = parsed_stack
                sample_entries.append(entry)

    findings = {
        "matched_entries": len(logs),
        "error_summary": error_summary,
        "sample_entries": sample_entries,
    }

    summary = None
    if logs:
        top_errors = sorted(
            error_summary.items(), key=lambda x: x[1]["count"], reverse=True
        )[:2]
        error_descriptions = [
            f"{count_info['count']}x {code}" for code, count_info in top_errors
        ]
        summary = f"Detected {len(logs)} error logs. Top issues: {', '.join(error_descriptions)}."
        print(f"[LOGS_AGENT|TOOL] Summary: {summary}", flush=True)

    return build_response_envelope(
        agent_name="logs_agent",
        incident_id=time_window.get("incident_id", "INC-UNKNOWN"),
        findings=[findings] if logs else [],
        start_time=start_time,
        summary=summary,
    )


# --- Agent Definition (A2A sub-agent of Commander) ---

logs_agent = LlmAgent(
    name="logs_agent",
    model=LiteLlm(model="bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
    description="Reads and analyzes CloudWatch Logs to identify errors and stack traces. Give it the service name, start time, end time, and incident_id.",
    instruction="""You are the Logs Intelligence Agent. When you receive a task:
1. Call `analyze_logs` with the service, time_window (dict with "start", "end", "incident_id"), and optional filter_pattern.
   OR call `diagnose_service_errors` with just the service name for a quick live query.
2. Review the results — focus on ERROR, FATAL, and WARN levels.
3. Summarize your findings as your final text response: error counts, dominant error code, stack trace root frames, and first/last seen timestamps.
4. After responding, you will automatically return control to the Commander.
""",
    tools=[analyze_logs, diagnose_service_errors],
    output_key="logs_findings",
)


# --- Test Runner (from main) ---


async def test_logs_agent():
    print("Running One-Shot Sub-Agent Test...")
    runner = InMemoryRunner(agent=logs_agent)
    query = "Diagnose the checkout-service."

    try:
        trajectory = await runner.run_debug(query, verbose=True)

        print("\n" + "=" * 60)
        print("ONE-SHOT VERIFICATION:")

        if isinstance(trajectory, list):
            final_text = (
                trajectory[-1].text
                if hasattr(trajectory[-1], "text")
                else str(trajectory[-1])
            )
        else:
            final_text = getattr(trajectory, "text", str(trajectory))

        print(final_text)
        print("=" * 60)

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(test_logs_agent())
