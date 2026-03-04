# Realtime

Real-time voice transcription using OpenAI's Realtime API with a live web viewer featuring waveform visualization, pitch tracking, and voice command routing.

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env   # add your OPENAI_API_KEY
python -m realtime      # start the daemon
```

Then open [http://localhost:3088](http://localhost:3088) in your browser.

Or use the launcher script (starts daemon + viewer together):

```bash
scripts/run.bat         # Windows
```

## Architecture

```
Microphone
    |
    v
[realtime.py]  -- streams PCM audio via WebSocket -->  OpenAI Realtime API
    |                                                       |
    |  <-- transcription deltas + completed turns ----------+
    |
    v
transcript.jsonl  <--  [viewer.py]  serves SSE stream --> Browser UI
    |                      |
    v                      v
[archive/]           http://localhost:3088
dated JSON             waveform + pitch EEG
per-turn metadata      live text + command panel
```

### Components

| File | Role |
|------|------|
| `realtime/realtime.py` | Core daemon: mic capture, WebSocket to OpenAI, transcription, archival |
| `realtime/viewer.py` | Web UI server: SSE streaming, waveform, pitch tracking, task/steer routing |
| `realtime/config.py` | Shared configuration and paths |
| `realtime/intent.py` | Voice command classification (wake words, skill triggers, mesh commands) |
| `realtime/action_router.py` | Executes classified voice commands |
| `realtime/archive.py` | Saves audio + transcripts to disk and optionally Milvus |
| `realtime/speak.py` | ElevenLabs TTS output |
| `realtime/listen.py` | Single-shot record-and-transcribe via Whisper |
| `realtime/hotmic.py` | Continuous rolling transcription mode |

## Configuration

All configuration is via environment variables. See `.env.example` for the full list.

**Required:** `OPENAI_API_KEY`

**Optional tuning:**
- `VOICE_INPUT_DEVICE` -- device index or name substring
- `VOICE_INPUT_GAIN` -- mic gain multiplier (default: 2.0)
- `VOICE_VAD_THRESHOLD` -- voice activity detection sensitivity (default: 0.18)

## Desktop Shortcut (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\create-shortcut.ps1
```

## License

MIT
