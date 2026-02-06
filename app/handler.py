"""Lambda entry point — receives EventBridge alarm events and runs the Commander agent.

Includes full execution tracing: agent transitions, tool calls, reasoning, and sub-agent delegation.
"""

import asyncio
import json
import logging
import smtplib
import time
from email.message import EmailMessage
from typing import Any, Dict

from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agents.commander import commander_agent

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
logger = logging.getLogger(__name__)

APP_NAME = "aic-commander"
USER_ID = "system"

# ── ANSI colors for local readability (no-op in Lambda CloudWatch) ─────────
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _trace(tag: str, msg: str, color: str = DIM):
    """Structured log line for both local and Lambda CloudWatch."""
    logger.info(f"{color}[{tag}]{RESET} {msg}")


def _truncate(text: str, max_len: int = 200) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


async def _run_commander(event: dict) -> dict:
    """Run the Commander agent with the alarm event, tracing every step."""
    runner = InMemoryRunner(agent=commander_agent, app_name=APP_NAME)

    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID
    )

    prompt = (
        "A CloudWatch alarm has fired. Here is the raw event:\n\n"
        f"```json\n{json.dumps(event, indent=2, default=str)}\n```\n\n"
        "Execute the full incident investigation: DETECT → PLAN → INVESTIGATE → DECIDE → REPORT."
    )
    content = types.Content(role="user", parts=[types.Part(text=prompt)])

    _trace("START", f"Session {session.id} | Invoking Commander agent", BOLD)

    final_text = ""
    current_agent = None
    step_count = 0
    t0 = time.time()

    async for ev in runner.run_async(
        user_id=USER_ID, session_id=session.id, new_message=content
    ):
        step_count += 1
        author = getattr(ev, "author", "?")
        actions = getattr(ev, "actions", None)
        transfer_to = getattr(actions, "transfer_to_agent", None) if actions else None
        escalate = getattr(actions, "escalate", None) if actions else None

        # ── Agent transition ──────────────────────────────────────
        if author != current_agent:
            if current_agent is not None:
                _trace("AGENT", f"<<< Exiting: {current_agent}", DIM)
            current_agent = author
            _trace("AGENT", f">>> Entering: {BOLD}{current_agent}{RESET}", CYAN)

        # ── Tool calls (function calls from LLM) ─────────────────
        func_calls = ev.get_function_calls()
        if func_calls:
            for fc in func_calls:
                args_str = json.dumps(
                    {k: _truncate(str(v)) for k, v in (fc.args or {}).items()},
                    default=str,
                )
                _trace("TOOL_CALL", f"{YELLOW}{fc.name}{RESET}({args_str})", YELLOW)

        # ── Tool responses (function results back to LLM) ────────
        func_responses = ev.get_function_responses()
        if func_responses:
            for fr in func_responses:
                resp_str = json.dumps(fr.response, default=str) if fr.response else ""
                _trace(
                    "TOOL_RESULT",
                    f"{GREEN}{fr.name}{RESET} → {_truncate(resp_str, 300)}",
                    GREEN,
                )

        # ── Agent transfer (A2A delegation) ───────────────────────
        if transfer_to:
            _trace(
                "A2A_TRANSFER",
                f"{MAGENTA}{author} → {transfer_to}{RESET}",
                MAGENTA,
            )

        # ── Escalation ────────────────────────────────────────────
        if escalate:
            _trace("ESCALATE", f"{author} escalating to parent", RED)

        # ── Text output (reasoning / final response) ─────────────
        if ev.content and ev.content.parts:
            text_parts = [p.text for p in ev.content.parts if p.text]
            if text_parts:
                combined = " ".join(text_parts)
                if ev.is_final_response():
                    _trace(
                        "FINAL_RESPONSE",
                        f"{BOLD}[{author}]{RESET} {_truncate(combined, 500)}",
                        GREEN,
                    )
                    final_text += combined
                elif not func_calls and not func_responses:
                    # Reasoning / planning text from the LLM
                    _trace(
                        "REASONING",
                        f"[{author}] {_truncate(combined, 300)}",
                        DIM,
                    )

    elapsed = time.time() - t0
    _trace(
        "DONE",
        f"Completed in {elapsed:.1f}s | {step_count} events | session={session.id}",
        BOLD,
    )

    # ── Retrieve sub-agent findings from session state ────────────
    sess = await runner.session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session.id
    )
    findings = {}
    for key in ["logs_findings", "metrics_findings", "deploy_findings"]:
        val = sess.state.get(key)
        if val:
            findings[key] = _truncate(str(val), 500)
            _trace("STATE", f"{key} = {_truncate(str(val), 200)}", DIM)
        else:
            _trace("STATE", f"{key} = (not set)", DIM)

    result = {
        "response": final_text,
        "session_id": session.id,
        "elapsed_seconds": round(elapsed, 1),
        "event_count": step_count,
        "sub_agent_findings": findings,
    }

    # ── Send findings via email ────────────────────────────────────
    try:
        _send_findings_email(result)
    except Exception as mail_err:
        _trace("EMAIL", f"Failed to send email: {mail_err}", RED)

    return result


def _send_findings_email(result: dict):
    """Send incident findings via Gmail SMTP."""
    EMAIL_ADDRESS = "sompalli.narendra98@gmail.com"
    EMAIL_PASSWORD = "lvkg extk jocm cyid"
    TO_ADDRESS = "nsompalle@gmail.com"

    findings = result.get("sub_agent_findings", {})
    response_text = result.get("response", "No response captured.")
    elapsed = result.get("elapsed_seconds", "?")

    # Build HTML body
    findings_html = ""
    for key, val in findings.items():
        label = key.replace("_", " ").title()
        findings_html += f"<h3>{label}</h3><pre style='background:#f4f4f4;padding:10px;border-radius:5px;white-space:pre-wrap;'>{val}</pre>"

    html_body = f"""\
<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto;">
    <h1 style="color: #d32f2f;">Incident Commander - Investigation Report</h1>
    <p><b>Session:</b> {result.get("session_id", "N/A")}</p>
    <p><b>Duration:</b> {elapsed}s | <b>Events processed:</b> {result.get("event_count", "?")}</p>
    <hr>
    <h2>Commander Response</h2>
    <pre style="background:#f4f4f4;padding:10px;border-radius:5px;white-space:pre-wrap;">{response_text}</pre>
    <hr>
    <h2>Sub-Agent Findings</h2>
    {findings_html if findings_html else "<p><i>No findings captured.</i></p>"}
</body>
</html>"""

    msg = EmailMessage()
    msg["Subject"] = f"[AIC] Incident Investigation Report - Session {result.get('session_id', 'N/A')}"
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = TO_ADDRESS
    msg.set_content(f"Incident Report\n\nResponse:\n{response_text}\n\nFindings:\n{json.dumps(findings, indent=2)}")
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        smtp.send_message(msg)
    _trace("EMAIL", "Findings email sent successfully", GREEN)


def lambda_handler(event: Any, context: Any = None) -> Dict[str, Any]:
    """AWS Lambda entry point.

    Handles both EventBridge alarm events and direct test invocations.
    """
    logger.info("Received event: %s", json.dumps(event, default=str)[:500])

    # Normalize: wrap raw alarm detail if needed
    if "detail-type" not in event and "detail" not in event:
        event = {"detail": event, "detail-type": "CloudWatch Alarm State Change"}

    try:
        # Lambda may already have a running event loop — handle both cases
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(asyncio.run, _run_commander(event)).result()
        else:
            result = asyncio.run(_run_commander(event))
        logger.info("Commander completed successfully")
        return {
            "statusCode": 200,
            "body": result,
        }
    except Exception as e:
        logger.exception("Commander failed: %s", e)
        return {
            "statusCode": 500,
            "body": {"error": str(e)},
        }
