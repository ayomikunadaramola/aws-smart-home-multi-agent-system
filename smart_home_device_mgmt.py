"""
smart_home_device_mgmt.py - STARTER
======================================
Module 1 Exercise: Build a 3-Agent Smart Home Device Management System

Follow the same multi-agent coordinator pattern from the demo:
  - 3 separate Agent instances (DeviceMonitor, DiagnosticsAgent, CommandAgent)
  - Each agent has its own model, system prompt, and tool
  - A coordinator calls them in sequence, passing outputs forward

You have 6 TODOs to complete:
  TODO 1: Build the Device Monitor agent (model + prompt + tool)
  TODO 2: Build the Diagnostics agent (model + prompt + tool)
  TODO 3: Build the Command agent (model + prompt + tool)
  TODO 4: Coordinator step 1 — call Device Monitor
  TODO 5: Coordinator step 2 — call Diagnostics Agent
  TODO 6: Coordinator step 3 — call Command Agent for each issue

Architecture:
    Sensor Data → DeviceMonitor → DiagnosticsAgent → CommandAgent → Action Report

Tech Stack:
  - Python 3.11+
  - Strands Agents SDK (Agent class, @tool decorator)
  - Amazon Bedrock (Claude 3 Sonnet)
"""

import json
import os
import re
import time
import logging
from datetime import datetime
from dotenv import load_dotenv
from strands import Agent, tool
from strands.models import BedrockModel

load_dotenv()

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

# ─────────────────────────────────────────────────────
# SAMPLE DATA — Pre-written, do not modify.
# ─────────────────────────────────────────────────────
DEVICE_REGISTRY = {
    "DEV-001": {"name": "Living Room Thermostat", "type": "thermostat", "location": "living room"},
    "DEV-002": {"name": "Front Door Smart Lock",  "type": "smart_lock", "location": "front door"},
    "DEV-003": {"name": "Doorbell Camera",         "type": "camera",     "location": "front porch"},
}

SENSOR_READINGS = [
    {
        "device_id": "DEV-001",
        "timestamp": "2026-01-15T14:30:00Z",
        "readings": {"temperature": 92.5, "humidity": 35, "connectivity": 85, "battery": 100},
    },
    {
        "device_id": "DEV-002",
        "timestamp": "2026-01-15T14:30:00Z",
        "readings": {"temperature": 68.0, "humidity": 45, "connectivity": 12, "battery": 72},
    },
    {
        "device_id": "DEV-003",
        "timestamp": "2026-01-15T14:30:00Z",
        "readings": {"temperature": 55.0, "humidity": 60, "connectivity": 90, "battery": 7},
    },
]

DIAGNOSTIC_RULES = {
    "overheating":    {"threshold": 85, "field": "temperature",  "operator": ">"},
    "firmware_issue": {"threshold": 20, "field": "connectivity", "operator": "<"},
    "low_battery":    {"threshold": 10, "field": "battery",      "operator": "<"},
}

CORRECTIVE_ACTIONS = {
    "overheating":    {"action": "restart_device",             "message": "Restarting device to cool down and recalibrate sensors."},
    "firmware_issue": {"action": "push_firmware_update",       "message": "Pushing firmware update v2.1.3 to restore connectivity."},
    "low_battery":    {"action": "send_recharge_notification", "message": "Sending low-battery alert to homeowner's phone."},
}

def clean_response(text: str) -> str:
    """
    Clean agent responses so the coordinator can parse JSON.

    Removes thinking tags and Markdown code fences while
    preserving the JSON object returned by the agent.
    """

    # Convert the response to a string and remove whitespace.
    cleaned = str(text).strip()

    # Remove Claude/Nova thinking tags.
    cleaned = re.sub(
        r"<thinking>.*?</thinking>",
        "",
        cleaned,
        flags=re.DOTALL
    ).strip()

    # Remove a leading Markdown JSON code fence.
    cleaned = re.sub(
        r"^```(?:json)?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE
    )

    # Remove a trailing Markdown code fence.
    cleaned = re.sub(
        r"\s*```\s*$",
        "",
        cleaned
    )

    return cleaned.strip()    


# NOTE: In production, extract shared helpers like run_agent_with_retry() and
# clean_response() to a common utils.py module to avoid code duplication.
def run_agent_with_retry(agent_builder, prompt: str, max_retries: int = 3) -> str:
    """Run an agent with retry logic for transient Bedrock errors.
    Uses exponential backoff (1s, 2s, 4s) to handle throttling."""
    for attempt in range(max_retries):
        try:
            agent = agent_builder()
            result = agent(prompt)
            return clean_response(result)
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"    [Retry {attempt + 1}/{max_retries}] {e.__class__.__name__}, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"    [Failed] {e.__class__.__name__} after {max_retries} attempts")
                raise


# ═══════════════════════════════════════════════════════
#  TOOL IMPLEMENTATIONS — Pre-written, do not modify.
#  You will use these tools inside your agent builders.
# ═══════════════════════════════════════════════════════

def _make_read_sensor_data_tool():
    """Returns the read_sensor_data tool for the Device Monitor agent."""
    @tool
    def read_sensor_data(device_id: str) -> str:
        """Read the latest sensor data for a device.

        Args:
            device_id: The device's unique identifier (e.g., "DEV-001")

        Returns:
            JSON string with device info and current sensor readings
        """
        device_info = DEVICE_REGISTRY.get(device_id)
        if not device_info:
            return json.dumps({"error": f"Device {device_id} not found in registry"})
        reading = None
        for r in SENSOR_READINGS:
            if r["device_id"] == device_id:
                reading = r
                break
        if not reading:
            return json.dumps({"error": f"No sensor data available for {device_id}"})
        return json.dumps({
            "device_id": device_id,
            "device_name": device_info["name"],
            "device_type": device_info["type"],
            "location": device_info["location"],
            "timestamp": reading["timestamp"],
            "readings": reading["readings"],
        }, indent=2)
    return read_sensor_data


def _make_diagnose_issue_tool():
    """Returns the diagnose_issue tool for the Diagnostics agent."""
    @tool
    def diagnose_issue(sensor_data_json: str) -> str:
        """Apply diagnostic rules to sensor readings to identify issues.

        Rules: temperature > 85 = overheating, connectivity < 20 = firmware_issue, battery < 10 = low_battery

        Args:
            sensor_data_json: JSON string from the Device Monitor

        Returns:
            JSON string with device_id, issues found, and status
        """
        data = {}
        readings = {}
        try:
            data = json.loads(sensor_data_json)
            readings = data.get("readings", {})
        except (json.JSONDecodeError, AttributeError, TypeError):
            text_lower = sensor_data_json.lower()
            temp_match = re.search(r'temperature["\s:]*(\d+\.?\d*)', text_lower)
            if temp_match:
                readings["temperature"] = float(temp_match.group(1))
            conn_match = re.search(r'connectivity["\s:]*(\d+\.?\d*)', text_lower)
            if conn_match:
                readings["connectivity"] = float(conn_match.group(1))
            batt_match = re.search(r'battery["\s:]*(\d+\.?\d*)', text_lower)
            if batt_match:
                readings["battery"] = float(batt_match.group(1))
        issues = []
        for issue_name, rule in DIAGNOSTIC_RULES.items():
            value = readings.get(rule["field"], 0)
            if rule["operator"] == ">" and value > rule["threshold"]:
                issues.append({"issue": issue_name, "field": rule["field"], "value": value, "threshold": rule["threshold"]})
            elif rule["operator"] == "<" and value < rule["threshold"]:
                issues.append({"issue": issue_name, "field": rule["field"], "value": value, "threshold": rule["threshold"]})
        return json.dumps({
            "device_id": data.get("device_id", "unknown"),
            "issues_found": len(issues),
            "issues": issues,
            "status": "issues_detected" if issues else "healthy",
        }, indent=2)
    return diagnose_issue


def _make_send_device_command_tool():
    """Returns the send_device_command tool for the Command agent."""
    @tool
    def send_device_command(device_id: str, issue_type: str) -> str:
        """Send a corrective command to a device based on the diagnosed issue.

        Args:
            device_id: The device's unique identifier
            issue_type: The type of issue ("overheating", "firmware_issue", "low_battery")

        Returns:
            JSON string with command confirmation and action taken
        """
        action_info = CORRECTIVE_ACTIONS.get(issue_type)
        if not action_info:
            return json.dumps({"status": "error", "message": f"No action for: {issue_type}"})
        device_info = DEVICE_REGISTRY.get(device_id, {"name": "Unknown"})
        return json.dumps({
            "status": "command_sent",
            "device_id": device_id,
            "device_name": device_info.get("name", "Unknown"),
            "issue": issue_type,
            "action": action_info["action"],
            "message": action_info["message"],
            "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, indent=2)
    return send_device_command


# ═══════════════════════════════════════════════════════
#  TODO 1: BUILD THE DEVICE MONITOR AGENT
#  Follow the 3-step pattern from the demo's build_symptom_analyzer():
#  model → system_prompt → Agent
# ═══════════════════════════════════════════════════════


def build_device_monitor() -> Agent:
    """Build the Device Monitor agent with a read_sensor_data tool."""

    # Step 1: Configure the Amazon Bedrock model.
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=AWS_REGION,
        temperature=0.0,
    )

    # Step 2: Define the agent's role and output requirements.
    system_prompt = """
    You are a Device Monitor Agent in a smart home device
    management system.

    Your responsibility is to retrieve sensor readings for
    the device ID provided by the coordinator.

    Follow these instructions strictly:

    1. Use the read_sensor_data tool to retrieve the latest
       sensor readings for the requested device.

    2. Return the complete JSON object produced by the tool.

    3. Preserve the original device ID, device name, device
       type, location, timestamp, and sensor readings.

    4. Do not modify, estimate, or invent sensor readings.

    5. If the tool reports an error or missing sensor data,
       return the tool's error response as valid JSON.

    6. Your entire response must be a single, valid JSON
       object that can be parsed using json.loads().

    7. Do not include conversational text, explanations,
       Markdown formatting, or code fences.

    8. Do not diagnose device problems or dispatch commands.
       Your only responsibility is retrieving sensor data.
    """

    # Step 3: Create and return the Device Monitor agent.
    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[_make_read_sensor_data_tool()],
    )    


# ═══════════════════════════════════════════════════════
#  TODO 2: BUILD THE DIAGNOSTICS AGENT
#  Follow the 3-step pattern from the demo's build_urgency_classifier():
#  model → system_prompt → Agent
# ═══════════════════════════════════════════════════════

def build_diagnostics_agent() -> Agent:
    """Build the Diagnostics agent with a diagnose_issue tool."""

    # Step 1: Configure the Amazon Bedrock model.
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=AWS_REGION,
        temperature=0.0,
    )

    # Step 2: Define the agent's diagnostic responsibilities.
    system_prompt = """
    You are a Diagnostics Agent in a smart home device
    management system.

    Your responsibility is to analyze sensor data received
    from the Device Monitor and identify device anomalies.

    Follow these instructions strictly:

    1. Receive the sensor data JSON provided by the coordinator.

    2. Use the diagnose_issue tool to analyze the sensor data.

    3. Pass the complete sensor data JSON to the tool without
       changing the original device ID or sensor readings.

    4. Use the diagnostic results returned by the tool.
       Do not invent additional issues or change the results.

    5. Return a single, valid JSON object containing exactly
       these fields:

       {
           "device_id": "device identifier",
           "issues_found": 0,
           "issues": [],
           "status": "healthy"
       }

    6. Preserve the issues_found, issues, and status values
       returned by the diagnose_issue tool.

    7. If no issues are detected, return an empty issues list
       and set the status to "healthy".

    8. If the sensor data is missing, malformed, or contains
       an error, do not assume that the device is healthy.
       Return a valid JSON object with:

       {
           "device_id": "device identifier or unknown",
           "issues_found": 0,
           "issues": [],
           "status": "error"
       }

    9. Return only raw, valid JSON that can be parsed using
       json.loads().

    10. Do not include explanations, introductory phrases,
        Markdown formatting, or code fences.

    11. Do not dispatch corrective commands.
        Your responsibility is limited to diagnosis.
    """

    # Step 3: Create and return the Diagnostics Agent.
    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[_make_diagnose_issue_tool()],
    )    


# ═══════════════════════════════════════════════════════
#  TODO 3: BUILD THE COMMAND AGENT
#  Build the Command Agent following the same pattern as TODOs 1-2.
# ═══════════════════════════════════════════════════════

def build_command_agent() -> Agent:
    """Build the Command agent with a send_device_command tool."""

    # Step 1: Configure the Amazon Bedrock model.
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=AWS_REGION,
        temperature=0.0,
    )

    # Step 2: Define the agent's responsibilities.
    system_prompt = """
    You are a Command Agent in a smart home device
    management system.

    Your responsibility is to dispatch corrective commands
    for diagnosed device issues.

    You receive a device ID and an issue type from the
    coordinator.

    Follow these instructions strictly:

    1. Use the send_device_command tool to dispatch the
       appropriate corrective action.

    2. Pass the exact device ID and issue type provided
       by the coordinator to the tool.

    3. Use only the corrective action returned by the tool.
       Do not invent commands or modify the action.

    4. Return the complete JSON response produced by the
       send_device_command tool.

    5. Preserve the original response fields, including:

       - status
       - device_id
       - device_name
       - issue
       - action
       - message
       - timestamp

    6. If the tool returns an error, return the error
       response as valid JSON.

    7. Your entire response must be a single, valid JSON
       object that can be parsed using json.loads().

    8. Do not include explanations, introductory phrases,
       Markdown formatting, or code fences.

    9. Do not retrieve sensor readings or diagnose issues.
       Your responsibility is limited to dispatching
       corrective commands.
    """

    # Step 3: Create and return the Command Agent.
    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[_make_send_device_command_tool()],
    )    


# ═══════════════════════════════════════════════════════
#  COORDINATOR — Wire the 3 agents together
#  Complete TODOs 4-6 to call each agent in sequence.
# ═══════════════════════════════════════════════════════

def run_device_pipeline(device_id: str) -> dict:
    """Run the 3-agent device management pipeline.

    Flow: DeviceMonitor → DiagnosticsAgent → CommandAgent
    """

    # TODO 4: Call the Device Monitor agent
    #   - Use run_agent_with_retry(build_device_monitor, prompt)
    #   - Parse the JSON response to get sensor data
    #   - Print the device name
    #   Hint: prompt should ask to read sensor data for the device_id
    print("    [1/3] Device Monitor...")
    
    # Prepare the Device Monitor prompt.
    monitor_prompt = (
        f"Retrieve the latest sensor readings for device {device_id}. "
        "Use the read_sensor_data tool and return only the "
        "complete JSON response."
    )

    # Execute the Device Monitor.
    sensor_str = run_agent_with_retry(
        build_device_monitor,
        monitor_prompt
    )

    # Parse and validate the JSON response.
    try:
        sensor_json = json.loads(sensor_str)

        if not isinstance(sensor_json, dict):
            raise ValueError(
                "Device Monitor returned a non-object JSON response."
            )

        if "error" in sensor_json:
            raise ValueError(sensor_json["error"])

        if not isinstance(sensor_json.get("readings"), dict):
            raise ValueError(
                "Device Monitor returned missing or invalid sensor readings."
            )

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        print(f"    [Error] Device Monitor failed: {e}")
        raise

    # Preserve the validated sensor data for the Diagnostics Agent.
    sensor_str = json.dumps(sensor_json)

    # Display the monitored device's name.
    print(
        f"    Device: {sensor_json.get('device_name', 'Unknown')}"
    )


    # TODO 5: Call the Diagnostics Agent
    #   - Use run_agent_with_retry(build_diagnostics_agent, prompt)
    #   - Pass the sensor_str from TODO 4 in the prompt
    #   - Parse the JSON response to get diagnosis
    #   - Extract the list of issues
    print("    [2/3] Diagnostics Agent...")

    # Prepare the Diagnostics Agent prompt.
    diagnostics_prompt = (
        "Analyze the following smart home device sensor data. "
        "Use the diagnose_issue tool to identify any device "
        "anomalies. Return only the complete JSON response.\n\n"
        f"Sensor data:\n{sensor_str}"
    )

    # Execute the Diagnostics Agent.
    diag_str = run_agent_with_retry(
        build_diagnostics_agent,
        diagnostics_prompt
    )

    # Parse and validate the diagnostic response.
    try:
        diag_json = json.loads(diag_str)

        if not isinstance(diag_json, dict):
            raise ValueError(
                "Diagnostics Agent returned a non-object JSON response."
            )

        if diag_json.get("status") == "error":
            raise ValueError(
                "Diagnostics Agent reported a diagnostic error."
            )

        issues = diag_json.get("issues")

        if not isinstance(issues, list):
            raise ValueError(
                "Diagnostics Agent returned an invalid issues list."
            )

        # Validate the structure of every identified issue.
        for issue in issues:
            if not isinstance(issue, dict):
                raise ValueError(
                    "Diagnostics Agent returned a malformed issue."
                )

            if not isinstance(issue.get("issue"), str):
                raise ValueError(
                    "An identified issue has no valid issue type."
                )

        # Confirm that the diagnosis belongs to this device.
        if diag_json.get("device_id") != device_id:
            raise ValueError(
                "Diagnostics Agent returned a mismatched device ID."
            )

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        print(f"    [Error] Diagnostics Agent failed: {e}")
        raise

    # Display the diagnostic results.
    print(f"    Status: {diag_json.get('status', 'unknown')}")
    print(f"    Issues detected: {len(issues)}")

    for issue in issues:
        print(f"    Identified issue: {issue['issue']}")


    # TODO 6: Call the Command Agent for each issue
    #   - Loop through the issues list from TODO 5
    #   - For each issue, call run_agent_with_retry(build_command_agent, prompt)
    #   - Pass device_id and issue_type in the prompt
    #   - If no issues, print "skipped (device healthy)"
    commands = []
    if issues:
        for issue in issues:
            issue_type = issue.get("issue", "unknown")
            print(f"    [3/3] Command Agent ({issue_type})...")
            
            
            # Prepare the Command Agent prompt.
            command_prompt = (
                f"Dispatch the appropriate corrective command "
                f"for device ID {device_id} with issue type "
                f"{issue_type}. "
                "Use the send_device_command tool and return "
                "only the complete JSON response."
            )

            # Execute the Command Agent.
            command_str = run_agent_with_retry(
                build_command_agent,
                command_prompt
            )

            # Parse and validate the command response.
            try:
                command_json = json.loads(command_str)

                if not isinstance(command_json, dict):
                    raise ValueError(
                        "Command Agent returned a non-object JSON response."
                    )

                if command_json.get("status") != "command_sent":
                    raise ValueError(
                        "Command Agent did not confirm command dispatch."
                    )

                if command_json.get("device_id") != device_id:
                    raise ValueError(
                        "Command Agent returned a mismatched device ID."
                    )

                if command_json.get("issue") != issue_type:
                    raise ValueError(
                        "Command Agent returned a mismatched issue type."
                    )

                if not command_json.get("action"):
                    raise ValueError(
                        "Command Agent returned no corrective action."
                    )

            except (json.JSONDecodeError, ValueError, TypeError) as e:
                print(f"    [Error] Command Agent failed: {e}")
                raise

            # Collect the validated command response.
            commands.append(command_json)

            # Display confirmation of the corrective action.
            print(
                f"    Command dispatched: {command_json['action']}"
            )

    else:
        print("    [3/3] Command Agent — skipped (device healthy)")

    return {
        "device_id": device_id,
        "sensor_data": sensor_json,
        "diagnosis": diag_json,
        "commands": commands,
    }


# ═══════════════════════════════════════════════════════
#  MAIN — Pre-written, do not modify.
# ═══════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  Smart Home Device Management — Module 1 Exercise")
    print("  3-Agent Architecture: Monitor → Diagnostics → Command")
    print("=" * 70)

    test_scenarios = [
        {"device_id": "DEV-001", "expected": "overheating",    "desc": "Thermostat at 92.5°F"},
        {"device_id": "DEV-002", "expected": "firmware_issue",  "desc": "Smart lock at 12% connectivity"},
        {"device_id": "DEV-003", "expected": "low_battery",     "desc": "Doorbell camera at 7% battery"},
    ]

    for s in test_scenarios:
        print(f"\n{'─' * 70}")
        print(f"  Scenario: {s['desc']}")
        print(f"  Device: {s['device_id']} | Expected: {s['expected']}")
        print(f"{'─' * 70}")

        result = run_device_pipeline(s["device_id"])

        print(f"\n  Summary:")
        print(f"    Device: {result['sensor_data'].get('device_name', '?')}")
        print(f"    Status: {result['diagnosis'].get('status', '?')}")
        issues = result['diagnosis'].get('issues', [])
        if issues:
            for issue in issues:
                print(f"    Issue: {issue.get('issue', '?')} ({issue.get('field', '?')}={issue.get('value', '?')})")
        for cmd in result['commands']:
            print(f"    Action: {cmd.get('action', '?')} — {cmd.get('message', '')}")


if __name__ == "__main__":
    main()
