"""
Voice action router -- takes classified commands from the realtime daemon
and actually executes them.

Called by archive_turn() after intent.classify() returns a command.
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("voice.action_router")

REALTIME_HOME = Path(__file__).resolve().parent.parent
SIGNAL_DIR = Path.home() / ".deepdolphin"
SIGNAL_FILE = SIGNAL_DIR / "signal.json"
MEMORY_FILE = RALPH_HOME / "memory" / "deepdolphin_missions.json"
LLM_COMMAND_FILE = REALTIME_HOME / "llm_commands.jsonl"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "")
    if not value:
        return default
    value = value.strip().lower()
    return value in {"1", "true", "yes", "on", "y", "t"}


def route(classification: str, details: dict, transcript: str):
    """Route a classified voice command to the right handler."""
    if classification != "command" or not details:
        return

    cmd_type = details.get("type")
    skill = details.get("skill")

    if skill == "deepdolphin-dispatch":
        _handle_go_dolphin(transcript)
    elif skill == "call-kindapat":
        _handle_call_kindapat(transcript)
    elif cmd_type == "general":
        logger.info("General voice command (no specific handler): %s", transcript[:100])
    elif cmd_type == "local_command":
        _handle_local_command(details, transcript)


def _append_local_command_record(command: str, details: dict, transcript: str, source: str = "local_llm"):
    record = {
        "t": datetime.now().isoformat(),
        "command": command,
        "source": source,
        "transcript": transcript,
        "command_type": str(details.get("command_type", "general")),
        "machine": str(details.get("machine", "")),
        "raw": details.get("raw"),
    }
    LLM_COMMAND_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LLM_COMMAND_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _handle_local_command(details: dict, transcript: str):
    command = str(details.get("command") or "").strip()
    if not command:
        logger.warning("Local command branch received empty command")
        return

    command_type = str(details.get("command_type", "general")).strip().lower()
    _append_local_command_record(command, details, transcript, source="voice-intent")
    logger.info("Local LLM command parsed: type=%s cmd=%s", command_type, command)

    if command_type == "deepdolphin":
        _handle_go_dolphin(command)
        return

    if not _env_bool("VOICE_EXECUTE_LLM_COMMANDS"):
        logger.info("Voice-local command not executed (VOICE_EXECUTE_LLM_COMMANDS is off): %s", command)
        return

    if command_type in {"shell", "general"}:
        try:
            subprocess.Popen(command, shell=True, cwd=str(REALTIME_HOME))
            logger.info("Executed local LLM command: %s", command)
        except Exception as exc:
            logger.error("Failed to execute local LLM command [%s]: %s", command, exc)
            return


def _extract_dolphin_mission(transcript: str) -> str:
    """Pull the mission text out of a 'Go Dolphin' command."""
    lowered = transcript.lower()
    for trigger in ["go dolphin", "dolphin go", "dd go", "dolphin do", "hear me dolphin"]:
        idx = lowered.find(trigger)
        if idx >= 0:
            after = transcript[idx + len(trigger):].strip().lstrip(",").strip()
            if after:
                return after
    return transcript


def _handle_go_dolphin(transcript: str):
    """Dispatch a DeepDolphin mission from realtime."""
    mission = _extract_dolphin_mission(transcript)
    if not mission:
        logger.warning("Go Dolphin triggered but no mission text found")
        return

    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    mission_id = f"dd_voice_{ts}"

    target = "internet"
    lowered = mission.lower()
    mesh_keywords = ["machine", "silver-fox", "spark", "green", "white", "neon",
                     "blue", "yellow", "black", "aqua", "cyan", "ultraviolet",
                     "disk", "container", "restart", "deploy"]
    if any(kw in lowered for kw in mesh_keywords):
        target = "mesh"

    signal = {
        "action": "task",
        "payload": {
            "mission_id": mission_id,
            "mission": mission,
            "target": target,
            "machines": [],
            "timeout_minutes": 5,
            "dispatched_by": "patrick-voice",
            "dispatched_at": datetime.now().isoformat(),
            "notes": f"Voice command: {transcript}",
        },
        "timestamp": int(time.time() * 1000),
    }

    SIGNAL_FILE.write_text(json.dumps(signal, indent=2))
    logger.info("DeepDolphin dispatched via voice: %s [%s]", mission_id, mission[:80])


def _handle_call_kindapat(transcript: str):
    """Launch a KindaPat call from realtime."""
    lowered = transcript.lower()
    for trigger in ["call kinda pat", "call kindapat", "phone kinda pat", "ring kinda pat"]:
        idx = lowered.find(trigger)
        if idx >= 0:
            after = transcript[idx + len(trigger):].strip().lstrip(",").strip()
            break
    else:
        after = ""

    cmd = [sys.executable, str(REALTIME_HOME / "call_kindapat.py")]
    if after:
        cmd.append(after)

    try:
        subprocess.Popen(cmd, cwd=str(REALTIME_HOME))
        logger.info("KindaPat call launched%s", f": {after[:60]}" if after else "")
    except Exception as e:
        logger.error("Failed to launch KindaPat call: %s", e)
