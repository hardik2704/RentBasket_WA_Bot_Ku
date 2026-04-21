"""Delayed follow-up scheduler for the Ku bot.

Two timers per phone, both reset on every inbound message:

  * 30-minute "ghost" nudge   — user stepped away mid-conversation
  * 19-hour  re-engagement    — next-day check-in

Both messages end with Continue / RESTART buttons so the user can either
resume where they left off or start fresh.

Thread-based (in-process). Timers are lost on restart — that's acceptable:
the next real inbound message will re-arm them.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import whatsapp_client

log = logging.getLogger(__name__)

GHOST_TIMEOUT_SECONDS = 30 * 60         # 30 minutes
FOLLOWUP_DELAY_SECONDS = 19 * 60 * 60   # 19 hours

BTN_CONTINUE = {"id": "CONTINUE", "title": "Continue"}
BTN_RESTART = {"id": "RESTART", "title": "RESTART"}
FOLLOWUP_BUTTONS = [BTN_CONTINUE, BTN_RESTART]

GHOST_TEXT = (
    "Looks like you stepped away. No worries at all!\n\n"
    "Would you like to continue where you left off, or start fresh?"
)

FOLLOWUP_TEXT = (
    "Hi! Just checking in.\n\n"
    "Were you still looking for furniture or appliances on rent?\n\n"
    "If timing or budget is a concern, we're happy to work something out. "
    "Many of our customers start with just 2-3 essentials and add more later.\n\n"
    "Would you like to continue where you left off, or start fresh?"
)

_lock = threading.Lock()
_timers: dict[str, dict[str, threading.Timer]] = {}


def _send_ghost(phone: str) -> None:
    try:
        whatsapp_client.send_interactive_buttons(phone, GHOST_TEXT, FOLLOWUP_BUTTONS)
        log.info("ghost nudge sent to %s", phone)
    except Exception as e:
        log.warning("ghost nudge failed for %s: %s", phone, e)
    finally:
        _clear_timer(phone, "ghost")


def _send_followup(phone: str) -> None:
    try:
        whatsapp_client.send_interactive_buttons(phone, FOLLOWUP_TEXT, FOLLOWUP_BUTTONS)
        log.info("19h follow-up sent to %s", phone)
    except Exception as e:
        log.warning("19h follow-up failed for %s: %s", phone, e)
    finally:
        _clear_timer(phone, "followup")


def _clear_timer(phone: str, key: str) -> None:
    with _lock:
        slot = _timers.get(phone)
        if slot and key in slot:
            slot.pop(key, None)
            if not slot:
                _timers.pop(phone, None)


def cancel_timers(phone: str) -> None:
    """Cancel any pending timers for a phone (both ghost and follow-up)."""
    if not phone:
        return
    with _lock:
        slot = _timers.pop(phone, None)
    if not slot:
        return
    for t in slot.values():
        try:
            t.cancel()
        except Exception:
            pass


def reset_timers(phone: str) -> None:
    """Cancel existing timers and arm fresh 30-min + 19-hr timers."""
    if not phone:
        return
    cancel_timers(phone)

    ghost = threading.Timer(GHOST_TIMEOUT_SECONDS, _send_ghost, args=(phone,))
    ghost.daemon = True
    followup = threading.Timer(FOLLOWUP_DELAY_SECONDS, _send_followup, args=(phone,))
    followup.daemon = True

    with _lock:
        _timers[phone] = {"ghost": ghost, "followup": followup}

    ghost.start()
    followup.start()
    log.debug("timers armed for %s (30m + 19h)", phone)
