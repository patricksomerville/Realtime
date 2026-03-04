"""
Voice system configuration for the Somertime Network.
All constants and paths in one place.
"""

import os
from pathlib import Path

REALTIME_HOME = Path(os.environ.get("REALTIME_HOME", Path(__file__).resolve().parent.parent))
VOICE_DIR = REALTIME_HOME
ARCHIVE_DIR = REALTIME_HOME / "archive"

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "float32"

# VAD settings
VAD_THRESHOLD = 0.4
SPEECH_PAD_MS = 300
MIN_SPEECH_DURATION_MS = 500
MIN_SILENCE_DURATION_MS = 1200
MAX_SEGMENT_DURATION_SEC = 600

# Whisper
WHISPER_MODEL = "whisper-1"

# Milvus
MILVUS_URI = "http://neon:19530"
VOICE_COLLECTION = "voice_archive"
EMBEDDING_DIM = 768
EMBEDDING_MODEL = "all-mpnet-base-v2"

# Archive retention
ARCHIVE_RETENTION_DAYS = 365

# Env file for API keys
ENV_FILE = Path.home() / ".env"


def load_env():
    """Load .env file into os.environ if keys not already set."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key and not os.environ.get(key):
                os.environ[key] = val
