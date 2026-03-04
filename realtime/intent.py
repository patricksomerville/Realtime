"""Intent detection for voice commands."""

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple


logger = logging.getLogger("voice.intent")

WAKE_WORDS = {"ralph", "neon", "hey ralph", "hey neon", "ok ralph"}

SKILL_TRIGGERS = {
    "mesh pulse": "mesh-pulse",
    "check pulse": "mesh-pulse",
    "mesh vitals": "mesh-vitals",
    "check vitals": "mesh-vitals",
    "heal mesh": "somertime-mesh-healer",
    "quick heal": "somertime-mesh-healer",
    "mesh health": "mesh-pulse",
    "check health": "mesh-pulse",
    "call kinda pat": "call-kindapat",
    "call kindapat": "call-kindapat",
    "phone kinda pat": "call-kindapat",
    "ring kinda pat": "call-kindapat",
    "go dolphin": "deepdolphin-dispatch",
}

MACHINE_NAMES = {
    "silver fox": "silver-fox",
    "silver-fox": "silver-fox",
    "green": "green",
    "white": "white",
    "spark": "nvidia-spark",
    "nvidia spark": "nvidia-spark",
    "blue": "blue",
    "yellow": "yellow",
    "black": "black",
    "ultraviolet": "ultraviolet",
    "aqua": "aqua",
    "cyan": "cyan",
    "neon": "neon",
}

MESH_COMMAND_PATTERNS = [
    re.compile(r"(?:on|at)\s+(\w[\w\s-]*?)\s*[,:]?\s*(?:run|execute|do)\s+(.+)", re.I),
    re.compile(r"(?:run|execute|do)\s+(.+?)\s+on\s+(\w[\w\s-]*)", re.I),
    re.compile(r"(?:restart|stop|start)\s+(?:the\s+)?(\w+)\s+on\s+(\w[\w\s-]*)", re.I),
]

LOCAL_LLM_ENABLED = os.environ.get("VOICE_USE_LOCAL_LLM_COMMANDS", "").strip().lower() in {
    "1", "true", "yes", "on"
}
LOCAL_LLM_URL = os.environ.get("VOICE_LOCAL_LLM_URL", "http://localhost:11434/v1/chat/completions")
LOCAL_LLM_MODEL = os.environ.get("VOICE_LOCAL_LLM_MODEL", "qwen2.5-coder:3b")
LOCAL_LLM_TIMEOUT = float(os.environ.get("VOICE_LOCAL_LLM_TIMEOUT", "8") or 8)
LOCAL_LLM_MIN_WORDS = int(os.environ.get("VOICE_LOCAL_LLM_MIN_WORDS", "4") or 4)


def _coerce_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on", "y", "t"}:
        return True
    if value in {"0", "false", "no", "off", "f", "n"}:
        return False
    return default


def _extract_choice_text(payload: Dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            msg = first.get("message")
            if isinstance(msg, dict):
                msg_content = msg.get("content")
                if isinstance(msg_content, str):
                    return msg_content
    return ""


def _clean_json_blob(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_local_payload(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    cleaned = _clean_json_blob(raw)
    try:
        payload = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _classify_with_local_llm(transcript: str) -> Optional[Dict[str, Any]]:
    if not LOCAL_LLM_ENABLED or not transcript:
        return None
    if len(transcript.split()) < LOCAL_LLM_MIN_WORDS:
        return None

    system = (
        "You are a strict command parser for a local voice command system. "
        "Return only JSON, no extra words. "
        'If the input is a command, set "is_command": true and fill out "command_type" and "command_text". '
        'If not a command, set "is_command": false.\n'
        '{"is_command": false, "command_type": "none", "command_text": "", "machine": "", "raw": ""}'
    )
    request_body = {
        "model": LOCAL_LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": transcript},
        ],
        "temperature": 0,
        "stream": False,
    }

    data = json.dumps(request_body).encode("utf-8")
    req = urllib.request.Request(
        LOCAL_LLM_URL,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=LOCAL_LLM_TIMEOUT) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        logger.debug("Local LLM unavailable: %s", exc)
        return None
    except Exception as exc:  # pragma: no cover
        logger.debug("Local LLM request failed: %s", exc)
        return None

    try:
        envelope = json.loads(raw)
    except Exception:
        envelope = {}
    if not isinstance(envelope, dict):
        return None

    content = _extract_choice_text(envelope)
    if not content:
        content = envelope.get("result")
    parsed = _parse_local_payload(content if isinstance(content, str) else "")
    if parsed and isinstance(parsed.get("is_command"), (bool, str)):
        parsed["raw"] = transcript
        return parsed
    if isinstance(envelope.get("is_command"), (bool, str)) and (
        isinstance(envelope.get("command_text"), str) or isinstance(envelope.get("command"), str)
    ):
        envelope_payload = dict(envelope)
        envelope_payload.setdefault("command_text", envelope_payload.get("command", ""))
        envelope_payload["raw"] = transcript
        return envelope_payload
    return None


def classify(transcript: str) -> Tuple[str, Optional[dict]]:
    """
    Classify a transcript.

    Returns:
        (classification, details)
        classification: "command", "ambient", or "conversation"
        details: dict with parsed command info, or None for ambient
    """
    if not transcript or not transcript.strip():
        return "ambient", None

    text = transcript.strip().lower()

    has_wake = any(text.startswith(w) or f" {w}" in text[:30] for w in WAKE_WORDS)

    for trigger, skill in SKILL_TRIGGERS.items():
        if trigger in text:
            return "command", {
                "type": "skill",
                "skill": skill,
                "raw": transcript,
                "wake_word_used": has_wake,
            }

    for pattern in MESH_COMMAND_PATTERNS:
        match = pattern.search(text)
        if match:
            groups = match.groups()
            return "command", {
                "type": "mesh_exec",
                "groups": groups,
                "raw": transcript,
                "wake_word_used": False,
            }

    for name, machine in MACHINE_NAMES.items():
        if name in text and any(verb in text for verb in
                                ["check", "restart", "ping", "status", "run", "deploy"]):
            return "command", {
                "type": "machine_mention",
                "machine": machine,
                "raw": transcript,
                "wake_word_used": has_wake,
            }

    if has_wake:
        return "command", {
            "type": "general",
            "raw": transcript,
            "wake_word_used": True,
        }

    if LOCAL_LLM_ENABLED:
        start = time.time()
        payload = _classify_with_local_llm(transcript)
        if payload:
            is_command = _coerce_bool(payload.get("is_command"), False)
            command_text = str(payload.get("command_text", "")).strip()
            logger.debug("Local LLM intent parse in %.2fs", time.time() - start)
            if is_command and command_text:
                return "command", {
                    "type": "local_command",
                    "command": command_text,
                    "command_type": str(payload.get("command_type", "general")).strip() or "general",
                    "machine": str(payload.get("machine", "")).strip(),
                    "raw": transcript,
                    "wake_word_used": False,
                    "source": "local_llm",
                }

    word_count = len(text.split())
    if word_count < 3:
        return "ambient", None

    return "ambient", None
