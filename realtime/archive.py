"""
Voice archive -- saves audio chunks and transcripts to disk and Milvus.
Raw audio goes to dated directories as FLAC files.
Transcripts go to Milvus voice_archive collection with embeddings for semantic search.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

import numpy as np
import soundfile as sf

from realtime.config import (
    ARCHIVE_DIR, SAMPLE_RATE, MILVUS_URI,
    VOICE_COLLECTION, EMBEDDING_DIM, EMBEDDING_MODEL,
)

logger = logging.getLogger("voice.archive")

try:
    from pymilvus import (
        connections, Collection, CollectionSchema, FieldSchema,
        DataType, utility,
    )
    PYMILVUS_OK = True
except ImportError:
    PYMILVUS_OK = False

try:
    from sentence_transformers import SentenceTransformer
    SBERT_OK = True
except ImportError:
    SBERT_OK = False


class VoiceArchive:
    """Persists audio segments and their transcripts."""

    def __init__(self):
        self._milvus_connected = False
        self._collection = None
        self._encoder = None
        self._connect_milvus()
        self._load_encoder()

    def _connect_milvus(self):
        if not PYMILVUS_OK:
            logger.warning("pymilvus not installed, archiving to disk only")
            return
        try:
            connections.connect("voice_archive", uri=MILVUS_URI, timeout=10)
            self._milvus_connected = True
            self._ensure_collection()
            logger.info("Voice archive connected to Milvus")
        except Exception as e:
            logger.warning("Milvus unreachable: %s -- disk-only mode", e)

    def _ensure_collection(self):
        if not self._milvus_connected:
            return
        try:
            if utility.has_collection(VOICE_COLLECTION, using="voice_archive"):
                self._collection = Collection(VOICE_COLLECTION, using="voice_archive")
                self._collection.load()
                return

            fields = [
                FieldSchema("id", DataType.INT64, is_primary=True, auto_id=True),
                FieldSchema("transcript", DataType.VARCHAR, max_length=65535),
                FieldSchema("timestamp", DataType.VARCHAR, max_length=64),
                FieldSchema("duration_sec", DataType.FLOAT),
                FieldSchema("audio_path", DataType.VARCHAR, max_length=1024),
                FieldSchema("classification", DataType.VARCHAR, max_length=32),
                FieldSchema("machine", DataType.VARCHAR, max_length=64),
                FieldSchema("word_count", DataType.INT64),
                FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM),
            ]
            schema = CollectionSchema(fields, description="Somertime voice archive")
            self._collection = Collection(VOICE_COLLECTION, schema, using="voice_archive")
            self._collection.create_index("embedding", {
                "metric_type": "L2",
                "index_type": "IVF_FLAT",
                "params": {"nlist": 128},
            })
            self._collection.load()
            logger.info("Created voice_archive collection in Milvus")
        except Exception as e:
            logger.error("Failed to ensure voice collection: %s", e)
            self._milvus_connected = False

    def _load_encoder(self):
        if not SBERT_OK:
            logger.warning("sentence_transformers not installed, no embeddings")
            return
        try:
            self._encoder = SentenceTransformer(EMBEDDING_MODEL)
        except Exception as e:
            logger.warning("Failed to load encoder: %s", e)

    def _embed(self, text: str):
        if self._encoder is None:
            return None
        try:
            return self._encoder.encode(text).tolist()
        except Exception:
            return None

    def save(
        self,
        audio: np.ndarray,
        transcript: str,
        classification: str = "ambient",
        timestamp: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Save audio + transcript. Returns metadata dict with paths and IDs."""
        ts = timestamp or datetime.now()
        date_dir = ARCHIVE_DIR / ts.strftime("%Y") / ts.strftime("%m") / ts.strftime("%d")
        date_dir.mkdir(parents=True, exist_ok=True)

        stem = ts.strftime("%Y-%m-%d_%H-%M-%S")
        flac_path = date_dir / f"{stem}.flac"
        meta_path = date_dir / f"{stem}.json"

        duration_sec = len(audio) / SAMPLE_RATE

        sf.write(str(flac_path), audio, SAMPLE_RATE, format="FLAC")

        meta = {
            "timestamp": ts.isoformat(),
            "duration_sec": round(duration_sec, 2),
            "transcript": transcript,
            "classification": classification,
            "audio_file": str(flac_path),
            "machine": "neon",
            "word_count": len(transcript.split()) if transcript else 0,
            "sample_rate": SAMPLE_RATE,
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        milvus_id = None
        if transcript.strip() and self._milvus_connected and self._collection:
            embedding = self._embed(transcript)
            if embedding:
                try:
                    result = self._collection.insert([
                        [transcript],
                        [ts.isoformat()],
                        [float(duration_sec)],
                        [str(flac_path)],
                        [classification],
                        ["neon"],
                        [len(transcript.split())],
                        [embedding],
                    ])
                    self._collection.flush()
                    milvus_id = result.primary_keys[0]
                except Exception as e:
                    logger.error("Milvus insert failed: %s", e)

        meta["milvus_id"] = milvus_id
        logger.info(
            "Archived %s (%.1fs, %d words, %s) -> %s",
            classification, duration_sec,
            meta["word_count"], stem, flac_path.name,
        )
        return meta

    def search(self, query: str, limit: int = 10):
        """Semantic search over voice transcripts."""
        if not self._milvus_connected or not self._collection:
            logger.warning("Milvus not connected, cannot search")
            return []

        embedding = self._embed(query)
        if not embedding:
            return []

        try:
            results = self._collection.search(
                data=[embedding],
                anns_field="embedding",
                param={"metric_type": "L2", "params": {"nprobe": 16}},
                limit=limit,
                output_fields=[
                    "transcript", "timestamp", "duration_sec",
                    "audio_path", "classification", "word_count",
                ],
            )
            output = []
            for hits in results:
                for hit in hits:
                    output.append({
                        "id": hit.id,
                        "score": hit.distance,
                        "transcript": hit.entity.get("transcript", ""),
                        "timestamp": hit.entity.get("timestamp", ""),
                        "duration_sec": hit.entity.get("duration_sec", 0),
                        "audio_path": hit.entity.get("audio_path", ""),
                        "classification": hit.entity.get("classification", ""),
                        "word_count": hit.entity.get("word_count", 0),
                    })
            return output
        except Exception as e:
            logger.error("Voice search failed: %s", e)
            return []

    def close(self):
        if self._milvus_connected:
            try:
                connections.disconnect("voice_archive")
            except Exception:
                pass
