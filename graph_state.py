"""Shared LangGraph state for the Ku conversational flow."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

Stage = Literal[
    "NEW",
    "GREETED",
    "ITEMS_CAPTURED",
    "DURATION_SET",
    "CART_SHOWN",
    "CHECKED_OUT",
]


class KuState(TypedDict, total=False):
    # Identity
    phone: str
    push_name: str
    session_id: str

    # Conversation position
    stage: Stage
    stage_before: Stage

    # Inbound turn
    inbound: dict[str, Any]          # {type, text, interactive_id, media_id, audio_bytes}
    wa_message_id: str | None

    # Extracted context
    transcript: str | None
    items: list[dict[str, Any]]      # [{product_id, qty, name}]
    duration: int | None             # numeric duration value
    duration_unit: str | None        # "months" or "days"

    # Cart
    cart: dict[str, Any] | None
    last_cart_link: str | None

    # Output accumulator (sent by webhook after graph.invoke)
    reply: list[dict[str, Any]]      # list of send ops: {kind: "text"|"buttons"|"image", ...}

    # Fallback / errors
    fallback_triggered: bool
    fallback_reason: str | None
    kb_hit: str | None

    # Rating
    rating: str | None               # "yes", "meh", "no" post-checkout feedback

    # Routing
    branch: str | None               # set by classify_inbound
