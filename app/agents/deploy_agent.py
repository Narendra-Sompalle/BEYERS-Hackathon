from google.adk import Agent
from google.adk.models.lite_llm import LiteLlm
from app.tools.github_deployments import get_github_deployments
from app.tools.deploy_correlator import correlate_deploy_to_incident
from app.tools.envelope import build_response_envelope
import datetime


def analyze_deployments(
    service: str, time_window: dict, anomaly_start: str = None
) -> dict:
    """Fetches GitHub commits and correlates them with the incident. Returns findings for analysis."""
    try:
        deployments = get_github_deployments(service, time_window)
    except Exception as e:
        return {"error": str(e)}

    ref_time = anomaly_start or time_window["end"]
    correlation_results = correlate_deploy_to_incident(deployments, ref_time)

    return {
        "deployments_found": len(deployments),
        "correlation_results": correlation_results,
        "service": service,
        "incident_id": time_window.get("incident_id", "INC-UNKNOWN"),
    }


def submit_deploy_response(incident_id: str, findings: list, summary: str) -> dict:
    """Submits the final response with the agent's generated summary."""
    start_time = datetime.datetime.now(datetime.timezone.utc)
    return build_response_envelope(
        agent_name="deploy_agent",
        incident_id=incident_id,
        findings=findings,
        start_time=start_time,
        summary=summary,
    )


deploy_agent = Agent(
    name="deploy_agent",
    model=LiteLlm(model="bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
    description="Analyzes GitHub commit history to identify risky deployments related to an incident. Give it the service name, time_window dict, and optional anomaly_start timestamp.",
    instruction="""You are the Deployment Intelligence Agent. When you receive a task:
1. Call `analyze_deployments` with the service, time_window (dict with "start", "end", "incident_id"), and optional anomaly_start.
2. Review the 'correlation_results', especially 'highest_risk_deploy' and any 'correlations'.
3. Analyze the commit messages and files changed to determine the risk.
4. Generate a professional summary (e.g., "Identified risky deployment [commit_id] by [user] changing [files]...").
5. Call `submit_deploy_response` with the 'correlations' list and your summary.
6. After responding, you will automatically return control to the Commander.
""",
    tools=[analyze_deployments, submit_deploy_response],
    output_key="deploy_findings",
)
