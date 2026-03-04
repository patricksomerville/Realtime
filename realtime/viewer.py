"""
Live transcript viewer v2 -- real-time streaming with waveform.

Handles the enhanced stream protocol from the realtime daemon:
  delta   - partial words as they arrive
  completed - full turn transcript
  vad     - speech start/stop
  status  - daemon state changes

Auto-requests mic for waveform. Shows flowing text, not just segments.
"""

import json
import os
import re
import time
import argparse
import threading
import webbrowser
import base64
import io
import math
import struct
import wave
from datetime import datetime
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.request import Request, urlopen

_BASE_DIR = Path(__file__).resolve().parent.parent
STREAMFILE = _BASE_DIR / "transcript.jsonl"
STATUSFILE = _BASE_DIR / "daemon.status"
TASKFILE = _BASE_DIR / "tasklist.jsonl"
DISPATCHFILE = _BASE_DIR / "task_dispatch.jsonl"
STEERFILE = _BASE_DIR / "steer_commands.jsonl"
STEER_STATEFILE = _BASE_DIR / "steer_state.json"
DEEPDOLPHIN_SIGNAL_DIR = Path.home() / ".deepdolphin" / "signals"
STEER_COOLDOWN_SEC = int(os.environ.get("VOICE_STEER_COOLDOWN_SEC", "12"))
STEER_WINDOW_SEC = int(os.environ.get("VOICE_STEER_WINDOW_SEC", "300"))
STEER_MAX_PER_WINDOW = int(os.environ.get("VOICE_STEER_MAX_PER_WINDOW", "6"))

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Realtime</title>
<style>
  :root {
    --bg: #06060b;
    --surface: #0c0c14;
    --surface-2: #101022;
    --border: #141428;
    --text: #c8c8d4;
    --text-dim: #3e3e56;
    --accent: #4a9eff;
    --live: #4aff6b;
    --command: #ff6b4a;
    --warn: #ffab4a;
    --wave-stroke: rgba(74, 158, 255, 0.5);
    --wave-fill: rgba(74, 158, 255, 0.08);
  }
  * { margin:0; padding:0; box-sizing:border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'SF Mono','Cascadia Code','JetBrains Mono','Fira Code',monospace;
    font-size: 14px;
    line-height: 1.6;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    width: 100%;
    max-width: 100%;
  }

  header {
    padding: 12px 60px 10px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    background: var(--surface);
    flex-shrink: 0;
  }
  .title { font-size:14px; font-weight:600; letter-spacing:1.5px; color:var(--text-dim); text-transform:uppercase; }
  #topDateTime {
    font-size: 10px;
    color: var(--accent);
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding-top: 1px;
  }

  .indicators {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .ind {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 10px;
    color: var(--text-dim);
  }
  .agent {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 9px;
    color: var(--text-dim);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 1px 6px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .agent.active {
    color: var(--text);
    border-color: rgba(74, 158, 255, 0.4);
    background: rgba(74, 158, 255, 0.12);
  }
  .dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: #222;
    transition: all 0.2s;
  }
  .dot.on { background: var(--live); box-shadow: 0 0 6px rgba(74,255,107,0.4); }
  .dot.speaking { background: var(--accent); box-shadow: 0 0 8px rgba(74,158,255,0.5); animation: breathe 0.6s ease infinite; }
  .dot.warn { background: var(--warn); box-shadow: 0 0 8px rgba(255,171,74,0.5); }
  .dot.error { background: var(--command); }
  @keyframes breathe { 0%,100%{opacity:1} 50%{opacity:0.4} }
  #micRetry {
    border: 1px solid rgba(74, 158, 255, 0.45);
    background: rgba(74, 158, 255, 0.16);
    color: #bfd8ff;
    border-radius: 999px;
    padding: 2px 8px;
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    cursor: pointer;
  }
  #sidetoneToggle {
    border: 1px solid rgba(74, 158, 255, 0.45);
    background: rgba(74, 158, 255, 0.1);
    color: #a8c8ff;
    border-radius: 999px;
    padding: 2px 8px;
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    cursor: pointer;
  }

  #ticker {
    padding: 6px 60px 7px;
    border-bottom: 1px solid var(--border);
    background: var(--surface-2);
    min-height: 28px;
    font-size: 12px;
    color: #9dbdff;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  #wave {
    flex-shrink: 0;
    height: 120px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    position: relative;
    box-shadow: inset 0 -1px 0 rgba(74, 158, 255, 0.2);
    resize: vertical;
    overflow: hidden;
    min-height: 40px;
  }
  #waveCanvas { width:100%; height:100%; display:block; }
  #pitchStrip {
    flex-shrink: 0;
    height: 220px;
    border-bottom: 1px solid var(--border);
    background:
      linear-gradient(180deg, rgba(74, 158, 255, 0.1), rgba(4, 6, 14, 0.92));
    position: relative;
    box-shadow: 0 0 30px rgba(74, 158, 255, 0.15), inset 0 0 0 1px rgba(74, 158, 255, 0.18);
    resize: vertical;
    overflow: hidden;
    min-height: 60px;
  }
  #pitchCanvas { width:100%; height:100%; display:block; }
  #pitchStripLabel {
    position: absolute;
    left: 60px;
    top: 8px;
    font-size: 10px;
    color: rgba(173, 211, 255, 0.75);
    letter-spacing: 0.05em;
    text-transform: uppercase;
    pointer-events: none;
  }
  #pitchBadge {
    position: absolute;
    right: 60px;
    top: 7px;
    font-size: 11px;
    color: #a8c8ff;
    padding: 4px 10px;
    border-radius: 999px;
    border: 1px solid rgba(74, 158, 255, 0.35);
    background: rgba(8, 10, 24, 0.7);
    letter-spacing: 0.03em;
    text-transform: uppercase;
    box-shadow: 0 0 12px rgba(74, 158, 255, 0.22);
  }

  .stats {
    display: flex;
    gap: 32px;
    padding: 12px 60px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    color: var(--text-dim);
    background: var(--surface);
    flex-shrink: 0;
    align-items: center;
    font-weight: 500;
  }
  .sv { 
    color: var(--text); 
    font-weight: 600; 
    font-size: 16px;
    margin-left: 6px;
    display: inline-block;
    padding: 2px 8px;
    background: rgba(255, 255, 255, 0.05);
    border-radius: 4px;
    border: 1px solid rgba(255, 255, 255, 0.08);
  }

  #liveArea {
    flex-shrink: 0;
    min-height: 80px;
    max-height: 35vh;
    padding: 20px 60px;
    background: rgba(74, 158, 255, 0.03);
    border-bottom: 1px solid var(--border);
    font-size: 35px;
    line-height: 1.35;
    color: #e0e0f0;
    overflow-y: auto;
    display: flex;
    align-items: center;
    transition: background 0.3s;
    position: relative;
    font-weight: 500;
    letter-spacing: -0.01em;
  }
  #liveArea.speaking {
    background:
      repeating-linear-gradient(
        135deg,
        rgba(74, 158, 255, 0.08) 0 10px,
        rgba(74, 158, 255, 0.02) 10px 20px
      );
    animation: hashMove 1.5s linear infinite;
  }
  #liveArea.empty { color: var(--text-dim); font-size: 20px; font-style: italic; font-weight: normal; }
  .decay-text {
    animation: decayTextAnim 8s ease forwards;
  }
  @keyframes decayTextAnim {
    0% { color: #e0e0f0; opacity: 1; }
    40% { color: #a0a0b0; opacity: 1; }
    100% { color: #505060; opacity: 0.3; }
  }
  @keyframes hashMove { from { background-position: 0 0; } to { background-position: 26px 0; } }
  #liveCursor {
    display: inline-block;
    width: 2px;
    height: 14px;
    background: var(--accent);
    margin-left: 2px;
    vertical-align: text-bottom;
    animation: blink 0.8s step-end infinite;
  }
  #liveCursor.hidden { display: none; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }

  #taskPanel {
    flex-shrink: 0;
    border-bottom: 1px solid var(--border);
    background: rgba(74, 158, 255, 0.03);
    max-height: 180px;
    display: flex;
    flex-direction: column;
  }
  #taskHead {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 60px;
    font-size: 11px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    border-bottom: 1px solid var(--border);
    background: rgba(0, 0, 0, 0.12);
  }
  #taskCount {
    color: var(--warn);
    font-weight: 600;
  }
  #taskList {
    overflow-y: auto;
    padding: 10px 60px;
  }
  #taskList::-webkit-scrollbar { width: 4px; }
  #taskList::-webkit-scrollbar-thumb { background: #2b2f49; border-radius: 999px; }
  .task-item {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    border: 1px solid rgba(255,255,255,0.05);
    border-left: 3px solid rgba(255, 171, 74, 0.7);
    border-radius: 6px;
    padding: 10px 14px;
    margin-bottom: 8px;
    background: rgba(15, 15, 27, 0.7);
    font-size: 14px;
    line-height: 1.45;
    color: var(--text);
  }
  .task-item .task-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    margin-top: 6px;
    flex-shrink: 0;
    background: var(--warn);
    box-shadow: 0 0 6px rgba(255, 171, 74, 0.45);
  }
  .task-item .task-text {
    flex: 1;
    word-break: break-word;
  }

  #textFlow {
    flex: 1;
    overflow-y: auto;
    padding: 24px 60px;
    font-size: 16px;
    line-height: 1.8;
    color: var(--text);
  }
  #textFlow::-webkit-scrollbar { width: 3px; }
  #textFlow::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
  #viewBar {
    display: flex;
    gap: 8px;
    padding: 8px 60px;
    border-bottom: 1px solid var(--border);
    background: rgba(6, 6, 14, 0.9);
    flex-shrink: 0;
  }
  .viewBtn {
    border: 1px solid rgba(74, 158, 255, 0.3);
    background: rgba(74, 158, 255, 0.08);
    color: #a9c6f8;
    border-radius: 6px;
    font-size: 10px;
    letter-spacing: 0.04em;
    padding: 4px 7px;
    cursor: pointer;
    text-transform: uppercase;
  }
  .viewBtn.active {
    border-color: rgba(74, 158, 255, 0.72);
    background: rgba(74, 158, 255, 0.2);
    color: #e3efff;
  }
  .hidden { display: none !important; }
  #paraFlow {
    white-space: pre-wrap;
    color: #ced5e8;
  }
  .para-frag {
    animation: fadeword 0.25s ease;
  }

  .turn {
    display: block;
    border: 1px solid rgba(255,255,255,0.04);
    border-left: 2px solid rgba(74, 158, 255, 0.45);
    border-radius: 7px;
    background: rgba(12, 12, 20, 0.75);
    padding: 12px 16px;
    margin-bottom: 10px;
    animation: fadeword 0.3s ease;
    position: relative;
    overflow: hidden;
  }
  .turn.cmd { border-left-color: var(--warn); color: #e8e8f0; font-weight: 500; }
  .turn-text {
    white-space: pre-wrap;
    word-break: break-word;
  }
  .turn-meta {
    margin-top: 4px;
    font-size: 10px;
    color: #8490b6;
    letter-spacing: 0.02em;
  }
  .turn-stamp {
    display: inline-block;
    color: #98b8ff;
    font-size: 11px;
    letter-spacing: 0.03em;
    margin-right: 6px;
  }
  .turn.processing::after {
    content: "";
    position: absolute;
    top: 0;
    left: -35%;
    width: 35%;
    height: 100%;
    background: linear-gradient(
      90deg,
      rgba(255, 171, 74, 0),
      rgba(255, 171, 74, 0.18),
      rgba(255, 171, 74, 0)
    );
    animation: flare 1.2s ease;
    pointer-events: none;
  }
  @keyframes flare { from { transform: translateX(0); } to { transform: translateX(420%); } }
  .turn.new {
    color: #b8d8ff;
    animation: coolfade 6s ease forwards;
  }
  @keyframes coolfade {
    0% { color: #cbe3ff; text-shadow: 0 0 7px rgba(74, 158, 255, 0.35); }
    100% { color: var(--text); text-shadow: none; }
  }
  .turn-sep { display: none; }

  @keyframes fadeword {
    from { opacity: 0; }
    to { opacity: 1; }
  }

  .placeholder {
    color: var(--text-dim);
    text-align: center;
    padding: 40px 16px;
    font-size: 13px;
  }

  footer {
    padding: 8px 60px;
    border-top: 1px solid var(--border);
    font-size: 10px;
    color: var(--text-dim);
    background: var(--surface);
    display: flex;
    justify-content: space-between;
    flex-shrink: 0;
  }

  #composer {
    padding: 12px 60px;
    border-top: 1px solid var(--border);
    background: var(--surface);
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }
  #quickInput {
    flex: 1;
    background: #0d1020;
    color: var(--text);
    border: 1px solid #2a2f4d;
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 14px;
    font-family: inherit;
    outline: none;
  }
  #quickInput:focus {
    border-color: rgba(74, 158, 255, 0.7);
    box-shadow: 0 0 0 2px rgba(74, 158, 255, 0.14);
  }
  #quickSend {
    border: 1px solid rgba(74, 158, 255, 0.6);
    background: rgba(74, 158, 255, 0.2);
    color: #d8e8ff;
    border-radius: 6px;
    padding: 7px 10px;
    font-size: 11px;
    font-family: inherit;
    cursor: pointer;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
</style>
</head>
<body>
<header>
  <span class="title">Realtime</span>
  <span id="topDateTime"></span>
  <div class="indicators">
    <div class="ind"><div class="dot" id="micDot"></div><span id="micLabel">mic</span></div>
    <div class="ind"><div class="dot" id="vadDot"></div><span>vad</span></div>
    <div class="ind"><div class="dot" id="srvDot"></div><span id="srvLabel">stream</span></div>
    <button id="micRetry" type="button">mic retry</button>
    <button id="sidetoneToggle" type="button">sidetone on</button>
    <div class="agent" id="agentDolphin"><div class="dot" id="dolphinDot"></div><span>dolphin</span></div>
    <div class="agent" id="agentTask"><div class="dot" id="taskDot"></div><span>task</span></div>
    <div class="agent" id="agentRouter"><div class="dot" id="routerDot"></div><span>router</span></div>
  </div>
</header>

<div id="ticker">booting stream...</div>

<div id="wave"><canvas id="waveCanvas"></canvas><span id="pitchBadge">pitch --</span></div>
<div id="pitchStrip"><canvas id="pitchCanvas"></canvas><span id="pitchStripLabel">pitch eeg timeline</span></div>

<div class="stats">
  <span>turns <span class="sv" id="sTurns">0</span></span>
  <span>words <span class="sv" id="sWords">0</span></span>
  <span>audio <span class="sv" id="sAudio">0s</span></span>
  <span>pitch <span class="sv" id="sPitch">--</span></span>
  <span>commands <span class="sv" id="sCmds">0</span></span>
</div>

  <div id="liveArea" class="empty">
  <span id="liveText">ready for speech</span><span id="liveCursor" class="hidden"></span>
</div>

<div id="taskPanel">
  <div id="taskHead">
    <span>Detected Commands & Actions</span>
    <span id="taskCount">0</span>
  </div>
  <div id="taskList">
    <div class="placeholder">Voice commands parsed by the local LLM will appear here</div>
  </div>
</div>

<div id="viewBar">
  <button id="viewParagraph" class="viewBtn active" type="button">paragraph</button>
  <button id="viewCards" class="viewBtn" type="button">cards</button>
</div>

<div id="textFlow">
  <div id="paraFlow">transcription will appear here as a rolling paragraph</div>
  <div id="cardFlow" class="hidden"></div>
</div>

<div id="composer">
  <input id="quickInput" type="text" placeholder="Type a quick note or command, then Enter">
  <button id="quickSend" type="button">add</button>
</div>

<footer>
  <span>realtime</span>
  <span id="clock"></span>
</footer>

<script>
const $ = id => document.getElementById(id);
const canvas = $('waveCanvas');
const ctx = canvas.getContext('2d');
const pitchCanvas = $('pitchCanvas');
const pitchCtx = pitchCanvas.getContext('2d');
const liveArea = $('liveArea');
const liveText = $('liveText');
const liveCursor = $('liveCursor');
const textFlow = $('textFlow');
const paraFlow = $('paraFlow');
const cardFlow = $('cardFlow');
const ticker = $('ticker');
const taskListEl = $('taskList');
const pitchBadge = $('pitchBadge');
const ENABLE_SOUNDS = false;
const SIMPLE_STREAM_MODE = true;

let audioCtx, analyser, dataArray, pitchArray, micSource, monitorGain;
let totalTurns = 0;
let totalWords = 0;
let totalAudio = 0;
let totalCommands = 0;
let hasFlowContent = false;
let flowMode = 'paragraph';
let currentItemText = {};
let isSpeaking = false;
let autoScroll = true;
let seen = new Set();
let tasks = [];
let taskKeys = new Set();
let pitchHistory = [];
let turnPitchSamples = [];
let currentPitchHz = 0;
let currentPitchNote = '--';
let itemSentenceCursor = {};
let lastSoundAt = 0;
let steerSeen = new Set();
let livePulse = 0;
let lastSteerPostAt = 0;
let lastSteerPrefix = '';
let daemonMicHot = 0;
let lastMicErrorMsg = '';
let sidetoneEnabled = true;
let sidetoneLevel = 0.06;
let pitchTrace = [];
const PITCH_WINDOW_MS = 60000;
const HARMONIC_CENTS_WINDOW = 18;
let wasHarmonicHit = false;

let lastDeltaAt = 0;
let lastCommandAt = 0;
let lastCompleteAt = 0;
let lastTaskAt = 0;
let lastTurnStampAt = 0;

function formatDur(s) {
  if (!s) return '0s';
  if (s < 60) return Math.round(s) + 's';
  return Math.floor(s / 60) + 'm' + Math.round(s % 60) + 's';
}

function formatClockLabel(ts) {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

function shouldShowTurnStamp(ts) {
  return !lastTurnStampAt || (ts - lastTurnStampAt) >= 60000;
}

function normalizeCommandTextForDisplay(text) {
  const raw = (text || '').trim();
  return raw
    .replace(/^\s*go\s+dolphin\s*[,:\-]?\s*/i, 'route to DeepDolphin: ')
    .replace(/^\s*dolphin\s*[,:\-]?\s*/i, 'route to DeepDolphin: ');
}

function setLiveWaiting(message) {
  if (!liveText.textContent || liveText.textContent === 'ready for speech' || liveText.textContent === 'waiting for speech') {
    liveText.textContent = message || 'ready for speech';
    liveArea.classList.add('empty');
  }
  liveArea.classList.remove('speaking');
  liveCursor.classList.add('hidden');
}

function setLiveSpeaking() {
  if (liveText.textContent === 'ready for speech' || liveText.textContent === 'waiting for speech') {
    liveText.textContent = '';
  } else if (liveText.querySelector('.decay-text')) {
    liveText.textContent = ''; // clear decayed text when starting new speech
  }
  liveArea.classList.remove('empty');
  liveArea.classList.add('speaking');
}

function hasActiveLiveText() {
  return Object.values(currentItemText).some(v => (v || '').trim().length > 0);
}

function setDot(id, state) {
  const dot = $(id);
  dot.className = 'dot';
  if (state) dot.classList.add(state);
}

function setAgent(id, active) {
  const el = $(id);
  if (!el) return;
  el.classList.toggle('active', !!active);
}

function setTicker(message) {
  ticker.textContent = new Date().toLocaleTimeString('en-US', { hour12: false }) + '  ' + message;
}

function updateStats() {
  $('sTurns').textContent = totalTurns;
  $('sWords').textContent = totalWords;
  $('sAudio').textContent = formatDur(totalAudio);
  $('sCmds').textContent = totalCommands;
}

function updateHeaderClock() {
  const now = new Date();
  const timeText = now.toLocaleTimeString('en-US', { hour12: false });
  $('clock').textContent = timeText;
  const dateText = now.toLocaleDateString('en-US', {
    weekday: 'short',
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
  $('topDateTime').textContent = dateText + '  ' + timeText;
}

function applySidetoneState() {
  const btn = $('sidetoneToggle');
  if (btn) {
    btn.textContent = sidetoneEnabled ? 'sidetone on' : 'sidetone off';
    btn.style.opacity = sidetoneEnabled ? '1' : '0.75';
  }
  if (monitorGain) {
    monitorGain.gain.value = sidetoneEnabled ? sidetoneLevel : 0;
  }
}

function hzToNote(hz) {
  const info = pitchNoteInfo(hz);
  if (!info) return '--';
  return info.note;
}

function pitchNoteInfo(hz) {
  if (!hz || !isFinite(hz)) return null;
  const noteNames = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];
  const midiFloat = 69 + (12 * Math.log2(hz / 440));
  const midi = Math.round(midiFloat);
  const cents = (midiFloat - midi) * 100;
  const noteHz = 440 * Math.pow(2, (midi - 69) / 12);
  const name = noteNames[((midi % 12) + 12) % 12];
  const octave = Math.floor(midi / 12) - 1;
  const note = name + octave;
  return {
    note,
    midi,
    cents,
    noteHz,
    inTune: Math.abs(cents) <= HARMONIC_CENTS_WINDOW,
  };
}

function estimatePitchHz(floatData, sampleRate) {
  const size = floatData.length;
  if (size < 2) return 0;

  let rms = 0;
  for (let i = 0; i < size; i++) rms += floatData[i] * floatData[i];
  rms = Math.sqrt(rms / size);
  if (rms < 0.015) return 0;

  let bestOffset = -1;
  let bestCorr = 0;
  const minHz = 65;
  const maxHz = 880;
  const minOffset = Math.floor(sampleRate / maxHz);
  const maxOffset = Math.floor(sampleRate / minHz);

  for (let offset = minOffset; offset <= maxOffset; offset++) {
    let corr = 0;
    for (let i = 0; i < size - offset; i++) {
      corr += floatData[i] * floatData[i + offset];
    }
    if (corr > bestCorr) {
      bestCorr = corr;
      bestOffset = offset;
    }
  }

  if (bestOffset <= 0 || bestCorr < 0.001) return 0;
  const hz = sampleRate / bestOffset;
  // Increase range to track pitch more accurately (up to C6 and down to lower voice limits)
  if (hz < 65 || hz > 880) return 0;
  return hz;
}

function summarizePitch(samples) {
  if (!samples || samples.length < 3) return null;
  let min = samples[0];
  let max = samples[0];
  let sum = 0;
  for (const hz of samples) {
    if (hz < min) min = hz;
    if (hz > max) max = hz;
    sum += hz;
  }
  const avg = sum / samples.length;
  return {
    avg,
    min,
    max,
    note: hzToNote(avg),
    samples: samples.length,
  };
}

function pitchToY(hz, h) {
  // 4-octave log scale from C2 (65.4Hz) to C6 (1046.5Hz)
  const minMidi = 36; // C2
  const maxMidi = 84; // C6
  
  if (hz <= 0) return null; // No line at all when no pitch
  
  const midiFloat = 69 + (12 * Math.log2(hz / 440));
  const clamped = Math.max(minMidi, Math.min(maxMidi, midiFloat));
  
  const norm = (clamped - minMidi) / (maxMidi - minMidi);
  // Invert Y (high pitch = top of canvas = low Y)
  // Give it slightly more padding at top and bottom so lines don't hit edge
  return h - ((norm * h * 0.7) + (h * 0.15));
}

function drawPitchEeg(nowMs) {
  const w = pitchCanvas.width / devicePixelRatio;
  const h = pitchCanvas.height / devicePixelRatio;
  pitchCtx.clearRect(0, 0, w, h);

  // EEG-style graph paper: minor + major grid, drifting in real time.
  const minorX = 20;
  const majorXEvery = 5;
  const minorY = 8;
  const majorYEvery = 5;
  const gridDriftX = ((nowMs * 0.018) % minorX);
  const gridDriftY = Math.sin(nowMs / 2200) * 2.2;

  pitchCtx.fillStyle = 'rgba(8, 15, 24, 0.62)';
  pitchCtx.fillRect(0, 0, w, h);

  for (let gx = -minorX + gridDriftX; gx <= w + minorX; gx += minorX) {
    const gIndex = Math.round((gx - gridDriftX) / minorX);
    const major = (Math.abs(gIndex) % majorXEvery) === 0;
    pitchCtx.beginPath();
    pitchCtx.strokeStyle = major ? 'rgba(122, 176, 255, 0.16)' : 'rgba(122, 176, 255, 0.08)';
    pitchCtx.lineWidth = major ? 1.15 : 1;
    pitchCtx.moveTo(gx, 0);
    pitchCtx.lineTo(gx, h);
    pitchCtx.stroke();
  }

  // Logarithmic musical staff lines
  const minMidi = 36; // C2
  const maxMidi = 84; // C6
  
  for (let m = minMidi; m <= maxMidi; m++) {
    const isC = m % 12 === 0;
    const isF = m % 12 === 5;
    const isG = m % 12 === 7;
    
    if (isC || isF || isG) {
      const norm = (m - minMidi) / (maxMidi - minMidi);
      // Give it slightly more padding at top and bottom so lines don't hit edge
      const gy = h - ((norm * h * 0.7) + (h * 0.15));
      
      pitchCtx.beginPath();
      pitchCtx.strokeStyle = isC ? 'rgba(255, 255, 255, 0.12)' : 'rgba(255, 255, 255, 0.05)';
      pitchCtx.lineWidth = isC ? 1.5 : 1;
      pitchCtx.moveTo(0, gy);
      pitchCtx.lineTo(w, gy);
      pitchCtx.stroke();
      
      // Label C notes
      if (isC) {
        pitchCtx.fillStyle = 'rgba(255, 255, 255, 0.3)';
        pitchCtx.font = '10px monospace';
        pitchCtx.fillText('C' + (Math.floor(m / 12) - 1), 60, gy - 4);
      }
    }
  }

  // Neutral pitch baseline (middle of canvas)
  pitchCtx.beginPath();
  pitchCtx.strokeStyle = 'rgba(74, 158, 255, 0.26)';
  pitchCtx.lineWidth = 1;
  pitchCtx.moveTo(0, h / 2);
  pitchCtx.lineTo(w, h / 2);
  pitchCtx.stroke();

  const active = pitchTrace.filter(p => nowMs - p.t <= PITCH_WINDOW_MS);
  const toPoint = (p) => {
    const age = nowMs - p.t;
    // Stretch x coordinate calculation across the wider window
    const x = w - (age / PITCH_WINDOW_MS) * w;
    const y = p.hz > 0 ? pitchToY(p.hz, h) : null;
    return {
      x,
      y,
      hz: p.hz,
      harmonicHit: !!p.harmonicHit,
      harmonicOnset: !!p.harmonicOnset,
    };
  };

  if (active.length) {
    let started = false;
    pitchCtx.beginPath();
    for (const p of active) {
      const pt = toPoint(p);
      if (pt.y === null) {
        started = false;
        continue;
      }
      if (!started) {
        pitchCtx.moveTo(pt.x, pt.y);
        started = true;
      } else {
        pitchCtx.lineTo(pt.x, pt.y);
      }
    }
    pitchCtx.strokeStyle = 'rgba(136, 206, 255, 0.36)';
    pitchCtx.lineWidth = 4.2;
    pitchCtx.stroke();

    started = false;
    pitchCtx.beginPath();
    for (const p of active) {
      const pt = toPoint(p);
      if (pt.y === null) {
        started = false;
        continue;
      }
      if (!started) {
        pitchCtx.moveTo(pt.x, pt.y);
        started = true;
      } else {
        pitchCtx.lineTo(pt.x, pt.y);
      }
    }
    pitchCtx.strokeStyle = 'rgba(66, 196, 255, 0.95)';
    pitchCtx.lineWidth = 1.5;
    pitchCtx.stroke();
  }

  // Harmonic hit markers (fat onset dot when pitch lands near note center).
  if (active.length) {
    for (const p of active) {
      if (!p.harmonicHit) continue;
      const pt = toPoint(p);
      if (pt.y === null) continue;
      
      const r = p.harmonicOnset ? 6.2 : 3.0;
      
      pitchCtx.beginPath();
      if (p.harmonicOnset) {
        pitchCtx.fillStyle = 'rgba(255, 203, 136, 0.3)';
        pitchCtx.arc(pt.x, pt.y, r + 8, 0, Math.PI * 2);
        pitchCtx.fill();
        pitchCtx.beginPath();
      }
      
      pitchCtx.fillStyle = p.harmonicOnset ? 'rgba(255, 203, 136, 0.96)' : 'rgba(255, 203, 136, 0.62)';
      pitchCtx.arc(pt.x, pt.y, r, 0, Math.PI * 2);
      pitchCtx.fill();
      
      if (p.harmonicOnset) {
        pitchCtx.beginPath();
        pitchCtx.strokeStyle = 'rgba(255, 203, 136, 0.58)';
        pitchCtx.lineWidth = 4; // reduced from 9 for performance
        pitchCtx.arc(pt.x, pt.y, r + 2, 0, Math.PI * 2);
        pitchCtx.stroke();
      }
    }
  }
}

function updatePitchReadout(hz, info = null) {
  if (!hz) {
    currentPitchHz = 0;
    currentPitchNote = '--';
    $('sPitch').textContent = '--';
    pitchBadge.textContent = 'pitch --';
    return;
  }
  const noteInfo = info || pitchNoteInfo(hz);
  currentPitchHz = hz;
  currentPitchNote = noteInfo ? noteInfo.note : hzToNote(hz);
  const hzText = hz.toFixed(1) + 'Hz';
  let noteText = currentPitchNote;
  if (noteInfo) {
    const cents = Math.round(noteInfo.cents);
    const centsText = (cents > 0 ? '+' : '') + String(cents) + 'c';
    if (noteInfo.inTune) {
      noteText += ' ' + centsText + ' hit';
    } else {
      noteText += ' ' + centsText;
    }
  }
  $('sPitch').textContent = hzText + ' ' + noteText;
  pitchBadge.textContent = 'pitch ' + hzText + ' ' + noteText;
}

async function playAck(text) {
  if (!ENABLE_SOUNDS) return;
  const phrase = (text || '').trim() || 'Task queued.';
  try {
    const resp = await fetch('/ack', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: phrase }),
    });
    if (!resp.ok) throw new Error('ack failed');
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.volume = 0.75;
    audio.play().catch(() => {});
    audio.onended = () => URL.revokeObjectURL(url);
    return;
  } catch (_) {
    // Browser/endpoint fallback: local synth chirp
  }

  try {
    if (!audioCtx) return;
    if (audioCtx.state === 'suspended') await audioCtx.resume();
    const now = audioCtx.currentTime + 0.01;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(760, now);
    osc.frequency.exponentialRampToValueAtTime(980, now + 0.11);
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.06, now + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.16);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start(now);
    osc.stop(now + 0.17);
  } catch (_) {
    // no-op
  }
}

function laughterSuffix(text) {
  const src = (text || '').toLowerCase();
  const haCount = (src.match(/\bha\b/g) || []).length;
  const lolCount = (src.match(/\blol\b/g) || []).length;
  const score = haCount + (lolCount * 2);
  if (score < 3) return '';
  return ' ' + '😂'.repeat(Math.min(3, Math.floor(score / 2)));
}

function typewriteLine(el, text) {
  const full = (text || '') + laughterSuffix(text);
  if (!full) {
    el.textContent = '';
    return;
  }
  let idx = 0;
  const stride = Math.max(2, Math.min(8, Math.floor(full.length / 26)));
  function step() {
    idx = Math.min(full.length, idx + stride);
    el.textContent = full.slice(0, idx);
    if (idx < full.length) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

async function playEventSound(kind) {
  if (!ENABLE_SOUNDS) return;
  const nowMs = Date.now();
  if (nowMs - lastSoundAt < 180) return;
  lastSoundAt = nowMs;
  try {
    if (!audioCtx) return;
    if (audioCtx.state === 'suspended') await audioCtx.resume();
    const now = audioCtx.currentTime + 0.005;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'triangle';
    if (kind === 'task') {
      osc.frequency.setValueAtTime(590, now);
      osc.frequency.exponentialRampToValueAtTime(900, now + 0.1);
    } else if (kind === 'received') {
      osc.frequency.setValueAtTime(420, now);
      osc.frequency.exponentialRampToValueAtTime(620, now + 0.09);
    } else if (kind === 'working') {
      osc.frequency.setValueAtTime(320, now);
      osc.frequency.exponentialRampToValueAtTime(460, now + 0.08);
    } else {
      osc.frequency.setValueAtTime(700, now);
      osc.frequency.exponentialRampToValueAtTime(760, now + 0.06);
    }
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.045, now + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.13);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start(now);
    osc.stop(now + 0.14);
  } catch (_) {
    // no-op
  }
}

function inferCommand(text) {
  return /\b(call|spin up|task list|send to|someone act|route this)\b/i.test(text || '');
}

function extractSteerCandidate(text) {
  const raw = (text || '').trim();
  if (!raw || raw.length < 8) return '';
  const steerCue =
    /\b(go dolphin|dolphin[,:\s]+(work|read|listen|go)|call [a-z0-9_-]+|spin up|route this|someone act|stop with|start|switch to|use orpheus|steer)\b/i;
  if (!steerCue.test(raw)) return '';
  const clean = raw.replace(/\s+/g, ' ').trim();
  return clean.length > 220 ? clean.slice(0, 217) + '...' : clean;
}

async function captureSteer(text, source, item) {
  const candidate = extractSteerCandidate(text);
  if (!candidate) return;
  const src = source || 'delta';
  const norm = normalizeTaskKey(candidate);
  if (src === 'delta') {
    const now = Date.now();
    const terminal = /[.!?]\s*$/.test(candidate);
    if (!terminal && candidate.split(/\s+/).length < 10) return;
    if (now - lastSteerPostAt < 900) return;
    if (lastSteerPrefix && norm.startsWith(lastSteerPrefix)) return;
    lastSteerPostAt = now;
    lastSteerPrefix = norm.slice(0, 80);
  }
  const key = norm;
  if (steerSeen.has(key)) return;
  steerSeen.add(key);
  if (steerSeen.size > 4000) {
    steerSeen = new Set(Array.from(steerSeen).slice(-2000));
  }
  try {
    const resp = await fetch('/steer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: candidate, source: src, item: item || '' }),
    });
    if (resp.ok) {
      const data = await resp.json();
      setAgent('agentRouter', true);
      if (data && data.route && data.route.approved === false) {
        setTicker('steer held: ' + (data.route.reason || 'filtered'));
      } else {
        setTicker('steer captured');
      }
      playEventSound('received');
      if (data && data.dispatch && data.dispatch.ok && !data.dispatch.duplicate) {
        setTicker('steer routed to dolphin ' + (data.dispatch.mission_id || ''));
        playAck('Steer command routed.');
      }
      return;
    }
  } catch (_) {
    // no-op
  }
  setTicker('steer captured (local)');
}

function normalizeTaskKey(text) {
  return (text || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function normalizeTaskDisplayText(text) {
  return normalizeCommandTextForDisplay(text || '');
}

function extractTaskCandidate(text) {
  const raw = (text || '').trim();
  if (!raw || raw.length < 16) return '';
  if (!/[a-zA-Z]{3}/.test(raw)) return '';

  const signal =
    /\b(need to|we should|there should|would be nice|i want|can you|please|interested in|next enhancement|task list|someone .* do|add |make |build |wire |spin up|set up)\b/i.test(raw);
  if (!signal) return '';

  const cleaned = raw
    .replace(/^(uh+|um+|hmm+|you know|i mean|so|well)[,\s]+/i, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!cleaned) return '';
  return cleaned.length > 180 ? cleaned.slice(0, 177) + '...' : cleaned;
}

function renderTasks() {
  $('taskCount').textContent = String(tasks.length);
  if (!tasks.length) {
    taskListEl.innerHTML = '<div class="placeholder">Voice commands parsed by the local LLM will appear here</div>';
    return;
  }
  let html = '';
  for (const task of tasks.slice(0, 80)) {
    const displayText = normalizeTaskDisplayText(task.text);
    html += '<div class="task-item"><span class="task-dot"></span><span class="task-text">' +
      displayText.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') +
      '</span></div>';
  }
  taskListEl.innerHTML = html;
}

function addTaskLocal(taskText, silent) {
  const key = normalizeTaskKey(taskText);
  if (!key || taskKeys.has(key)) return false;
  taskKeys.add(key);
  tasks.unshift({
    id: 'local-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7),
    text: taskText,
    t: new Date().toISOString(),
  });
  if (tasks.length > 500) tasks = tasks.slice(0, 500);
  renderTasks();
  if (!silent) {
    lastTaskAt = Date.now();
    setTicker('task extracted');
  }
  return true;
}

function scanDeltaSentences(item) {
  const text = currentItemText[item] || '';
  if (!text) return;
  let cursor = itemSentenceCursor[item] || 0;
  if (cursor < 0 || cursor > text.length) cursor = 0;

  for (let i = cursor; i < text.length; i++) {
    const ch = text[i];
    if (ch !== '.' && ch !== '?' && ch !== '!') continue;
    const sentence = text.slice(cursor, i + 1).trim();
    cursor = i + 1;
    if (sentence.length >= 20) {
      captureTask(sentence, 'delta-sentence');
      setAgent('agentTask', true);
      playEventSound('working');
    }
  }
  itemSentenceCursor[item] = cursor;
}

async function captureTask(text, source) {
  const candidate = extractTaskCandidate(text);
  if (!candidate) return;

  try {
    const resp = await fetch('/task', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: candidate, source }),
    });
    if (resp.ok) {
      const data = await resp.json();
      if (data && data.task && data.task.text) {
        addTaskLocal(data.task.text, false);
        if (data.dispatch && data.dispatch.ok && !data.dispatch.duplicate) {
          lastTaskAt = Date.now();
          setAgent('agentTask', true);
          setTicker('task queued to dolphin ' + (data.dispatch.mission_id || ''));
          playEventSound('task');
          playAck('Task queued to dolphin.');
        } else if (data.dispatch && data.dispatch.duplicate) {
          setTicker('task already queued');
        } else {
          setTicker('task captured');
          playEventSound('task');
          playAck('Task captured.');
        }
        return;
      }
      if (data && data.duplicate) return;
    }
  } catch (_) {
    // Fall through to local add.
  }
  if (addTaskLocal(candidate, false)) {
    playEventSound('task');
    playAck('Task captured.');
  }
}

async function loadTasks() {
  try {
    const resp = await fetch('/tasks', { cache: 'no-store' });
    if (!resp.ok) return;
    const data = await resp.json();
    const items = Array.isArray(data.tasks) ? data.tasks : [];
    tasks = [];
    taskKeys = new Set();
    for (const task of items.slice(-200).reverse()) {
      if (!task || !task.text) continue;
      addTaskLocal(task.text, true);
    }
  } catch (_) {
    // no-op
  }
}

function addTurn(text, type, t, dur, pitchMeta) {
  const clean = (text || '').trim();
  if (!clean) return;
  const turnTs = typeof t === 'number' ? t : (Date.parse(t || '') || Date.now());
  const showStamp = shouldShowTurnStamp(turnTs);
  const displayText = normalizeCommandTextForDisplay(clean);

  if (!hasFlowContent) {
    paraFlow.textContent = '';
    cardFlow.textContent = '';
    hasFlowContent = true;
  }

  if (SIMPLE_STREAM_MODE) {
    if (paraFlow.textContent.trim().length > 0) {
      paraFlow.appendChild(document.createTextNode(' '));
    }
    if (showStamp && paraFlow.textContent.trim().length > 0) {
      const stamp = document.createElement('span');
      stamp.className = 'turn-stamp';
      stamp.textContent = `[${formatClockLabel(turnTs)}] `;
      paraFlow.appendChild(stamp);
    }
    const textNode = document.createElement('span');
    paraFlow.appendChild(textNode);
    typewriteLine(textNode, displayText);
    if (autoScroll) textFlow.scrollTop = textFlow.scrollHeight;
    lastTurnStampAt = turnTs;
    return;
  }

  const hadParaText = paraFlow.textContent.trim().length > 0;
  if (showStamp) {
    if (hadParaText) paraFlow.appendChild(document.createTextNode('\n'));
    const stamp = document.createElement('span');
    stamp.className = 'turn-stamp';
    stamp.textContent = `[${formatClockLabel(turnTs)}] `;
    paraFlow.appendChild(stamp);
  } else if (hadParaText) {
    paraFlow.appendChild(document.createTextNode(' '));
  }

  const frag = document.createElement('span');
  frag.className = 'para-frag';
  paraFlow.appendChild(frag);
  typewriteLine(frag, displayText);

  const turn = document.createElement('div');
  turn.className = 'turn new processing' + (type === 'command' ? ' cmd' : '');
  const body = document.createElement('div');
  body.className = 'turn-text';
  typewriteLine(body, displayText);
  turn.appendChild(body);

  const metaBits = [];
  if (showStamp) {
    metaBits.push(formatClockLabel(turnTs));
  }
  metaBits.push(formatDur(dur || 0));
  if (pitchMeta && pitchMeta.avg) {
    metaBits.push('pitch ' + pitchMeta.note + ' ' + Math.round(pitchMeta.avg) + 'Hz');
    metaBits.push('range ' + Math.round(pitchMeta.min) + '-' + Math.round(pitchMeta.max) + 'Hz');
  }

  const meta = document.createElement('div');
  meta.className = 'turn-meta';
  meta.textContent = metaBits.join(' | ');
  turn.appendChild(meta);
  turn.title = metaBits.join(' | ');

  if (metaBits.length === 0) {
    turn.title = displayText;
  }

  cardFlow.appendChild(turn);

  setTimeout(() => turn.classList.remove('processing'), 1300);
  setTimeout(() => turn.classList.remove('new'), 6200);
    if (autoScroll) textFlow.scrollTop = textFlow.scrollHeight;
  lastTurnStampAt = turnTs;
  return;
}

textFlow.addEventListener('scroll', () => {
  autoScroll = (textFlow.scrollTop + textFlow.clientHeight) >= (textFlow.scrollHeight - 150);
});

function setViewMode(mode) {
  if (SIMPLE_STREAM_MODE) {
    flowMode = 'paragraph';
    const bar = $('viewBar');
    if (bar) bar.classList.add('hidden');
    paraFlow.classList.remove('hidden');
    cardFlow.classList.add('hidden');
    if (autoScroll) textFlow.scrollTop = textFlow.scrollHeight;
    return;
  }
  flowMode = mode === 'cards' ? 'cards' : 'paragraph';
  const paragraphMode = flowMode === 'paragraph';
  paraFlow.classList.toggle('hidden', !paragraphMode);
  cardFlow.classList.toggle('hidden', paragraphMode);
  $('viewParagraph').classList.toggle('active', paragraphMode);
  $('viewCards').classList.toggle('active', !paragraphMode);
    if (autoScroll) textFlow.scrollTop = textFlow.scrollHeight;
}

function handleEvent(ev) {
  const key = (ev.type || ev.cls || 'legacy') + '|' + (ev.t || '') + '|' + (ev.item || '') + '|' + (ev.text || '');
  if (seen.has(key)) return;
  seen.add(key);
  if (seen.size > 5000) {
    seen = new Set(Array.from(seen).slice(-2500));
  }

  const t = ev.type;

  if (t === 'delta') {
    setLiveSpeaking();
    const item = ev.item || 'unknown';
    if (!currentItemText[item]) currentItemText[item] = '';
    const piece = ev.text || '';
    currentItemText[item] += piece;
    liveText.textContent += piece;
    scanDeltaSentences(item);
    captureSteer(currentItemText[item], 'delta', item);
    livePulse = 1.0;
    liveArea.scrollTop = liveArea.scrollHeight;
    if (Date.now() - lastDeltaAt > 1200) {
      playEventSound('working');
    }
    lastDeltaAt = Date.now();
    setAgent('agentDolphin', true);
    setAgent('agentRouter', true);
    setDot('dolphinDot', 'on');

  } else if (t === 'completed') {
    const text = ev.text || '';
    if (!text.trim()) return;

    currentItemText = {};
    itemSentenceCursor = {};
    liveText.innerHTML = '<span class="decay-text">' + text.replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</span>';
    setLiveWaiting('');

    const command = inferCommand(text);
    const pitchMeta = summarizePitch(turnPitchSamples);
    turnPitchSamples = [];
    addTurn(text, command ? 'command' : 'speech', ev.t, ev.dur, pitchMeta);

    totalTurns += 1;
    totalWords += (ev.words || text.trim().split(/\s+/).length);
    totalAudio += (ev.dur || 0);
    if (command) {
      totalCommands += 1;
      lastCommandAt = Date.now();
      setTicker('command-like turn detected');
    } else {
      setTicker('turn committed');
    }
    playEventSound('received');
    captureTask(text, 'completed');
    captureSteer(text, 'completed', ev.item || '');
    updateStats();
    lastCompleteAt = Date.now();

  } else if (t === 'vad') {
    isSpeaking = ev.speaking;
    setDot('vadDot', ev.speaking ? 'speaking' : '');
    if (ev.speaking) {
      turnPitchSamples = [];
      setLiveSpeaking();
      setTicker('speech started');
    } else {
      if (!hasActiveLiveText() && !liveText.querySelector('.decay-text')) {
        setLiveWaiting('ready for speech');
      } else if (!hasActiveLiveText() && liveText.querySelector('.decay-text')) {
        setLiveWaiting('');
      } else {
        setLiveSpeaking();
      }
    }

  } else if (t === 'status') {
    if (ev.text) setTicker('status ' + ev.text);

  } else if (t === 'manual') {
    const text = ev.text || '';
    if (!text.trim()) return;
    const command = inferCommand(text);
    addTurn(text, command ? 'command' : 'speech', ev.t, 0, null);
    totalTurns += 1;
    totalWords += (ev.words || text.trim().split(/\s+/).length);
    if (command) {
      totalCommands += 1;
      lastCommandAt = Date.now();
    }
    captureTask(text, 'manual');
    captureSteer(text, 'manual', ev.item || '');
    updateStats();
    setTicker('manual note added');
    playEventSound('received');

  } else if (ev.cls) {
    const text = (ev.text || '').trim();
    if (!text) return;
    const command = ev.cls === 'command' || inferCommand(text);
    addTurn(text, command ? 'command' : 'speech', ev.t, ev.dur, null);

    totalTurns += 1;
    totalWords += (ev.words || text.split(/\s+/).length);
    totalAudio += (ev.dur || 0);
    if (command) {
      totalCommands += 1;
      lastCommandAt = Date.now();
    }
    captureTask(text, 'legacy');
    captureSteer(text, 'legacy', ev.item || '');
    updateStats();
    lastCompleteAt = Date.now();
    playEventSound('received');
  }
}

function updateAgentLiveness() {
  const now = Date.now();
  const deltaLive = now - lastDeltaAt < 12000;
  const taskLive = now - Math.max(lastTaskAt, lastCommandAt) < 12000;
  const completeLive = now - lastCompleteAt < 12000;

  setAgent('agentDolphin', deltaLive);
  setAgent('agentTask', taskLive);
  setAgent('agentRouter', completeLive);

  setDot('dolphinDot', deltaLive ? 'on' : '');
  setDot('taskDot', taskLive ? 'warn' : '');
  setDot('routerDot', completeLive ? 'on' : '');
}

async function pollStatus() {
  try {
    const resp = await fetch('/status', { cache: 'no-store' });
    if (!resp.ok) return;
    const data = await resp.json();
    const rms = Number(data.mic_rms || 0);
    daemonMicHot = Math.max(0, Math.min(1, rms * 4.2));
    const micError = (data.mic_error || '').trim();
    if (data.connected) {
      setDot('srvDot', 'on');
      $('srvLabel').textContent = 'live';
      if (!analyser) {
        setDot('micDot', (data.mic_active || rms > 0.003) ? 'on' : 'warn');
        $('micLabel').textContent = (data.mic_active || rms > 0.003) ? 'mic daemon' : 'mic idle';
      }
    } else {
      setDot('srvDot', 'error');
      $('srvLabel').textContent = 'offline';
      if (!analyser) {
        if (micError) {
          setDot('micDot', 'error');
          $('micLabel').textContent = 'mic error';
          if (micError !== lastMicErrorMsg) {
            setTicker('mic issue: ' + micError);
            lastMicErrorMsg = micError;
          }
        } else {
          setDot('micDot', 'warn');
          $('micLabel').textContent = 'mic offline';
          lastMicErrorMsg = '';
        }
      }
    }
    if ((data.commands_detected || 0) > totalCommands) {
      totalCommands = data.commands_detected || 0;
      lastCommandAt = Date.now();
      updateStats();
    }
  } catch (_) {
    setDot('srvDot', 'error');
    $('srvLabel').textContent = 'reconnecting';
  }
}

async function sendQuickInput() {
  const input = $('quickInput');
  const text = (input.value || '').trim();
  if (!text) return;
  input.value = '';
  try {
    const resp = await fetch('/inject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!resp.ok) throw new Error('inject failed');
  } catch (_) {
    handleEvent({
      type: 'manual',
      text,
      t: new Date().toISOString(),
      words: text.split(/\s+/).length,
    });
  }
}

function resizeCanvas() {
  const r = canvas.parentElement.getBoundingClientRect();
  canvas.width = r.width * devicePixelRatio;
  canvas.height = r.height * devicePixelRatio;
  ctx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);

  const pr = pitchCanvas.parentElement.getBoundingClientRect();
  pitchCanvas.width = pr.width * devicePixelRatio;
  pitchCanvas.height = pr.height * devicePixelRatio;
  pitchCtx.setTransform(devicePixelRatio, 0, 0, devicePixelRatio, 0, 0);
}
resizeCanvas();
addEventListener('resize', resizeCanvas);

function drawWave() {
  const w = canvas.width / devicePixelRatio;
  const h = canvas.height / devicePixelRatio;
  
  // Hard clear to prevent blown-out accumulation from the resting lines
  ctx.clearRect(0, 0, w, h);
  
  const nowMs = Date.now();
  const drawHotLine = (hot) => {
    const glow = Math.max(0, Math.min(1, hot));
    ctx.beginPath();
    ctx.strokeStyle = `rgba(74,158,255,${0.08 + (0.28 * glow)})`;
    ctx.lineWidth = 5 + (18 * glow);
    ctx.moveTo(0, h / 2);
    ctx.lineTo(w, h / 2);
    ctx.stroke();

    ctx.beginPath();
    ctx.strokeStyle = `rgba(74,158,255,${0.35 + (0.52 * glow)})`;
    ctx.lineWidth = 1.5 + (2.8 * glow);
    ctx.moveTo(0, h / 2);
    ctx.lineTo(w, h / 2);
    ctx.stroke();

    const shimmerX = ((nowMs * (0.11 + (0.04 * glow))) % (w + 160)) - 160;
    const shimmer = ctx.createLinearGradient(shimmerX, 0, shimmerX + 130, 0);
    shimmer.addColorStop(0.0, 'rgba(74,158,255,0)');
    shimmer.addColorStop(0.45, `rgba(154,208,255,${0.16 + (0.28 * glow)})`);
    shimmer.addColorStop(0.72, `rgba(214,238,255,${0.1 + (0.2 * glow)})`);
    shimmer.addColorStop(1.0, 'rgba(74,158,255,0)');
    ctx.beginPath();
    ctx.strokeStyle = shimmer;
    ctx.lineWidth = 2 + (2 * glow);
    ctx.moveTo(0, h / 2);
    ctx.lineTo(w, h / 2);
    ctx.stroke();
  };

  if (!analyser) {
    updatePitchReadout(0);
    wasHarmonicHit = false;
    if (nowMs - lastDeltaAt < 1200) livePulse = Math.max(livePulse, 0.92);
    livePulse = Math.max(livePulse, daemonMicHot * 0.9);
    livePulse = Math.max(0, livePulse - 0.018);
    drawHotLine(livePulse);
    // Never draw synthetic waveform; keep a calm baseline until real analyser data exists.
    ctx.beginPath();
    ctx.moveTo(0, h / 2);
    ctx.lineTo(w, h / 2);
    ctx.strokeStyle = 'rgba(74,158,255,0.5)';
    ctx.lineWidth = 1;
    ctx.stroke();
    pitchTrace.push({ t: nowMs, hz: 0, harmonicHit: false, harmonicOnset: false });
    if (pitchTrace.length > 900) pitchTrace = pitchTrace.slice(-900);
    drawPitchEeg(nowMs);
    requestAnimationFrame(drawWave);
    return;
  }

  analyser.getByteTimeDomainData(dataArray);
  let energy = 0;
  for (let i = 0; i < dataArray.length; i++) {
    const v = (dataArray[i] - 128) / 128;
    energy += v * v;
  }
  energy = Math.sqrt(energy / Math.max(1, dataArray.length));
  const hotMic = Math.max(
    isSpeaking ? 0.24 : 0,
    Math.min(1, energy * 5.2),
    daemonMicHot * 0.6
  );
  drawHotLine(hotMic);

  const sw = w / dataArray.length;
  let x = 0;
  ctx.beginPath();
  // Draw the waveform with a much larger amplitude mapping
  for (let i = 0; i < dataArray.length; i++) {
    const centered = (dataArray[i] - 128) / 128;
    const y = (h / 2) + (centered * (h * 0.85)); // 85% amplitude stretch
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
    x += sw;
  }

  // 1. Faster outer ambient glow (no shadowBlur)
  ctx.strokeStyle = `rgba(74, 158, 255, ${isSpeaking ? 0.35 : 0.1})`;
  ctx.lineWidth = isSpeaking ? 16 : 6;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.stroke();

  // 2. Fast Inner core
  ctx.strokeStyle = isSpeaking ? 'rgba(255, 255, 255, 0.9)' : 'rgba(180, 220, 255, 0.6)';
  ctx.lineWidth = isSpeaking ? 3 : 1.5;
  ctx.stroke();

  let pitchHz = 0;
  if (isSpeaking && pitchArray && audioCtx && analyser.getFloatTimeDomainData) {
    analyser.getFloatTimeDomainData(pitchArray);
    pitchHz = estimatePitchHz(pitchArray, audioCtx.sampleRate);
    if (pitchHz && currentPitchHz) {
      pitchHz = (pitchHz * 0.45) + (currentPitchHz * 0.55);
    }
  }

  pitchHistory.push(pitchHz || 0);
  if (pitchHistory.length > 240) pitchHistory.shift();

  let harmonicHit = false;
  let harmonicOnset = false;
  let noteInfo = null;
  if (pitchHz) {
    noteInfo = pitchNoteInfo(pitchHz);
    harmonicHit = !!(noteInfo && noteInfo.inTune);
    harmonicOnset = harmonicHit && !wasHarmonicHit;
    wasHarmonicHit = harmonicHit;
    updatePitchReadout(pitchHz, noteInfo);
    turnPitchSamples.push(pitchHz);
    if (turnPitchSamples.length > 420) {
      turnPitchSamples = turnPitchSamples.slice(-420);
    }
  } else {
    wasHarmonicHit = false;
    if (!isSpeaking) {
      updatePitchReadout(0);
    }
  }
  pitchTrace.push({
    t: nowMs,
    hz: pitchHz || 0,
    harmonicHit,
    harmonicOnset,
  });
  if (pitchTrace.length > 10000) pitchTrace = pitchTrace.slice(-10000);

  if (pitchHistory.length > 3) {
    const step = w / Math.max(1, pitchHistory.length - 1);
    let started = false;
    ctx.beginPath();
    for (let i = 0; i < pitchHistory.length; i++) {
      const hz = pitchHistory[i];
      const px = i * step;
      if (!hz) {
        started = false;
        continue;
      }
      const py = pitchToY(hz, h);
      if (!started) {
        ctx.moveTo(px, py);
        started = true;
      } else {
        ctx.lineTo(px, py);
      }
    }
    ctx.strokeStyle = 'rgba(255, 171, 74, 0.86)';
    ctx.lineWidth = 1.25;
    ctx.stroke();
  }
  drawPitchEeg(nowMs);

  requestAnimationFrame(drawWave);
}
requestAnimationFrame(drawWave);

async function initMic() {
  try {
    if (analyser) {
      applySidetoneState();
      setTicker('mic already connected');
      return;
    }
    audioCtx = audioCtx || new AudioContext();
    if (audioCtx.state === 'suspended') {
      await audioCtx.resume();
    }
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const src = audioCtx.createMediaStreamSource(stream);
    micSource = src;
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 2048;
    analyser.smoothingTimeConstant = 0.85;
    src.connect(analyser);
    monitorGain = audioCtx.createGain();
    src.connect(monitorGain);
    monitorGain.connect(audioCtx.destination);
    applySidetoneState();
    dataArray = new Uint8Array(analyser.fftSize);
    pitchArray = new Float32Array(analyser.fftSize);
    setDot('micDot', 'on');
    $('micLabel').textContent = 'mic on';
    setTicker('mic connected + sidetone ' + (sidetoneEnabled ? 'on' : 'off'));
  } catch (e) {
    $('micLabel').textContent = 'mic denied';
    setDot('micDot', 'warn');
    setTicker('mic denied');
  }
}

function connect() {
  const es = new EventSource('/stream');
  es.onopen = () => {
    setDot('srvDot', 'on');
    $('srvLabel').textContent = 'live';
    setTicker('stream connected');
  };
  es.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch (_) {}
  };
  es.onerror = () => {
    setDot('srvDot', 'error');
    $('srvLabel').textContent = 'reconnecting';
    es.close();
    setTimeout(connect, 2000);
  };
}

$('quickSend').addEventListener('click', sendQuickInput);
$('quickInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    sendQuickInput();
  }
});
$('micRetry').addEventListener('click', () => {
  setTicker('retrying mic...');
  initMic();
});
$('sidetoneToggle').addEventListener('click', async () => {
  sidetoneEnabled = !sidetoneEnabled;
  try {
    if (audioCtx && audioCtx.state === 'suspended') {
      await audioCtx.resume();
    }
  } catch (_) {
    // no-op
  }
  applySidetoneState();
  setTicker('sidetone ' + (sidetoneEnabled ? 'on' : 'off'));
});
$('viewParagraph').addEventListener('click', () => setViewMode('paragraph'));
$('viewCards').addEventListener('click', () => setViewMode('cards'));

setInterval(updateHeaderClock, 1000);
setInterval(updateAgentLiveness, 1000);
setInterval(pollStatus, 2000);
setInterval(loadTasks, 15000);
updateHeaderClock();
updateStats();
setViewMode('paragraph');
applySidetoneState();
loadTasks();

initMic().catch(() => {
  const activator = () => {
    initMic();
    document.removeEventListener('click', activator);
  };
  document.addEventListener('click', activator);
});

document.addEventListener('keydown', function once() {
  if (!audioCtx) initMic();
  document.removeEventListener('keydown', once);
}, { once: true });

connect();
pollStatus();
</script>
</body>
</html>"""


def _normalize_task_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", text.lower())).strip()


def _load_tasks(limit: int = 300):
    if not TASKFILE.exists():
        return []
    tasks = []
    try:
        with open(TASKFILE, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                    if isinstance(item, dict) and item.get("text"):
                        tasks.append(item)
                except Exception:
                    continue
    except Exception:
        return []
    return tasks[-limit:]


def _append_task(text: str, source: str = "stream"):
    clean = (text or "").strip()
    if not clean:
        return None, True

    key = _normalize_task_key(clean)
    recent = _load_tasks(limit=150)
    for item in recent:
        if item.get("key") == key:
            return item, True

    task = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "text": clean,
        "key": key,
        "source": source,
        "status": "pending",
        "t": datetime.now().isoformat(),
    }

    TASKFILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TASKFILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(task, ensure_ascii=False) + "\n")

    return task, False


def _load_dispatch_keys(limit: int = 1200):
    keys = set()
    if not DISPATCHFILE.exists():
        return keys
    rows = []
    try:
        with open(DISPATCHFILE, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except Exception:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:
        return keys
    for row in rows[-limit:]:
        key = str(row.get("task_key", "")).strip()
        if key:
            keys.add(key)
    return keys


def _append_dispatch_record(record: dict):
    DISPATCHFILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DISPATCHFILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _dispatch_task_to_deepdolphin(task: dict, source: str = "voice-viewer"):
    text = str(task.get("text", "")).strip()
    key = str(task.get("key") or _normalize_task_key(text))
    if not text or not key:
        return {"ok": False, "error": "empty task"}

    if key in _load_dispatch_keys(limit=1800):
        return {"ok": True, "duplicate": True, "reason": "already dispatched"}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    mission_id = f"dd_voice_{ts}"
    safe_suffix = key.replace(" ", "_")[:28] or "task"
    signal_path = DEEPDOLPHIN_SIGNAL_DIR / f"{mission_id}_{safe_suffix}.json"

    signal = {
        "action": "task",
        "payload": {
            "mission_id": mission_id,
            "mission": f"Voice task from Patrick stream: {text}",
            "target": "local",
            "machines": [],
            "timeout_minutes": 6,
            "dispatched_by": source,
            "dispatched_at": datetime.now().isoformat(),
            "notes": "auto-dispatched from Realtime task extraction",
            "voice_task": {
                "task_id": task.get("id"),
                "task_key": key,
                "task_text": text,
                "task_source": task.get("source", "stream"),
            },
            "swarm_plan": {
                "tasks": [
                    {
                        "agent": "kindapat",
                        "type": "reasoning",
                        "task": (
                            "Convert this voice request into a concise, actionable task card with "
                            f"clear acceptance criteria: {text}"
                        ),
                        "rationale": "voice-intent triage",
                    }
                ]
            },
        },
        "timestamp": int(time.time() * 1000),
    }

    DEEPDOLPHIN_SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    signal_path.write_text(json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8")

    record = {
        "task_id": task.get("id"),
        "task_key": key,
        "task_text": text,
        "mission_id": mission_id,
        "source": source,
        "signal_path": str(signal_path),
        "t": datetime.now().isoformat(),
    }
    _append_dispatch_record(record)
    return {"ok": True, "duplicate": False, **record}


def _openai_tts(text: str):
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None, None
    model = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
    voice = os.environ.get("OPENAI_TTS_VOICE", "alloy")
    payload = {"model": model, "voice": voice, "input": text, "format": "mp3"}
    req = Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(req, timeout=15) as resp:
        return resp.read(), "audio/mpeg"


def _orpheus_tts(text: str):
    endpoint = os.environ.get("ORPHEUS_TTS_URL", "").strip()
    if not endpoint:
        return None, None

    payload = {
        "text": text,
        "voice": os.environ.get("ORPHEUS_TTS_VOICE", "orpheus"),
        "format": "mp3",
    }
    req = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        raw = resp.read()

    if ctype.startswith("audio/"):
        return raw, ctype

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None

    b64 = data.get("audio_base64") or data.get("b64_audio") or data.get("audio")
    if isinstance(b64, str) and b64.strip():
        try:
            return base64.b64decode(b64), data.get("mime", "audio/mpeg")
        except Exception:
            return None, None

    media_url = data.get("audio_url") or data.get("url")
    if isinstance(media_url, str) and media_url.strip():
        with urlopen(Request(media_url), timeout=10) as resp:
            ctype = (resp.headers.get("Content-Type") or "audio/mpeg").split(";")[0].strip()
            return resp.read(), ctype

    return None, None


def _squeak_wav():
    sample_rate = 22050
    duration_s = 0.24
    n = int(sample_rate * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n):
            t = i / sample_rate
            freq = 740.0 + (360.0 * (t / duration_s))
            env = max(0.0, 1.0 - (t / duration_s))
            amp = 0.28 * env
            sample = int(32767 * amp * math.sin(2 * math.pi * freq * t))
            frames += struct.pack("<h", sample)
        wf.writeframes(bytes(frames))
    return buf.getvalue(), "audio/wav"


def _ack_audio(text: str):
    clean = (text or "").strip() or "Task queued."
    try:
        audio, ctype = _orpheus_tts(clean)
        if audio:
            return audio, ctype or "audio/mpeg"
    except Exception:
        pass

    try:
        audio, ctype = _openai_tts(clean)
        if audio:
            return audio, ctype or "audio/mpeg"
    except Exception:
        pass

    return _squeak_wav()


def _load_steer_state():
    if not STEER_STATEFILE.exists():
        return {"dispatch_times": [], "recent": {}}
    try:
        data = json.loads(STEER_STATEFILE.read_text(encoding="utf-8"))
    except Exception:
        return {"dispatch_times": [], "recent": {}}
    if not isinstance(data, dict):
        return {"dispatch_times": [], "recent": {}}
    times = data.get("dispatch_times", [])
    recent = data.get("recent", {})
    if not isinstance(times, list):
        times = []
    if not isinstance(recent, dict):
        recent = {}
    return {"dispatch_times": times, "recent": recent}


def _save_steer_state(state: dict):
    STEER_STATEFILE.parent.mkdir(parents=True, exist_ok=True)
    STEER_STATEFILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _score_steer(text: str, source: str = "delta"):
    raw = (text or "").strip()
    low = raw.lower()
    score = 0
    strong = re.search(
        r"\b(go dolphin|route this|spin up|switch to|use orpheus|"
        r"call [a-z0-9_-]+|start [a-z0-9_-]+|stop [a-z0-9_-]+)\b",
        low,
    )
    medium = re.search(
        r"\b(please|can you|need to|do this now|execute|dispatch|queue|steer)\b",
        low,
    )
    soft = re.search(
        r"\b(maybe|would be nice|i think|could be|someday|if we can|interested in)\b",
        low,
    )

    if strong:
        score += 4
    if medium:
        score += 2
    if source in {"completed", "manual", "legacy"}:
        score += 1
    if re.search(r"[.!?]\s*$", raw):
        score += 1
    if len(raw.split()) <= 18:
        score += 1
    if soft:
        score -= 2
    return score


def _should_route_steer(text: str, source: str = "delta"):
    now_ts = time.time()
    key = _normalize_task_key(text)
    state = _load_steer_state()
    dispatch_times = [float(t) for t in state.get("dispatch_times", []) if now_ts - float(t) <= STEER_WINDOW_SEC]
    recent = {
        str(k): float(v)
        for k, v in state.get("recent", {}).items()
        if now_ts - float(v) <= max(STEER_WINDOW_SEC * 2, 900)
    }

    score = _score_steer(text, source)
    threshold = 5 if source == "delta" else 4

    if not key:
        return False, "empty", score, threshold

    last_for_key = recent.get(key)
    if last_for_key and now_ts - last_for_key < 180:
        state["dispatch_times"] = dispatch_times
        state["recent"] = recent
        _save_steer_state(state)
        return False, "duplicate_recent", score, threshold

    if score < threshold:
        state["dispatch_times"] = dispatch_times
        state["recent"] = recent
        _save_steer_state(state)
        return False, "low_confidence", score, threshold

    if dispatch_times and now_ts - dispatch_times[-1] < STEER_COOLDOWN_SEC:
        state["dispatch_times"] = dispatch_times
        state["recent"] = recent
        _save_steer_state(state)
        return False, "cooldown", score, threshold

    if len(dispatch_times) >= STEER_MAX_PER_WINDOW:
        state["dispatch_times"] = dispatch_times
        state["recent"] = recent
        _save_steer_state(state)
        return False, "budget", score, threshold

    dispatch_times.append(now_ts)
    recent[key] = now_ts
    if len(recent) > 500:
        newest = sorted(recent.items(), key=lambda kv: kv[1], reverse=True)[:500]
        recent = {k: v for k, v in newest}
    state["dispatch_times"] = dispatch_times
    state["recent"] = recent
    _save_steer_state(state)
    return True, "accepted", score, threshold


def _append_steer(
    text: str,
    source: str = "delta",
    item: str = "",
    status: str = "captured",
    reason: str = "",
    score: int = 0,
    threshold: int = 0,
):
    clean = (text or "").strip()
    if not clean:
        return None
    event = {
        "id": datetime.now().strftime("%Y%m%d%H%M%S%f"),
        "text": clean,
        "source": source,
        "item": item,
        "status": status,
        "reason": reason,
        "score": score,
        "threshold": threshold,
        "t": datetime.now().isoformat(),
    }
    STEERFILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STEERFILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path not in ("/inject", "/task", "/ack", "/steer"):
            self.send_error(404)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0

        raw = b""
        if length > 0:
            raw = self.rfile.read(length)

        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}

        if self.path == "/ack":
            phrase = str(payload.get("text", "")).strip() or "Task queued."
            try:
                audio, ctype = _ack_audio(phrase)
            except Exception as exc:
                body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype or "audio/wav")
            self.send_header("Content-Length", str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)
            return

        text = str(payload.get("text", "")).strip()
        if not text:
            body = json.dumps({"ok": False, "error": "text is required"}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/task":
            try:
                task, duplicate = _append_task(text, str(payload.get("source", "stream")))
            except Exception as exc:
                body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            dispatch = None
            should_dispatch = False
            if task:
                task_key = str(task.get("key", "")).strip()
                if task_key and task_key not in _load_dispatch_keys(limit=1800):
                    should_dispatch = True
                elif not duplicate:
                    should_dispatch = True

            if task and should_dispatch:
                try:
                    dispatch = _dispatch_task_to_deepdolphin(task, source="somertime-voice")
                except Exception as exc:
                    dispatch = {"ok": False, "error": str(exc)}

            body = json.dumps({
                "ok": True,
                "duplicate": duplicate,
                "task": task,
                "dispatch": dispatch,
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/steer":
            source = str(payload.get("source", "delta"))
            item = str(payload.get("item", ""))
            route_ok, route_reason, route_score, route_threshold = _should_route_steer(text, source)
            try:
                steer = _append_steer(
                    text,
                    source,
                    item,
                    status="routed" if route_ok else "suppressed",
                    reason=route_reason,
                    score=route_score,
                    threshold=route_threshold,
                )
                task = None
                duplicate = False
                if route_ok or source in {"completed", "manual", "legacy"}:
                    task, duplicate = _append_task(text, "steer")
            except Exception as exc:
                body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            dispatch = None
            should_dispatch = False
            if task and route_ok:
                task_key = str(task.get("key", "")).strip()
                if task_key and task_key not in _load_dispatch_keys(limit=1800):
                    should_dispatch = True
                elif not duplicate:
                    should_dispatch = True

            if task and should_dispatch:
                try:
                    dispatch = _dispatch_task_to_deepdolphin(task, source="somertime-voice-steer")
                except Exception as exc:
                    dispatch = {"ok": False, "error": str(exc)}

            body = json.dumps({
                "ok": True,
                "steer": steer,
                "task": task,
                "duplicate": duplicate,
                "dispatch": dispatch,
                "route": {
                    "approved": route_ok,
                    "reason": route_reason,
                    "score": route_score,
                    "threshold": route_threshold,
                },
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        event = {
            "type": "manual",
            "text": text,
            "t": datetime.now().isoformat(),
            "dur": 0,
            "words": len(text.split()),
            "source": "viewer",
        }
        try:
            with open(STREAMFILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as exc:
            body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            line_count = 0
            if STREAMFILE.exists():
                try:
                    with open(STREAMFILE, "r", encoding="utf-8") as f:
                        for raw in f:
                            raw = raw.strip()
                            if raw:
                                self.wfile.write(f"data: {raw}\n\n".encode("utf-8"))
                                line_count += 1
                    self.wfile.flush()
                except Exception:
                    pass

            try:
                while True:
                    if STREAMFILE.exists():
                        try:
                            with open(STREAMFILE, "r", encoding="utf-8") as f:
                                lines = f.readlines()
                            if len(lines) > line_count:
                                for ln in lines[line_count:]:
                                    ln = ln.strip()
                                    if ln:
                                        self.wfile.write(f"data: {ln}\n\n".encode("utf-8"))
                                self.wfile.flush()
                                line_count = len(lines)
                        except Exception:
                            pass
                    try:
                        self.wfile.write(b": hb\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                        return
                    time.sleep(0.3)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                return

        elif self.path == "/tasks":
            tasks = _load_tasks(limit=400)
            body = json.dumps({"ok": True, "tasks": tasks}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/status":
            data = {}
            if STATUSFILE.exists():
                try:
                    data = json.loads(STATUSFILE.read_text(encoding="utf-8"))
                except Exception:
                    pass
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


def main():
    parser = argparse.ArgumentParser(description="Realtime Viewer")
    parser.add_argument("--port", type=int, default=3088)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"Voice viewer at {url}", flush=True)

    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        server.server_close()


if __name__ == "__main__":
    main()
