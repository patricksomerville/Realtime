"""
Speak text - called by /conversation command
Takes text as argument or stdin, speaks it via TTS
"""

import sys
import os
import subprocess
import tempfile
import wave
import numpy as np
import requests

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
if not ELEVENLABS_API_KEY:
    raise RuntimeError("ELEVENLABS_API_KEY is required (set it in your environment).")

# ElevenLabs voices:
# Jessica (cgSgspJ2msm6clMCkdW9) - Playful, Bright, Warm
# Sarah (EXAVITQu4vr4xnSDxMaL) - Mature, Reassuring, Confident
# Alice (Xb7hH8MSUJpSbSDYk0k2) - Clear, Engaging Educator
VOICE_ID = "cgSgspJ2msm6clMCkdW9"  # Jessica
SPEED = 1.05  # Slight bump, mostly natural
MODEL = "eleven_turbo_v2_5"  # Fast, high quality
ELEVENLABS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"


PCM_SAMPLE_RATE = 24000

def _synthesize_with_http(text: str) -> bytes:
    """Generate speech bytes directly from ElevenLabs REST API."""
    url = f"{ELEVENLABS_URL}?output_format=pcm_24000"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "accept": "application/octet-stream",
        "content-type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": MODEL,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "speed": SPEED,
        },
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=45)
    if not resp.ok:
        body = resp.text if isinstance(resp.text, str) else ""
        raise RuntimeError(f"ElevenLabs TTS failed ({resp.status_code}): {body[:400]}")
    return resp.content

def speak(text):
    """Convert text to speech and play via ElevenLabs."""
    if not text.strip():
        return

    # Truncate if too long
    text = text[:5000]

    raw_pcm = _synthesize_with_http(text)
    audio_data = np.frombuffer(raw_pcm, dtype=np.int16)

    wav_path = os.path.join(tempfile.gettempdir(), "ralph_speak.wav")
    with wave.open(wav_path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(PCM_SAMPLE_RATE)
        wf.writeframes(audio_data.tobytes())

    subprocess.run(
        ["powershell.exe", "-Command",
         f'(New-Object Media.SoundPlayer "{wav_path}").PlaySync()'],
        timeout=60,
    )

def main():
    # Get text from argument or stdin
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = sys.stdin.read()
    
    speak(text)

if __name__ == "__main__":
    main()
