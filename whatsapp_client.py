"""Thin WhatsApp Cloud API client.

Only the methods the lean flow needs:
  - send_text
  - send_interactive_buttons  (up to 3 reply buttons)
  - send_image                (by URL or media_id)
  - download_media            (for voice notes)
  - mark_read                 (optional blue ticks)

Every method swallows transport errors into a dict result and logs — callers
should never crash on a failed WhatsApp send.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from config import ACCESS_TOKEN, PHONE_NUMBER_ID, WA_API_BASE

log = logging.getLogger(__name__)

_HEADERS_JSON = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Content-Type": "application/json",
}

_TIMEOUT = 15


def _url(path: str) -> str:
    return f"{WA_API_BASE}/{PHONE_NUMBER_ID}/{path}"


def _post(path: str, payload: dict) -> dict:
    try:
        r = requests.post(_url(path), headers=_HEADERS_JSON, json=payload, timeout=_TIMEOUT)
        if r.status_code >= 400:
            log.warning("WA API %s failed: %s %s", path, r.status_code, r.text)
            return {"ok": False, "status": r.status_code, "error": r.text}
        return {"ok": True, "data": r.json()}
    except requests.RequestException as e:
        log.exception("WA API %s exception: %s", path, e)
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------

def send_text(to: str, body: str, preview_url: bool = False) -> dict:
    return _post("messages", {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": body, "preview_url": preview_url},
    })


def send_interactive_buttons(to: str, body: str, buttons: list[dict],
                             header: str | None = None, footer: str | None = None) -> dict:
    """`buttons` is a list of `{"id": str, "title": str}` — max 3."""
    button_entries = [
        {"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}}
        for b in buttons[:3]
    ]
    interactive: dict[str, Any] = {
        "type": "button",
        "body": {"text": body},
        "action": {"buttons": button_entries},
    }
    if header:
        interactive["header"] = {"type": "text", "text": header[:60]}
    if footer:
        interactive["footer"] = {"text": footer[:60]}
    return _post("messages", {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    })


def send_image(to: str, image_url: str | None = None, media_id: str | None = None,
               caption: str | None = None) -> dict:
    if not image_url and not media_id:
        return {"ok": False, "error": "image_url or media_id required"}
    image: dict[str, Any] = {}
    if image_url:
        image["link"] = image_url
    else:
        image["id"] = media_id
    if caption:
        image["caption"] = caption
    return _post("messages", {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": image,
    })


def mark_read(message_id: str) -> dict:
    return _post("messages", {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    })


def download_media(media_id: str) -> bytes | None:
    """Resolve a WA media id to a URL then fetch the binary."""
    try:
        meta = requests.get(
            f"{WA_API_BASE}/{media_id}",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            timeout=_TIMEOUT,
        )
        meta.raise_for_status()
        url = meta.json().get("url")
        if not url:
            return None
        blob = requests.get(
            url,
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            timeout=_TIMEOUT,
        )
        blob.raise_for_status()
        return blob.content
    except requests.RequestException as e:
        log.exception("download_media(%s) failed: %s", media_id, e)
        return None
