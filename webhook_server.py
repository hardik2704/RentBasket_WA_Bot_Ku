"""Flask webhook for the lean Ku WhatsApp bot.

Routes:
  GET  /webhook   - verify subscription challenge
  POST /webhook   - handle Meta WhatsApp Cloud API payloads
  GET  /catalogue - serve the product catalogue PNG
  GET  /healthz   - liveness probe
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flask import Flask, abort, jsonify, request, send_from_directory

import firestore_store
import whatsapp_client
from config import (
    CATALOGUE_IMAGE_PATH,
    DEDUP_MAX_IDS,
    DEDUP_TTL_SECONDS,
    MAX_WORKERS,
    PUBLIC_DIR,
    VERIFY_TOKEN,
)
from graph import get_graph

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ku")

app = Flask(__name__)

# Warm the graph at import so the first request isn't slow.
print("[boot] Compiling LangGraph...", flush=True)
GRAPH = get_graph()
print("[boot] LangGraph compiled.", flush=True)

# Warm Firestore at import so connection status is visible in deploy logs
# (not deferred to first webhook, which keeps the user blind until a real message).
print("[boot] Warming Firestore connection...", flush=True)
firestore_store.warm_init()
print(
    f"[boot] Firestore available={firestore_store.is_available()}. Server ready.",
    flush=True,
)

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# In-memory dedupe: OrderedDict keyed by wa_message_id -> enqueue_ts
_dedupe_lock = threading.Lock()
_dedupe: "OrderedDict[str, float]" = OrderedDict()

# Per-phone locks so concurrent webhook deliveries from the same user serialize.
_phone_locks_lock = threading.Lock()
_phone_locks: dict[str, threading.Lock] = {}


def _phone_lock(phone: str) -> threading.Lock:
    with _phone_locks_lock:
        lock = _phone_locks.get(phone)
        if lock is None:
            lock = threading.Lock()
            _phone_locks[phone] = lock
        return lock


def _normalize_phone(phone: str) -> str:
    """Strip country code (91) to get 10-digit Indian phone number."""
    if not phone:
        return phone
    # Remove country code prefix if present
    if phone.startswith("91") and len(phone) > 10:
        return phone[2:]
    return phone


def _is_duplicate(wa_message_id: str | None) -> bool:
    if not wa_message_id:
        return False
    now = time.time()
    with _dedupe_lock:
        # Purge expired
        stale = [k for k, ts in _dedupe.items() if now - ts > DEDUP_TTL_SECONDS]
        for k in stale:
            _dedupe.pop(k, None)
        if wa_message_id in _dedupe:
            return True
        _dedupe[wa_message_id] = now
        while len(_dedupe) > DEDUP_MAX_IDS:
            _dedupe.popitem(last=False)
    return False


# ---------------------------------------------------------------------------
# Webhook routes
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    from config import FIREBASE_CREDENTIALS_PATH, FIREBASE_CREDENTIALS_SOURCE
    return jsonify({
        "status": "ok",
        "firestore_available": firestore_store.is_available(),
        "firestore_creds_source": FIREBASE_CREDENTIALS_SOURCE,
        "firestore_creds_path": FIREBASE_CREDENTIALS_PATH,
        "firestore_creds_path_exists": os.path.exists(FIREBASE_CREDENTIALS_PATH),
    }), 200


@app.get("/catalogue")
def catalogue():
    if not os.path.exists(CATALOGUE_IMAGE_PATH):
        abort(404)
    return send_from_directory(PUBLIC_DIR, "RentBasket_Catalogue.png")


@app.get("/webhook")
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "forbidden", 403


@app.post("/webhook")
def inbound():
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}
    parsed = _parse_payload(payload)
    if not parsed:
        return jsonify({"status": "ignored"}), 200
    if _is_duplicate(parsed.get("wa_message_id")):
        log.info("dedupe skip %s", parsed.get("wa_message_id"))
        return jsonify({"status": "dup"}), 200

    _executor.submit(_handle_async, parsed)
    return jsonify({"status": "processing"}), 200


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_payload(payload: dict) -> dict[str, Any] | None:
    try:
        entry = (payload.get("entry") or [{}])[0]
        change = (entry.get("changes") or [{}])[0]
        value = change.get("value") or {}
        messages = value.get("messages") or []
        if not messages:
            return None
        msg = messages[0]

        contacts = value.get("contacts") or [{}]
        push_name = contacts[0].get("profile", {}).get("name") if contacts else None

        from_phone = _normalize_phone(msg.get("from"))
        mtype = msg.get("type")
        wa_id = msg.get("id")

        inbound: dict[str, Any] = {"type": mtype}

        if mtype == "text":
            inbound["text"] = (msg.get("text") or {}).get("body") or ""
        elif mtype == "interactive":
            interactive = msg.get("interactive") or {}
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            inbound["interactive_id"] = reply.get("id")
            inbound["text"] = reply.get("title")
        elif mtype == "audio":
            inbound["media_id"] = (msg.get("audio") or {}).get("id")
        elif mtype == "image":
            inbound["media_id"] = (msg.get("image") or {}).get("id")
            inbound["caption"] = (msg.get("image") or {}).get("caption")
        else:
            inbound["text"] = ""

        return {
            "phone": from_phone,
            "push_name": push_name,
            "wa_message_id": wa_id,
            "inbound": inbound,
        }
    except Exception as e:
        log.exception("parse_payload failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Async handler: graph.invoke + flush replies
# ---------------------------------------------------------------------------

def _handle_async(parsed: dict[str, Any]) -> None:
    phone = parsed["phone"]
    wa_id = parsed.get("wa_message_id")
    inbound = parsed["inbound"]
    push_name = parsed.get("push_name")

    with _phone_lock(phone):
        # Firestore backup dedupe (in case of restart)
        if wa_id and firestore_store.has_processed_wa_id(phone, wa_id):
            log.info("firestore-dedupe skip %s for %s", wa_id, phone)
            return

        # Hydrate stage / duration / items from profile
        profile = firestore_store.get_profile(phone) or {}
        highlights = profile.get("highlights") or {}
        session_id = firestore_store.get_or_open_session(phone, push_name=push_name)

        firestore_store.upsert_profile(phone, {
            "push_name": push_name,
            "name": profile.get("name") or push_name,
        })

        # Download audio bytes if needed
        if inbound.get("type") == "audio" and inbound.get("media_id"):
            inbound["audio_bytes"] = whatsapp_client.download_media(inbound["media_id"]) or b""

        # Log inbound message
        try:
            firestore_store.append_message(
                phone,
                direction="in",
                msg_type=inbound.get("type") or "unknown",
                text=inbound.get("text"),
                interactive_id=inbound.get("interactive_id"),
                media_id=inbound.get("media_id"),
                caption=inbound.get("caption"),
                wa_message_id=wa_id,
                session_id=session_id,
                stage_before=profile.get("stage"),
            )
        except Exception as e:
            log.warning("inbound append failed: %s", e)

        state: dict[str, Any] = {
            "phone": phone,
            "push_name": push_name,
            "session_id": session_id,
            "stage": profile.get("stage") or "NEW",
            "inbound": inbound,
            "wa_message_id": wa_id,
            "items": highlights.get("last_cart_items") or [],
            "duration": highlights.get("preferred_duration_months"),
            "last_cart_link": highlights.get("last_cart_link"),
            "reply": [],
        }

        try:
            new_state = GRAPH.invoke(state)
        except Exception as e:
            log.exception("graph.invoke failed: %s", e)
            new_state = state
            new_state["reply"] = []
            new_state["fallback_triggered"] = True
            new_state["fallback_reason"] = f"graph_exception:{e.__class__.__name__}"
            # queue recovery inline
            import graph_nodes as gn
            gn._fallback_inline(new_state)
            try:
                gn.write_firestore(new_state)
            except Exception:
                pass

        # Persist final stage onto profile so the next turn hydrates correctly
        try:
            firestore_store.upsert_profile(phone, {
                "stage": new_state.get("stage"),
            })
        except Exception:
            pass

        # Mark inbound as read (best-effort)
        if wa_id:
            try:
                whatsapp_client.mark_read(wa_id)
            except Exception:
                pass

        # Flush outbound queue
        for op in new_state.get("reply") or []:
            kind = op.get("kind")
            try:
                if kind == "text":
                    whatsapp_client.send_text(phone, op["body"], preview_url=op.get("preview_url", False))
                elif kind == "buttons":
                    whatsapp_client.send_interactive_buttons(phone, op["body"], op["buttons"])
                elif kind == "image":
                    whatsapp_client.send_image(phone, image_url=op.get("image_url"), caption=op.get("caption"))
            except Exception as e:
                log.exception("send %s failed: %s", kind, e)


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
