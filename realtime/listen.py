"""
Listen and transcribe - called by /conversation command
Records until silence or max duration, transcribes, outputs text
"""

import sys
import io
import os
import numpy as np
import sounddevice as sd
import soundfile as sf
from openai import OpenAI

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required (set it in your environment).")

SAMPLE_RATE = 16000
MAX_DURATION = 30  # seconds
SILENCE_THRESHOLD = 0.015  # Lower = more sensitive to quiet speech
SPEECH_THRESHOLD = 0.05  # Must detect speech above this before silence counting
SILENCE_DURATION = 1.5  # seconds of silence to stop
MIN_RECORD_TIME = 1.5  # minimum seconds before checking silence

def record_until_silence():
    """Record audio until silence detected or max duration."""
    audio_chunks = []
    silence_samples = 0
    silence_samples_threshold = int(SILENCE_DURATION * SAMPLE_RATE)
    min_samples = int(MIN_RECORD_TIME * SAMPLE_RATE)
    max_samples = int(MAX_DURATION * SAMPLE_RATE)
    warmup_samples = int(0.5 * SAMPLE_RATE)  # Skip first 0.5s to avoid mic pop
    total_samples = 0
    speech_detected = False

    def callback(indata, frames, time, status):
        nonlocal silence_samples, total_samples, speech_detected
        total_samples += frames

        # Skip warmup period to avoid mic activation noise
        if total_samples < warmup_samples:
            return

        audio_chunks.append(indata.copy())
        volume = np.abs(indata).mean()

        # Detect if speech has started
        if volume > SPEECH_THRESHOLD:
            speech_detected = True
            silence_samples = 0
        elif speech_detected and volume < SILENCE_THRESHOLD:
            # Only count silence after speech detected
            silence_samples += frames

    print("Listening... (speak now, stops on silence)", file=sys.stderr)

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype='float32', callback=callback):
        while total_samples < max_samples:
            # Don't stop until minimum time AND speech detected AND silence
            if total_samples > min_samples and speech_detected and silence_samples > silence_samples_threshold:
                break
            sd.sleep(100)
    
    if not audio_chunks:
        return None
    
    return np.concatenate(audio_chunks, axis=0)

def transcribe(audio_data):
    """Transcribe audio using Whisper."""
    buffer = io.BytesIO()
    sf.write(buffer, audio_data, SAMPLE_RATE, format='WAV')
    buffer.seek(0)
    buffer.name = "audio.wav"
    
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.audio.transcriptions.create(
        model="whisper-1",
        file=buffer,
        response_format="text"
    )
    return response

def main():
    audio = record_until_silence()
    
    if audio is None or len(audio) < SAMPLE_RATE:  # Less than 1 second
        print("", end="")  # Empty output - too short to be real speech
        return
    
    print("Transcribing...", file=sys.stderr)
    text = transcribe(audio)
    print(text, end="")  # Output transcript to stdout

if __name__ == "__main__":
    main()
