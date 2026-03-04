"""
Streaming transcription daemon using OpenAI Realtime API.

Captures mic continuously, streams PCM audio over WebSocket to OpenAI,
receives incremental transcription deltas in real-time. Archives completed
turns to disk + Milvus. Writes live stream to JSONL for the viewer.

Usage:
    python voice/realtime.py              # run in foreground
    python voice/realtime.py --passive    # archive only, no command routing
"""

import sys
import io
import os
import json
import base64
import signal
import asyncio
import logging
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

# Ensure parent dir is on path for realtime.* imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import sounddevice as sd

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logger = logging.getLogger("voice.realtime")

REALTIME_SAMPLE_RATE = 24000
CHANNELS = 1
FRAME_MS = 100
FRAME_SIZE = int(REALTIME_SAMPLE_RATE * FRAME_MS / 1000)

_BASE_DIR = Path(__file__).resolve().parent.parent
STREAMFILE = _BASE_DIR / "transcript.jsonl"
PIDFILE = _BASE_DIR / "daemon.pid"
STATUSFILE = _BASE_DIR / "daemon.status"
ARCHIVE_DIR = _BASE_DIR / "archive"
ENV_FILE = _BASE_DIR / ".env"
_HOME_ENV = Path.home() / ".env"

_stats = {
    "started_at": None,
    "mode": "realtime",
    "turns_completed": 0,
    "total_words": 0,
    "total_audio_sec": 0.0,
    "commands_detected": 0,
    "errors": 0,
    "connected": False,
    "mic_device": "",
    "mic_device_index": None,
    "mic_gain": 1.0,
    "mic_active": False,
    "mic_rms": 0.0,
    "mic_peak": 0.0,
    "last_audio_at": None,
    "last_transcript_at": None,
    "vad_threshold": 0.4,
    "mic_error": "",
}

_seen_event_types = set()


def load_env():
    for _env_path in (ENV_FILE, _HOME_ENV):
        if not _env_path.exists():
            continue
        for line in _env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if key and not os.environ.get(key):
                    os.environ[key] = val


def write_stream(event_type: str, text: str = "", item_id: str = "",
                 duration: float = 0, speaking: bool = False):
    """Write an event to the JSONL stream for the viewer."""
    try:
        line = json.dumps({
            "type": event_type,
            "text": text,
            "item": item_id,
            "t": datetime.now().isoformat(),
            "dur": round(duration, 1) if duration else 0,
            "speaking": speaking,
            "words": len(text.split()) if text else 0,
        }, ensure_ascii=False)
        with open(STREAMFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def write_status():
    try:
        STATUSFILE.write_text(json.dumps(_stats, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _device_name_score(name: str) -> int:
    low = (name or "").lower()
    score = 0
    if "microphone" in low:
        score += 6
    if "headset" in low:
        score += 4
    if "input" in low:
        score += 2
    if "headphone" in low:
        score -= 10
    if "speaker" in low:
        score -= 12
    if "output" in low:
        score -= 12
    if "hands-free" in low:
        score -= 20
    if "mapper" in low:
        score -= 8
    if "primary sound capture driver" in low:
        score -= 7
    if "virtual" in low:
        score -= 2
    return score


def _looks_output_like(name: str) -> bool:
    low = (name or "").lower()
    if "microphone" in low:
        return False
    return any(token in low for token in ("headphone", "speaker", "output"))


def _probe_device_level(index: int, sample_rate: int) -> Tuple[bool, float, float, int]:
    preferred = int(max(8000, min(float(sample_rate or REALTIME_SAMPLE_RATE), 96000)))
    trial_rates: List[int] = [preferred, 24000, 22050, 16000, 12000, 11025, 8000, 32000, 44100, 48000]
    seen = set()
    rates: List[int] = []
    for rate in trial_rates:
        if rate > 0 and rate not in seen:
            seen.add(rate)
            rates.append(rate)
    rates.append(REALTIME_SAMPLE_RATE)
    seen.add(REALTIME_SAMPLE_RATE)

    for rate in rates:
        try:
            frames = int(rate * 0.12)
            rms = 0.0
            peak = 0.0
            got_audio = False

            def _cb(indata, frame_count, time_info, status):
                nonlocal rms, peak, got_audio
                if status:
                    logger.debug("Probe status (idx=%s, rate=%s): %s", index, rate, status)
                arr = np.asarray(indata[:, 0], dtype=np.float32)
                if arr.size == 0:
                    return
                got_audio = True
                rms = float(np.sqrt(np.mean(arr * arr)))
                peak = float(np.max(np.abs(arr)))

            with sd.InputStream(
                samplerate=rate,
                channels=1,
                dtype="float32",
                blocksize=max(64, min(8192, frames)),
                device=index,
                callback=_cb,
            ):
                sd.sleep(160)

            if got_audio:
                return True, rms, peak, rate
            return True, 0.0, 0.0, rate
        except Exception as exc:
            logger.debug("Probe failed (idx=%s, rate=%s): %s", index, rate, exc)
            continue
    return False, 0.0, 0.0, int(preferred)


def _resample_float32(mono: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or mono.size < 2:
        return mono
    src_n = mono.size
    dst_n = max(1, int(round(src_n * (float(dst_rate) / float(src_rate)))))
    x_old = np.linspace(0.0, 1.0, src_n, endpoint=False, dtype=np.float32)
    x_new = np.linspace(0.0, 1.0, dst_n, endpoint=False, dtype=np.float32)
    return np.interp(x_new, x_old, mono).astype(np.float32)


def _resolve_input_device() -> Tuple[Optional[int], str, int]:
    pref = (os.environ.get("VOICE_INPUT_DEVICE") or "").strip()
    allow_output_like = (os.environ.get("VOICE_ALLOW_OUTPUT_INPUT") or "").strip().lower() in ("1", "true", "yes", "on")
    try:
        devices = sd.query_devices()
    except Exception as e:
        logger.warning("Unable to query audio devices: %s", e)
        return None, "default", REALTIME_SAMPLE_RATE

    def _dev_tuple(idx: int) -> Tuple[int, str, int]:
        dev = devices[idx]
        name = str(dev.get("name", f"device-{idx}"))
        sr = int(max(8000, min(float(dev.get("default_samplerate", REALTIME_SAMPLE_RATE)), 48000)))
        return idx, name, sr

    def _usable_input(idx: int) -> bool:
        return 0 <= idx < len(devices) and devices[idx].get("max_input_channels", 0) > 0

    # Explicit device override (index or substring).
    if pref:
        resolved = None
        if pref.isdigit():
            idx = int(pref)
            if _usable_input(idx):
                resolved = _dev_tuple(idx)
        else:
            for i, dev in enumerate(devices):
                if dev.get("max_input_channels", 0) <= 0:
                    continue
                if pref.lower() in str(dev.get("name", "")).lower():
                    resolved = _dev_tuple(i)
                    break
        if resolved:
            idx, name, sr = resolved
            if not allow_output_like and _looks_output_like(name):
                logger.warning(
                    "Ignoring VOICE_INPUT_DEVICE=%s -> %s (%s). Set VOICE_ALLOW_OUTPUT_INPUT=1 to force.",
                    pref,
                    idx,
                    name,
                )
            else:
                ok, _, _, probe_sr = _probe_device_level(idx, sr)
                if ok:
                    logger.info("Using explicit input device %s (%s) sr=%s", idx, name, probe_sr)
                    return idx, name, probe_sr
                logger.warning("Explicit input device %s (%s) probe failed across fallback rates", idx, name)
        else:
            logger.warning("VOICE_INPUT_DEVICE=%s did not resolve to a valid input device; falling back", pref)

    default_idx = None
    try:
        default_cfg = sd.default.device
        if isinstance(default_cfg, (list, tuple)) and default_cfg:
            default_idx = int(default_cfg[0])
    except Exception:
        default_idx = None

    # Prefer OS default input, unless it looks like an output endpoint and not explicitly allowed.
    if default_idx is not None and _usable_input(default_idx):
        d_idx, d_name, d_sr = _dev_tuple(default_idx)
        if allow_output_like or not _looks_output_like(d_name):
            try:
                ok, _, _, probe_sr = _probe_device_level(d_idx, d_sr)
                if ok:
                    logger.info("Using system default input device %s (%s) sr=%s", d_idx, d_name, probe_sr)
                    return d_idx, d_name, probe_sr
                logger.warning("System default input device probe failed (%s)", d_name)
            except Exception as e:
                logger.warning("System default input device probe failed (%s): %s", d_name, e)
        logger.warning("System default input looks output-like (%s); selecting best microphone-style input", d_name)

    # Deterministic fallback: pick the best microphone-style input, avoid output-like names.
    candidates = []
    for i, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        idx, name, sr = _dev_tuple(i)
        score = _device_name_score(name)
        if _looks_output_like(name) and not allow_output_like:
            score -= 24
        if default_idx is not None and i == default_idx:
            score += 4
        candidates.append((score, idx, name, sr))

    if not candidates:
        fallback_name = "default"
        fallback_sr = REALTIME_SAMPLE_RATE
        if default_idx is not None and _usable_input(default_idx):
            _, fallback_name, fallback_sr = _dev_tuple(default_idx)
            ok, _, _, probe_sr = _probe_device_level(default_idx, fallback_sr)
            if ok:
                logger.info("Using fallback default device %s sr=%s", fallback_name, probe_sr)
                return default_idx, fallback_name, probe_sr
        return default_idx, fallback_name, fallback_sr

    candidates.sort(key=lambda row: (row[0], -row[1]), reverse=True)

    # Pick the first candidate that can actually be opened by PortAudio.
    for _, idx, name, sr in candidates:
        try:
            ok, _, _, probe_sr = _probe_device_level(idx, sr)
            if ok:
                logger.info("Selected fallback input device %s (%s) sr=%s", idx, name, probe_sr)
                return int(idx), str(name), int(probe_sr)
            logger.debug("Skipping fallback input device %s (%s) due probe failure", idx, name)
        except Exception as e:
            logger.warning("Skipping input device %s (%s): probe failed: %s", idx, name, e)

    logger.error("No openable input devices available")
    return None, "no-openable-input-device", REALTIME_SAMPLE_RATE


def archive_turn(transcript: str, timestamp: datetime, duration: float):
    """Save a completed turn to disk as JSON, then route commands to action handlers."""
    if not transcript or not transcript.strip():
        return

    from realtime.intent import classify
    classification, details = classify(transcript)

    date_dir = ARCHIVE_DIR / timestamp.strftime("%Y") / timestamp.strftime("%m") / timestamp.strftime("%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    stem = timestamp.strftime("%Y-%m-%d_%H-%M-%S")
    meta_path = date_dir / f"{stem}_rt.json"

    meta = {
        "timestamp": timestamp.isoformat(),
        "duration_sec": round(duration, 2),
        "transcript": transcript,
        "classification": classification,
        "source": "realtime-api",
        "machine": "neon",
        "word_count": len(transcript.split()),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    _stats["turns_completed"] += 1
    _stats["total_words"] += len(transcript.split())
    _stats["total_audio_sec"] += duration
    if classification == "command":
        _stats["commands_detected"] += 1

    logger.info("[%s] %s", classification.upper(), transcript[:120])

    if classification == "command":
        try:
            from realtime.action_router import route
            route(classification, details, transcript)
        except Exception as e:
            logger.error("Action routing failed: %s", e)


async def run_realtime(passive: bool = False):
    """Main async loop: mic capture + WebSocket to OpenAI Realtime API."""
    import websockets

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.error("OPENAI_API_KEY not set")
        return

    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-mini-realtime-preview"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "OpenAI-Beta": "realtime=v1",
    }
    input_gain = max(1.0, _env_float("VOICE_INPUT_GAIN", 2.0))
    vad_threshold = max(0.05, min(0.9, _env_float("VOICE_VAD_THRESHOLD", 0.18)))

    _stats["started_at"] = datetime.now().isoformat()
    _stats["mic_gain"] = input_gain
    _stats["vad_threshold"] = vad_threshold
    _stats["connected"] = False
    write_status()

    PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    PIDFILE.write_text(str(os.getpid()), encoding="utf-8")

    logger.info("Connecting to OpenAI Realtime API...")

    turn_starts = {}

    while True:
        device_idx, device_name, input_sample_rate = _resolve_input_device()
        input_frame_size = max(240, int(input_sample_rate * FRAME_MS / 1000))
        _stats["mic_device"] = device_name
        _stats["mic_device_index"] = device_idx
        _stats["mic_error"] = ""
        write_status()
        if device_idx is None:
            _stats["connected"] = False
            _stats["mic_active"] = False
            _stats["mic_rms"] = 0.0
            _stats["mic_peak"] = 0.0
            _stats["mic_error"] = "No openable microphone input device"
            write_status()
            write_stream("status", text="mic unavailable")
            logger.error("No openable microphone input device; retrying in 8s")
            await asyncio.sleep(8)
            continue
        try:
            async with websockets.connect(
                url,
                extra_headers=headers,
                max_size=2**24,
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                logger.info("WebSocket connected")
                _stats["connected"] = True
                write_status()

                session_config = {
                    "type": "session.update",
                    "session": {
                        "modalities": ["text"],
                        "input_audio_format": "pcm16",
                        "input_audio_transcription": {
                            "model": "gpt-4o-mini-transcribe",
                            "language": "en",
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": vad_threshold,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 600,
                        },
                    },
                }
                await ws.send(json.dumps(session_config))
                logger.info("Session configured: VAD + transcription enabled")

                audio_queue = asyncio.Queue(maxsize=200)
                stop_event = asyncio.Event()
                audio_health = {"last_status_write": 0.0}

                def audio_callback(indata, frames, time_info, status):
                    if status:
                        logger.debug("Audio: %s", status)
                    try:
                        raw = indata[:, 0].astype(np.float32)
                        boosted = np.clip(raw * input_gain, -1.0, 1.0)
                        if input_sample_rate != REALTIME_SAMPLE_RATE:
                            boosted = _resample_float32(boosted, input_sample_rate, REALTIME_SAMPLE_RATE)
                        rms = float(np.sqrt(np.mean(boosted * boosted)))
                        peak = float(np.max(np.abs(boosted)))
                        _stats["mic_active"] = True
                        _stats["mic_rms"] = round(rms, 6)
                        _stats["mic_peak"] = round(peak, 6)
                        _stats["last_audio_at"] = datetime.now().isoformat()
                        now_ts = datetime.now().timestamp()
                        if now_ts - audio_health["last_status_write"] > 1.0:
                            write_status()
                            audio_health["last_status_write"] = now_ts
                        pcm_int16 = (boosted * 32767).astype(np.int16)
                        audio_queue.put_nowait(pcm_int16.tobytes())
                    except asyncio.QueueFull:
                        pass

                async def send_audio():
                    with sd.InputStream(
                        samplerate=input_sample_rate,
                        channels=CHANNELS,
                        dtype="float32",
                        blocksize=input_frame_size,
                        device=device_idx,
                        callback=audio_callback,
                    ):
                        logger.info(
                            "Mic stream open at %d Hz -> %d Hz device=%s gain=%.2f",
                            input_sample_rate,
                            REALTIME_SAMPLE_RATE,
                            f"{device_idx}:{device_name}",
                            input_gain,
                        )
                        write_stream("status", text="listening")

                        while not stop_event.is_set():
                            try:
                                pcm_bytes = await asyncio.wait_for(
                                    audio_queue.get(), timeout=0.5
                                )
                                b64 = base64.b64encode(pcm_bytes).decode("ascii")
                                await ws.send(json.dumps({
                                    "type": "input_audio_buffer.append",
                                    "audio": b64,
                                }))
                            except asyncio.TimeoutError:
                                continue
                            except Exception as e:
                                logger.debug("Send error: %s", e)
                                break

                async def receive_events():
                    try:
                        async for raw in ws:
                            event = json.loads(raw)
                            etype = event.get("type", "")
                            if etype and etype not in _seen_event_types:
                                _seen_event_types.add(etype)
                                logger.info("Realtime event: %s", etype)

                            if etype == "conversation.item.input_audio_transcription.delta":
                                delta_text = event.get("delta", "")
                                item_id = event.get("item_id", "")
                                if delta_text:
                                    write_stream("delta", text=delta_text, item_id=item_id)
                                    _stats["last_transcript_at"] = datetime.now().isoformat()

                            elif etype == "conversation.item.input_audio_transcription.completed":
                                transcript = event.get("transcript", "")
                                item_id = event.get("item_id", "")
                                if transcript:
                                    start_time = turn_starts.pop(item_id, datetime.now())
                                    duration = (datetime.now() - start_time).total_seconds()
                                    write_stream("completed", text=transcript,
                                                 item_id=item_id, duration=duration)
                                    archive_turn(transcript, start_time, duration)
                                    _stats["last_transcript_at"] = datetime.now().isoformat()
                                    write_status()

                            elif etype == "input_audio_buffer.speech_started":
                                item_id = event.get("item_id", "")
                                turn_starts[item_id] = datetime.now()
                                write_stream("vad", speaking=True, item_id=item_id)
                                logger.debug("Speech started")

                            elif etype == "input_audio_buffer.speech_stopped":
                                item_id = event.get("item_id", "")
                                write_stream("vad", speaking=False, item_id=item_id)
                                logger.debug("Speech stopped")

                            elif etype == "input_audio_buffer.committed":
                                pass

                            elif etype in ("session.created", "session.updated",
                                           "transcription_session.created",
                                           "transcription_session.updated"):
                                logger.info("Session event: %s", etype)

                            elif etype == "error":
                                err = event.get("error", {})
                                logger.error("API error: %s", err.get("message", err))
                                _stats["errors"] += 1

                            else:
                                logger.debug("Event: %s", etype)

                    except websockets.ConnectionClosed as e:
                        logger.warning("WebSocket closed: %s", e)
                        stop_event.set()

                send_task = asyncio.create_task(send_audio())
                recv_task = asyncio.create_task(receive_events())
                await asyncio.gather(send_task, recv_task)

        except Exception as e:
            logger.error("Connection error: %s -- reconnecting in 5s", e)
            _stats["connected"] = False
            _stats["errors"] += 1
            if "Invalid device" in str(e):
                _stats["mic_error"] = "Input device failed to open"
            write_status()
            await asyncio.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="Realtime Voice Daemon")
    parser.add_argument("--passive", action="store_true")
    args = parser.parse_args()

    load_env()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    mode = "PASSIVE" if args.passive else "ACTIVE"
    logger.info("Realtime voice daemon starting [%s]", mode)

    def handle_signal(sig, frame):
        logger.info("Shutdown signal received")
        if PIDFILE.exists():
            PIDFILE.unlink()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    asyncio.run(run_realtime(passive=args.passive))


if __name__ == "__main__":
    main()
