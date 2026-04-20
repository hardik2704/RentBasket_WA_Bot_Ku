"""Firestore persistence layer (append-mostly, production-grade).

Schema
------
users/{phone}
    profile:   { phone, first_seen_at, last_seen_at, name, push_name,
                 current_session_id, highlights: {...} }
users/{phone}/messages/{auto_id}
    append-only per-message log (direction, ts, type, text, media, ids, stage)
users/{phone}/sessions/{session_id}
    rolling session summary doc
users/{phone}/fallback_events/{auto_id}
    append-only RL review log for 5% discount fallbacks

All profile updates use merge=True / Increment / ArrayUnion so we never blindly
overwrite a document.

Every function swallows exceptions into a log line — persistence failures must
NEVER block a WhatsApp reply.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from config import FIREBASE_CREDENTIALS_PATH, SESSION_GAP_HOURS

log = logging.getLogger(__name__)

_client = None
_available = False


def _init_once():
    """Lazy init so missing creds don't crash import."""
    global _client, _available
    if _client is not None or _available:
        return
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            cred_path = FIREBASE_CREDENTIALS_PATH
            print(f"[firestore] Initializing — checking credentials at: {cred_path}", flush=True)
            if os.path.exists(cred_path):
                print(f"[firestore] Credentials file found, loading certificate", flush=True)
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
                print(f"[firestore] firebase_admin initialized from certificate", flush=True)
            else:
                print(
                    f"[firestore] Credentials file NOT found at {cred_path}, "
                    f"trying Application Default Credentials (ADC)",
                    flush=True,
                )
                firebase_admin.initialize_app()
                print(f"[firestore] firebase_admin initialized via ADC", flush=True)
        _client = firestore.client()
        _available = True
        print(f"[firestore] CONNECTED SUCCESSFULLY — client ready", flush=True)
    except Exception as e:
        print(f"[firestore] INIT FAILED (continuing without persistence): {type(e).__name__}: {e}", flush=True)
        _client = None
        _available = False


def is_available() -> bool:
    """Public health-check accessor. Runs init if not already done."""
    _init_once()
    return _available


def warm_init() -> None:
    """Call from webhook server at startup so logs appear on boot instead of first request."""
    _init_once()


def _fs():
    _init_once()
    return _client


def _server_ts():
    from firebase_admin import firestore
    return firestore.SERVER_TIMESTAMP


def _increment(n: int = 1):
    from firebase_admin import firestore
    return firestore.Increment(n)


def _array_union(values: list):
    from firebase_admin import firestore
    return firestore.ArrayUnion(values)


# ---------- profile ----------------------------------------------------------

def get_profile(phone: str) -> dict:
    db = _fs()
    if not db:
        return {}
    try:
        doc = db.collection("users").document(phone).get()
        return doc.to_dict() or {}
    except Exception as e:
        log.warning("get_profile(%s) failed: %s", phone, e)
        return {}


def upsert_profile(phone: str, fields: dict[str, Any]) -> None:
    db = _fs()
    if not db:
        return
    try:
        payload = {**fields, "phone": phone, "last_seen_at": _server_ts()}
        payload.setdefault("first_seen_at", _server_ts())
        db.collection("users").document(phone).set(payload, merge=True)
    except Exception as e:
        log.warning("upsert_profile(%s) failed: %s", phone, e)


def update_highlights(phone: str, highlights: dict[str, Any]) -> None:
    """Merge highlights into profile.highlights.*"""
    db = _fs()
    if not db:
        return
    try:
        payload = {f"highlights.{k}": v for k, v in highlights.items()}
        payload["last_seen_at"] = _server_ts()
        db.collection("users").document(phone).set(
            {"highlights": highlights, "last_seen_at": _server_ts(), "phone": phone},
            merge=True,
        )
    except Exception as e:
        log.warning("update_highlights(%s) failed: %s", phone, e)


def increment_highlight(phone: str, field: str, by: int = 1) -> None:
    db = _fs()
    if not db:
        return
    try:
        db.collection("users").document(phone).set(
            {"highlights": {field: _increment(by)}, "phone": phone},
            merge=True,
        )
    except Exception as e:
        log.warning("increment_highlight(%s, %s) failed: %s", phone, field, e)


def append_note(phone: str, note: str) -> None:
    db = _fs()
    if not db:
        return
    try:
        db.collection("users").document(phone).set(
            {"highlights": {"notes": _array_union([note])}, "phone": phone},
            merge=True,
        )
    except Exception as e:
        log.warning("append_note(%s) failed: %s", phone, e)


# ---------- sessions ---------------------------------------------------------

def get_or_open_session(phone: str, push_name: str | None = None) -> str:
    """Return the current session_id. Start a new one if >SESSION_GAP_HOURS since
    the last activity or none exists. Stores `current_session_id` on profile.
    """
    db = _fs()
    if not db:
        # offline fallback — ephemeral id per process
        return f"local-{uuid.uuid4().hex[:12]}"
    try:
        profile = get_profile(phone)
        current = profile.get("current_session_id")
        last_seen = profile.get("last_seen_at")
        reuse = False
        if current and last_seen:
            try:
                last_ts = last_seen.timestamp() if hasattr(last_seen, "timestamp") else float(last_seen)
                if (time.time() - last_ts) <= SESSION_GAP_HOURS * 3600:
                    reuse = True
            except Exception:
                reuse = False
        if reuse:
            return current
        session_id = uuid.uuid4().hex[:16]
        db.collection("users").document(phone).collection("sessions").document(session_id).set({
            "started_at": _server_ts(),
            "last_activity_at": _server_ts(),
            "stage": "NEW",
            "items_captured": False,
            "duration_chosen": None,
            "checkout_reached": False,
            "fallback_discount_triggered": False,
        })
        db.collection("users").document(phone).set(
            {"current_session_id": session_id, "phone": phone, "last_seen_at": _server_ts()},
            merge=True,
        )
        if push_name:
            db.collection("users").document(phone).set(
                {"push_name": push_name, "phone": phone}, merge=True,
            )
        return session_id
    except Exception as e:
        log.warning("get_or_open_session(%s) failed: %s", phone, e)
        return f"local-{uuid.uuid4().hex[:12]}"


def update_session(phone: str, session_id: str, fields: dict[str, Any]) -> None:
    db = _fs()
    if not db:
        return
    try:
        payload = {**fields, "last_activity_at": _server_ts()}
        db.collection("users").document(phone).collection("sessions").document(session_id).set(
            payload, merge=True,
        )
    except Exception as e:
        log.warning("update_session(%s/%s) failed: %s", phone, session_id, e)


# ---------- messages (append-only) ------------------------------------------

def append_message(phone: str, *, direction: str, msg_type: str,
                   text: str | None = None,
                   interactive_id: str | None = None,
                   transcript: str | None = None,
                   media_id: str | None = None,
                   caption: str | None = None,
                   wa_message_id: str | None = None,
                   session_id: str | None = None,
                   stage_before: str | None = None,
                   stage_after: str | None = None,
                   extra: dict | None = None) -> None:
    db = _fs()
    if not db:
        return
    try:
        doc = {
            "direction": direction,
            "ts": _server_ts(),
            "type": msg_type,
            "text": text,
            "interactive_id": interactive_id,
            "transcript": transcript,
            "media_id": media_id,
            "caption": caption,
            "wa_message_id": wa_message_id,
            "session_id": session_id,
            "stage_before": stage_before,
            "stage_after": stage_after,
        }
        if extra:
            doc.update(extra)
        db.collection("users").document(phone).collection("messages").add(doc)
    except Exception as e:
        log.warning("append_message(%s) failed: %s", phone, e)


def has_processed_wa_id(phone: str, wa_message_id: str) -> bool:
    """Backup dedupe when the in-memory TTL cache misses (e.g. restart)."""
    db = _fs()
    if not db or not wa_message_id:
        return False
    try:
        q = (db.collection("users").document(phone).collection("messages")
             .where("wa_message_id", "==", wa_message_id).limit(1).get())
        return len(list(q)) > 0
    except Exception as e:
        log.warning("has_processed_wa_id(%s) failed: %s", phone, e)
        return False


# ---------- fallback RL log --------------------------------------------------

def log_fallback_event(phone: str, *, session_id: str | None,
                       last_stage: str | None,
                       inbound_payload: dict | None,
                       last_cart_link: str | None,
                       reason: str) -> None:
    db = _fs()
    if not db:
        return
    try:
        db.collection("users").document(phone).collection("fallback_events").add({
            "ts": _server_ts(),
            "session_id": session_id,
            "last_stage": last_stage,
            "inbound_payload": inbound_payload,
            "last_cart_link": last_cart_link,
            "reason": reason,
        })
        if session_id:
            update_session(phone, session_id, {"fallback_discount_triggered": True})
        increment_highlight(phone, "fallback_discount_count", 1)
    except Exception as e:
        log.warning("log_fallback_event(%s) failed: %s", phone, e)
