"""Simplified MemCell clustering (time-based until embedding available)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from loguru import logger


def cluster_id_from_timestamp(ts: str) -> str:
    """Derive cluster ID from MemCell timestamp (YYYY-MM-DD)."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return datetime.now().strftime("%Y-%m-%d")


def load_cluster_state(path: Path) -> dict:
    """Load cluster state from JSON file."""
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            state = json.loads(text)
            logger.debug("EnhancedMem [file READ] cluster_state.json: {} eventids", len(state.get("eventid_to_cluster", {})))
            return state
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "eventid_to_cluster": {},
        "cluster_counts": {},
        "cluster_last_ts": {},
    }


def save_cluster_state(path: Path, state: dict) -> None:
    """Save cluster state to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    logger.debug("EnhancedMem [file WRITE] cluster_state.json: {} eventids", len(state.get("eventid_to_cluster", {})))


def assign_memcell_to_cluster(
    event_id: str,
    timestamp: str,
    state_path: Path,
) -> str:
    """Assign MemCell to cluster (time-based). Returns cluster_id."""
    state = load_cluster_state(state_path)
    eventid_to_cluster = state.setdefault("eventid_to_cluster", {})
    cluster_counts = state.setdefault("cluster_counts", {})
    cluster_last_ts = state.setdefault("cluster_last_ts", {})

    cluster_id = cluster_id_from_timestamp(timestamp)
    eventid_to_cluster[event_id] = cluster_id
    cluster_counts[cluster_id] = cluster_counts.get(cluster_id, 0) + 1
    cluster_last_ts[cluster_id] = timestamp

    state["eventid_to_cluster"] = eventid_to_cluster
    state["cluster_counts"] = cluster_counts
    state["cluster_last_ts"] = cluster_last_ts
    save_cluster_state(state_path, state)
    return cluster_id


def get_cluster_event_ids(state_path: Path, cluster_id: str) -> list[str]:
    """Get event IDs belonging to a cluster."""
    state = load_cluster_state(state_path)
    eventid_to_cluster = state.get("eventid_to_cluster", {})
    return [eid for eid, cid in eventid_to_cluster.items() if cid == cluster_id]
