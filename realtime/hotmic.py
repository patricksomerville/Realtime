"""
Hot mic v2 - continuous streaming transcription.
Always recording, transcribes in rolling chunks every few seconds.
No turn-taking, no waiting for silence.
"""

import sys
import io
import os
import time
import json
import threading
import numpy as np
import sounddevice as sd
import soundfile as sf
from openai import OpenAI

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required.")

SAMPLE_RATE = 16000
CHUNK_INTERVAL = 3.0  # Transcribe every 3 seconds
SPEECH_THRESHOLD = 0.02  # Volume threshold to detect speech in a chunk
MIN_SPEECH_RATIO = 0.05  # At least 5% of chunk frames must be speech

VOICE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "inbox", "voice")
os.makedirs(VOICE_DIR, exist_ok=True)

STATE_FILE = os.path.join(VOICE_DIR, "hotmic_state.json")
TRANSCRIPT_FILE = os.path.join(VOICE_DIR, "latest.txt")
STREAM_FILE = os.path.join(VOICE_DIR, "stream.jsonl")  # Rolling stream of all utterances
PID_FILE = os.path.join(VOICE_DIR, "hotmic.pid")

client = OpenAI(api_key=OPENAI_API_KEY)


def write_state(state, **kwargs):
    data = {"state": state, "ts": time.time(), **kwargs}
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass


def has_speech(audio_data):
    """Check if audio chunk contains speech above threshold."""
    frame_volumes = np.abs(audio_data).reshape(-1, 160).mean(axis=1)  # 10ms frames
    speech_frames = (frame_volumes > SPEECH_THRESHOLD).sum()
    return speech_frames / len(frame_volumes) > MIN_SPEECH_RATIO


def transcribe_chunk(audio_data):
    """Transcribe audio chunk via Whisper."""
    buffer = io.BytesIO()
    sf.write(buffer, audio_data, SAMPLE_RATE, format='WAV')
    buffer.seek(0)
    buffer.name = "chunk.wav"

    try:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=buffer,
            response_format="text"
        )
        return response.strip()
    except Exception as e:
        print(f"Transcription error: {e}", file=sys.stderr, flush=True)
        return ""


def main():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    # Clear old state
    with open(TRANSCRIPT_FILE, "w") as f:
        f.write("")
    with open(STREAM_FILE, "w") as f:
        f.write("")

    print("Hot mic v2 active. Streaming transcription.", file=sys.stderr, flush=True)
    write_state("listening")

    # Shared audio buffer
    audio_buffer = []
    buffer_lock = threading.Lock()
    seq = [0]

    def audio_callback(indata, frames, time_info, status):
        with buffer_lock:
            audio_buffer.append(indata.copy())

    # Start persistent audio stream
    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype='float32',
        callback=audio_callback,
        blocksize=1600  # 100ms blocks
    )
    stream.start()

    try:
        while True:
            # Wait for chunk interval
            time.sleep(CHUNK_INTERVAL)

            # Grab current buffer
            with buffer_lock:
                if not audio_buffer:
                    continue
                chunks = audio_buffer.copy()
                audio_buffer.clear()

            audio = np.concatenate(chunks, axis=0)

            # Skip if no speech detected
            if not has_speech(audio):
                continue

            # Transcribe in background to not block recording
            write_state("transcribing")
            text = transcribe_chunk(audio)

            if text and len(text) > 1:
                seq[0] += 1
                ts = time.time()

                # Write latest
                with open(TRANSCRIPT_FILE, "w") as f:
                    f.write(text)

                # Append to stream
                entry = {"seq": seq[0], "ts": ts, "text": text}
                with open(STREAM_FILE, "a") as f:
                    f.write(json.dumps(entry) + "\n")

                write_state("heard", text=text, seq=seq[0])
                print(f"[{seq[0]}] {text}", file=sys.stderr, flush=True)
            else:
                write_state("listening")

    except KeyboardInterrupt:
        print("\nHot mic stopped.", file=sys.stderr)
    finally:
        stream.stop()
        stream.close()
        write_state("stopped")


if __name__ == "__main__":
    main()
