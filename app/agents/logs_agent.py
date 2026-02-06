import os
import asyncio
import time
import boto3
from typing import List, Dict, Any
from dotenv import load_dotenv

# ADK Core Imports
from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.adk.models.lite_llm import LiteLlm

# 1. LOAD ENVIRONMENT
load_dotenv()

# --- 🛠️ THE MASTER TOOL ---

def diagnose_service_errors(service: str, lookback_minutes: int = 15) -> str:
    """
    SINGLE TOOL CALL: 
    1. Calculates timestamps.
    2. Queries CloudWatch Logs for ERRORs.
    3. Returns ONLY the last 100 characters of the findings.
    """
    logs_client = boto3.client('logs', region_name=os.getenv('AWS_REGION', 'us-east-1'))
    
    # 1. Internal Time Calculation
    end_time = int(time.time())
    start_time = end_time - (lookback_minutes * 60)
    
    log_group = f"/bayer/{service}" 
    query = "fields @timestamp, @message | filter @message like /ERROR/ | sort @timestamp desc | limit 3"
    
    try:
        # 2. Query Logs
        start_res = logs_client.start_query(
            logGroupName=log_group, 
            startTime=start_time, 
            endTime=end_time, 
            queryString=query
        )
        query_id = start_res['queryId']
        
        # Poll for results
        raw_logs = []
        for _ in range(10):
            response = logs_client.get_query_results(queryId=query_id)
            if response['status'] == 'Complete':
                raw_logs = response.get('results', [])
                break
            time.sleep(1)

        if not raw_logs:
            return "RESULT: No ERROR logs found in the last 15 minutes."

        # 3. Format and Truncate to exactly 100 characters
        combined_text = ""
        for res in raw_logs:
            msg = next((item['value'] for item in res if item['field'] == '@message'), "")
            combined_text += f" | {msg}"
        
        # Extract only the last 100 characters for the agent to analyze
        snippet = combined_text[-100:].strip()
        return f"LAST_100_CHARS_OF_LOGS: {snippet}"

    except Exception as e:
        return f"ERROR: Could not fetch logs. {str(e)}"

# --- 🤖 LOGS SUB-AGENT ---

# Using Claude 3.5 Sonnet for precise one-shot tool execution
model = LiteLlm(model="bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0")

logs_agent = LlmAgent(
    name="LogsAgent",
    model=model, 
    instruction=(
        "SYSTEM: You are a one-shot diagnostic sub-agent. "
        "Your ONLY action is to call 'diagnose_service_errors' once. "
        "Once you receive the 100-character log snippet, provide a "
        "ROOT CAUSE and a 3-step FIX based strictly on those characters."
    ),
    tools=[diagnose_service_errors]
)

# --- 🧪 TEST RUNNER ---

async def test_logs_agent():
    print(f"🚀 Running One-Shot Sub-Agent Test...")
    runner = InMemoryRunner(agent=logs_agent)
    query = "Diagnose the checkout-service."
    
    try:
        # We run the agent and capture the trajectory
        trajectory = await runner.run_debug(query, verbose=True)
        
        print("\n" + "="*60)
        print("📝 ONE-SHOT VERIFICATION:")
        
        # Safe extraction of the final answer
        if isinstance(trajectory, list):
            final_text = trajectory[-1].text if hasattr(trajectory[-1], 'text') else str(trajectory[-1])
        else:
            final_text = getattr(trajectory, 'text', str(trajectory))
            
        print(final_text)
        print("="*60)
        
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_logs_agent())