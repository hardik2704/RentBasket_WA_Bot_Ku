"""LangGraph node implementations for the Ku bot.

Each node receives the shared `KuState`, mutates/returns a state patch, and
appends outbound messages to `state["reply"]`. The webhook layer flushes those
messages to WhatsApp after `graph.invoke` returns. No WhatsApp I/O happens
inside the graph — keeps the graph unit-testable.
"""

from __future__ import annotations

import logging
import re

import kb_retriever
from typing import Any

import cart_builder
import firestore_store
import voice_parser
from config import CATALOGUE_IMAGE_URL
from graph_state import KuState

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Canonical response copy (verbatim from the source bot where applicable)
# --------------------------------------------------------------------------

HOW_RENTING_WORKS_TEXT = (
    "Let's get you settled! Here is your 4-step journey with RentBasket:\n\n"
    "1. Select Products (2 mins)\n"
    "Pick your items.\n\n"
    "2. Secure & Relax\n"
    "Pay a one-time refundable deposit. We're proud to say 95% of our "
    "customers get their full deposit back!\n\n"
    "3. Free Installation (72 hrs)\n"
    "We deliver and install everything for FREE within 72 hours. "
    "No need to hunt for help; we handle it all.\n\n"
    "4. Enjoy & Live\n"
    "Pay monthly rent and leave the maintenance to us. If you move, "
    "we'll relocate your items for zero extra cost.\n\n"
    "Ready to upgrade your space?"
)

LATEST_REVIEWS_TEXT = (
    "Check out our Customer Reviews: https://rentbasket.short.gy/reviews\n\n"
    "Latest 5 Customer Experiences with RentBasket\n\n"
    "Prateek Jain\n"
    "23 hours ago\n\n"
    "\"Got a fresh new mattress on urgent basis. Ordered at 8 PM and it was "
    "delivered by 10 PM. Very quick and authentic service!\"\n\n"
    "--------------------\n"
    "Sejal Sangole\n"
    "6 days ago\n\n"
    "\"Great experience with RentBasket. Smooth process and reliable service.\"\n\n"
    "--------------------\n"
    "Shivam Sood\n"
    "3 weeks ago\n\n"
    "\"Very prompt service and delivery along with installation. Truly hassle-free.\"\n\n"
    "--------------------\n"
    "Harmeet Kaur\n"
    "3 weeks ago\n\n"
    "\"Very good experience, fast delivery, and good quality products.\"\n\n"
    "--------------------\n"
    "Justin Shibu\n"
    "4 weeks ago\n\n"
    "\"Great and quick fridge service. Highly satisfied.\"\n\n"
    "--------------------\n"
    "Comfort On Rent, Happiness Delivered.\n"
    "RentBasket - Furnishing Homes, Effortlessly."
)

WHY_RENTBASKET_TEXT = (
    "Why RentBasket Specifically?\n\n"
    "We're not just a rental service — we're your comfort partners:\n\n"
    "4.9 Google Star Rating\n"
    "Check out real reviews from our happy customers!\n\n"
    "Hyper-Localization\n"
    "We know your city better than anyone, so we get you the best people and fastest service.\n\n"
    "95% Full Security Refund\n"
    "We're blessed with customers who treat our products beautifully.\n\n"
    "Customer-First Approach\n"
    "Our reviews speak louder than words."
)

BTN_HOW_RENTING_WORKS = {"id": "HOW_RENTING_WORKS", "title": "How Renting Works?"}
BTN_WHY_RENTBASKET = {"id": "WHY_RENTBASKET", "title": "Why RentBasket?"}
BTN_CHECKOUT = {"id": "CHECKOUT", "title": "Checkout"}
BTN_REVIEWS = {"id": "REVIEWS", "title": "Latest Reviews"}
BTN_SHARE_LIST = {"id": "SHARE_LIST", "title": "I'll share my list"}
BTN_LIST_VOICE = {"id": "LIST_VOICE", "title": "List via Voice Note"}
BTN_VOICE_NOTE = {"id": "VOICE_NOTE", "title": "Voice Note"}
BTN_CONTINUE = {"id": "CONTINUE", "title": "Continue"}
BTN_RESTART = {"id": "RESTART", "title": "RESTART"}
BTN_DUR_3 = {"id": "DUR_3", "title": "3-Short & Sweet"}
BTN_DUR_6 = {"id": "DUR_6", "title": "6-Affordable"}
BTN_RATING_YES = {"id": "RATING_YES", "title": "Yes, great"}
BTN_RATING_MEH = {"id": "RATING_MEH", "title": "Somewhat"}
BTN_RATING_NO = {"id": "RATING_NO", "title": "Not really"}
BTN_DUR_12 = {"id": "DUR_12", "title": "12-Max Discount"}

GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|hola|namaste|start|interested|"
    r"hi[!,.\s]|hello[!,.\s]|i'?m interested|i am interested)",
    re.IGNORECASE,
)

QUESTION_RE = re.compile(
    r"^\s*(what|how|why|when|where|who|which|can |do |does |is |are |will |should )",
    re.IGNORECASE,
)

EDIT_RE = re.compile(
    r"(edit|modify|change|add|remove|update|adjust|redo)",
    re.IGNORECASE,
)

FALLBACK_RECOVERY_TEXT = (
    "Maybe I am not able to help you with this, so I would want to give you "
    "5% additional discount and use this link to create your cart! You can "
    "always reachout to me if you need anything!"
)

SALES_CONTACT_TEXT = (
    "Or reach out to our sales team directly:\n"
    "Call/WhatsApp: +91 9958187021\n"
    "You can also browse at rentbasket.com"
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _reply(state: KuState) -> list[dict[str, Any]]:
    state.setdefault("reply", [])
    return state["reply"]


def _queue_text(state: KuState, body: str, preview_url: bool = False) -> None:
    _reply(state).append({"kind": "text", "body": body, "preview_url": preview_url})


def _queue_buttons(state: KuState, body: str, buttons: list[dict]) -> None:
    _reply(state).append({"kind": "buttons", "body": body, "buttons": buttons})


def _queue_image(state: KuState, image_url: str, caption: str) -> None:
    _reply(state).append({"kind": "image", "image_url": image_url, "caption": caption})


def _greeting_body(name: str | None) -> str:
    first = (name or "there").split()[0] if name else "there"
    return (
        f"Hi {first},\n"
        "I'm Ku from RentBasket, your personal rental assistant.\n"
        "*Please share the items you're looking for.* I'll share the "
        "complete cart right away.\n\n"
        "We offer quality furniture and appliances on rent at competitive "
        "prices, powered by customer service which is best in the market."
    )


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------

def classify_inbound(state: KuState) -> dict[str, Any]:
    """Deterministic router: pick a branch based on inbound + stage."""
    inbound = state.get("inbound") or {}
    i_type = inbound.get("type")
    i_id = inbound.get("interactive_id")
    text = (inbound.get("text") or "").strip()
    stage = state.get("stage", "NEW")

    branch: str
    if i_type == "interactive":
        if i_id == "HOW_RENTING_WORKS":
            branch = "how_works"
        elif i_id == "WHY_RENTBASKET":
            branch = "why_rentbasket"
        elif i_id == "REVIEWS":
            branch = "reviews"
        elif i_id == "SHARE_LIST":
            branch = "share_list"
        elif i_id == "LIST_VOICE":
            branch = "list_via_voice"
        elif i_id == "VOICE_NOTE":
            branch = "voice_note_prompt"
        elif i_id == "CONTINUE":
            branch = "continue_flow"
        elif i_id == "RESTART":
            branch = "restart_flow"
        elif i_id in ("DUR_3", "DUR_6", "DUR_12"):
            state["duration"] = {"DUR_3": 3, "DUR_6": 6, "DUR_12": 12}[i_id]
            branch = "build_cart"
        elif i_id == "CHECKOUT":
            branch = "checkout"
        elif i_id in ("RATING_YES", "RATING_MEH", "RATING_NO"):
            branch = "rating"
        else:
            state["fallback_reason"] = f"unknown_button:{i_id}"
            branch = "fallback_discount"
    elif i_type == "audio":
        branch = "extract_items"
    elif i_type == "text":
        if GREETING_RE.match(text) and (stage == "NEW" or stage == "GREETED" or not stage):
            branch = "greeting"
        elif stage in ("CART_SHOWN", "CHECKED_OUT") and EDIT_RE.search(text):
            branch = "edit_cart"
        elif stage in ("NEW", "GREETED", "ITEMS_CAPTURED", "CART_SHOWN", "CHECKED_OUT"):
            branch = "extract_items"
        else:
            state["fallback_reason"] = f"off_script_text_at_{stage}"
            branch = "fallback_discount"
    else:
        state["fallback_reason"] = f"unknown_inbound_type:{i_type}"
        branch = "fallback_discount"

    state["stage_before"] = stage
    state["branch"] = branch
    return state


# -- greeting --

def greeting(state: KuState) -> dict[str, Any]:
    name = state.get("push_name")
    _queue_buttons(
        state,
        _greeting_body(name),
        [BTN_HOW_RENTING_WORKS, BTN_SHARE_LIST, BTN_LIST_VOICE],
    )
    state["stage"] = "GREETED"
    return state


def how_works(state: KuState) -> dict[str, Any]:
    _queue_buttons(
        state,
        HOW_RENTING_WORKS_TEXT,
        [BTN_WHY_RENTBASKET, BTN_REVIEWS, BTN_SHARE_LIST],
    )
    return state


def why_rentbasket(state: KuState) -> dict[str, Any]:
    _queue_buttons(
        state,
        WHY_RENTBASKET_TEXT,
        [BTN_REVIEWS, BTN_SHARE_LIST],
    )
    return state


def reviews(state: KuState) -> dict[str, Any]:
    _queue_text(state, LATEST_REVIEWS_TEXT, preview_url=True)
    _queue_buttons(state, "Ready to proceed?", [BTN_CHECKOUT, BTN_SHARE_LIST])
    return state


def share_list(state: KuState) -> dict[str, Any]:
    """User tapped 'I'll share my list' — send catalogue + voice note nudge."""
    _queue_image(
        state,
        CATALOGUE_IMAGE_URL,
        "Great choice! Here's the Product Catalog!\n"
        "*Please share the items you're looking for.* I'll share the "
        "complete cart right away.\n\n"
        "Example : 2x Double Beds, 1x Washing Machine, 1x 5 Seater Sofa",
    )
    _queue_buttons(
        state,
        "Wanna try sending a Voice Note?",
        [BTN_VOICE_NOTE],
    )
    state["stage"] = "GREETED"
    return state


def list_via_voice(state: KuState) -> dict[str, Any]:
    """User tapped 'List via Voice Note' — send catalogue + voice prompt caption."""
    _queue_image(
        state,
        CATALOGUE_IMAGE_URL,
        "Great choice! Here's the Product Catalog!\n"
        "*Please share a voice note of your preferred items.* I'll share "
        "the complete cart right away.\n\n"
        "Example : I want two Double Beds, one Washing Machine, "
        "one 5 Seater Sofa",
    )
    state["stage"] = "GREETED"
    return state


def voice_note_prompt(state: KuState) -> dict[str, Any]:
    """User tapped 'Voice Note' on the nudge — prompt for a voice note."""
    _queue_text(
        state,
        "Great choice! Many prefer speaking their cart rather than typing it :)\n\n"
        "*Please share a voice note of your preferred items.* I'll share "
        "the complete cart right away.\n\n"
        "Example : I want two Double Beds, one Washing Machine, "
        "one 5 Seater Sofa",
    )
    state["stage"] = "GREETED"
    return state


def sales_activate(state: KuState) -> dict[str, Any]:
    """User typed 'SALES' — enable the full Order Confirmation cart format."""
    state["sales_mode"] = True
    _queue_text(
        state,
        "*SALES mode activated.*\n\n"
        "Your cart will now include the full Order Confirmation with "
        "taxes, security deposit, and terms & conditions.\n\n"
        "*Please share the items you're looking for* — text or voice note.\n\n"
        "Example : 2x Double Beds, 1x Washing Machine, 1x 5 Seater Sofa",
    )
    state["stage"] = "GREETED"
    return state


def continue_flow(state: KuState) -> dict[str, Any]:
    """User tapped 'Continue' on a follow-up — resume based on last stage."""
    stage = state.get("stage") or "NEW"
    items = state.get("items") or []
    cart_link = state.get("last_cart_link")

    if stage == "CHECKED_OUT" and cart_link:
        _queue_text(state, "Welcome back! Here's your cart link to complete checkout:")
        _queue_text(state, cart_link, preview_url=True)
    elif stage == "CART_SHOWN" and cart_link:
        _queue_text(state, "Welcome back! Here's where we left off:")
        _queue_text(state, cart_link, preview_url=True)
        _queue_buttons(state, "Ready to proceed?", [BTN_CHECKOUT, BTN_REVIEWS])
    elif stage == "ITEMS_CAPTURED" and items:
        summary = ", ".join(f"{it.get('qty',1)}x {it.get('name','')}" for it in items)
        _queue_text(state, f"Welcome back! You had these items: {summary}")
        _queue_buttons(
            state,
            "*Step 1 of 3: Duration*\n\nYour expected rental duration in months:",
            [BTN_DUR_3, BTN_DUR_6, BTN_DUR_12],
        )
    else:
        _queue_buttons(
            state,
            "Welcome back! *Please share the items you're looking for.* "
            "I'll share the complete cart right away.",
            [BTN_SHARE_LIST, BTN_LIST_VOICE],
        )
    return state


def restart_flow(state: KuState) -> dict[str, Any]:
    """User tapped 'RESTART' — clear their session and show greeting."""
    state["items"] = []
    state["duration"] = None
    state["duration_unit"] = None
    state["cart"] = None
    state["last_cart_link"] = None
    state["sales_mode"] = False
    state["stage"] = "NEW"

    name = state.get("push_name")
    _queue_buttons(
        state,
        _greeting_body(name),
        [BTN_HOW_RENTING_WORKS, BTN_SHARE_LIST, BTN_LIST_VOICE],
    )
    state["stage"] = "GREETED"
    return state


def edit_cart(state: KuState) -> dict[str, Any]:
    """User wants to edit their cart — provide the checkout link to modify."""
    cart_link = state.get("last_cart_link") or ""
    _queue_text(
        state,
        "No worries, if some products are not matching. You can easily "
        "modify it from this link:",
    )
    if cart_link:
        _queue_text(state, cart_link, preview_url=True)
    return state


# -- items extraction (text or voice) --

def extract_items(state: KuState) -> dict[str, Any]:
    inbound = state.get("inbound") or {}
    i_type = inbound.get("type")

    # 1. Get the source text (transcribe if voice)
    if i_type == "audio":
        audio_bytes = inbound.get("audio_bytes") or b""
        transcript = voice_parser.transcribe(audio_bytes)
        state["transcript"] = transcript
        source_text = transcript
        if transcript:
            _queue_text(state, f'Perfect, so you are saying : "{transcript}"')
        else:
            state["fallback_reason"] = "voice_transcribe_empty"
            return state  # conditional edge will route to fallback
    else:
        source_text = inbound.get("text", "") or ""

    if not source_text.strip():
        state["fallback_reason"] = "empty_source_text"
        return state

    # 2. Extract items + optional duration in a single LLM call
    intent = voice_parser.extract_intent(source_text)
    items = intent.get("items") or []
    duration = intent.get("duration")
    duration_unit = intent.get("duration_unit")

    # Merge with any pre-existing items if user is adding on.
    prev = state.get("items") or []
    if items:
        if prev:
            merged: dict[int, dict] = {it["product_id"]: dict(it) for it in prev}
            for it in items:
                pid = it["product_id"]
                if pid in merged:
                    merged[pid]["qty"] = int(merged[pid].get("qty", 1)) + int(it.get("qty", 1))
                else:
                    merged[pid] = it
            items = list(merged.values())
        state["items"] = items
    elif prev and duration:
        # Duration-only message (e.g. "8 months") with prior items already
        # captured — skip items confirmation and let after_extract route
        # straight to build_cart.
        items = prev  # use existing items for display
        state["duration"] = int(duration)
        if duration_unit:
            state["duration_unit"] = duration_unit
        state["stage"] = "ITEMS_CAPTURED"
        return state
    elif not prev:
        # No new items AND no prior items — fallback
        state["fallback_reason"] = "no_items_matched"
        return state
    # else: no new items but prior items exist (hydrated) — keep them, continue below

    if duration:
        state["duration"] = int(duration)
        if duration_unit:
            state["duration_unit"] = duration_unit

    # 3. Confirm what we found
    _queue_text(state, cart_builder.format_items_found(items))
    state["stage"] = "ITEMS_CAPTURED"
    return state


# -- duration prompt --

def duration_prompt(state: KuState) -> dict[str, Any]:
    _queue_buttons(
        state,
        "*Step 1 of 3: Duration*\n\nYour expected rental duration in months "
        "(you can always extend the duration):",
        [BTN_DUR_3, BTN_DUR_6, BTN_DUR_12],
    )
    return state


# -- build cart --

def build_cart_node(state: KuState) -> dict[str, Any]:
    items = state.get("items") or []
    duration = state.get("duration")
    unit = state.get("duration_unit") or "months"
    if not items or not duration:
        state["fallback_reason"] = "build_cart_missing_context"
        _fallback_inline(state)
        return state

    cart = cart_builder.build_cart(items, int(duration), unit=unit)
    if not cart:
        state["fallback_reason"] = "build_cart_returned_none"
        _fallback_inline(state)
        return state

    state["cart"] = cart
    state["last_cart_link"] = cart_builder.build_cart_link(items, int(duration), unit=unit)
    state["stage"] = "CART_SHOWN"

    if state.get("sales_mode"):
        cart_text = cart_builder.format_sales_draft_cart(cart, unit=unit)
    else:
        cart_text = cart_builder.format_cart_text(cart, unit=unit)

    _queue_buttons(
        state,
        cart_text,
        [BTN_CHECKOUT, BTN_REVIEWS],
    )
    return state


# -- checkout --

def checkout(state: KuState) -> dict[str, Any]:
    items = state.get("items") or []
    duration = state.get("duration") or 12
    unit = state.get("duration_unit") or "months"
    # Rebuild link in case it wasn't stored (e.g. reviews -> checkout path)
    if not state.get("last_cart_link"):
        state["last_cart_link"] = cart_builder.build_cart_link(items, int(duration), unit=unit)

    _queue_text(
        state,
        "Step 3 of 3: Checkout\n\nGet an additional *2% discount* by completing "
        "your order through this curated Cart with the Product Pics:",
    )
    _queue_text(state, state["last_cart_link"], preview_url=True)
    _queue_buttons(
        state,
        "Did Ku help you today?",
        [BTN_RATING_YES, BTN_RATING_MEH, BTN_RATING_NO],
    )
    state["stage"] = "CHECKED_OUT"
    return state


# -- rating handler --

def rating(state: KuState) -> dict[str, Any]:
    inbound = state.get("inbound") or {}
    i_id = inbound.get("interactive_id")
    rating_map = {"RATING_YES": "yes", "RATING_MEH": "meh", "RATING_NO": "no"}
    rating_value = rating_map.get(i_id, "unknown")
    state["rating"] = rating_value
    _queue_text(state, "Thanks for the feedback! It helps Ku get better.")
    return state


# -- fallback --

def _fallback_inline(state: KuState) -> None:
    """Inline fallback used by nodes that detect a dead-end mid-flow."""
    items = state.get("items") or []
    duration = state.get("duration") or 12
    link = cart_builder.build_cart_link(items, int(duration), fallback=True)
    state["last_cart_link"] = link
    _queue_text(state, FALLBACK_RECOVERY_TEXT)
    _queue_text(state, link, preview_url=True)
    state["fallback_triggered"] = True


def fallback_discount(state: KuState) -> dict[str, Any]:
    # Feature 1: If the user asked a question, try the KB first
    inbound = state.get("inbound") or {}
    text = (inbound.get("text") or "").strip()
    if text and QUESTION_RE.match(text):
        kb_answer = kb_retriever.search(text, k=3)
        if kb_answer:
            state["kb_hit"] = kb_answer
            _queue_text(state, kb_answer)

    # Feature 2: 5% discount link + sales contact
    _fallback_inline(state)
    _queue_text(state, SALES_CONTACT_TEXT)
    # Stage unchanged — we don't want to lock the user out of recovery.
    return state


# -- firestore persistence (terminal node) --

def write_firestore(state: KuState) -> dict[str, Any]:
    phone = state.get("phone") or ""
    if not phone:
        return state

    session_id = state.get("session_id")
    stage_before = state.get("stage_before")
    stage_after = state.get("stage")

    # Append outbound messages
    try:
        for r in state.get("reply", []):
            kind = r.get("kind")
            if kind == "text":
                firestore_store.append_message(
                    phone, direction="out", msg_type="text",
                    text=r.get("body"),
                    session_id=session_id,
                    stage_before=stage_before, stage_after=stage_after,
                )
            elif kind == "buttons":
                firestore_store.append_message(
                    phone, direction="out", msg_type="buttons",
                    text=r.get("body"),
                    session_id=session_id,
                    stage_before=stage_before, stage_after=stage_after,
                    extra={"buttons": r.get("buttons")},
                )
            elif kind == "image":
                firestore_store.append_message(
                    phone, direction="out", msg_type="image",
                    caption=r.get("caption"),
                    session_id=session_id,
                    stage_before=stage_before, stage_after=stage_after,
                    extra={"image_url": r.get("image_url")},
                )
    except Exception as e:
        log.warning("write_firestore outbound log failed: %s", e)

    # Highlights
    highlights: dict[str, Any] = {}
    branch = state.get("branch")
    if branch == "how_works":
        highlights["ever_clicked_how_works"] = True
    if branch == "reviews":
        highlights["ever_clicked_reviews"] = True
    if state.get("stage") == "CHECKED_OUT":
        highlights["ever_reached_checkout"] = True
    if state.get("rating"):
        highlights["last_rating"] = state["rating"]
    if state.get("duration"):
        highlights["preferred_duration_months"] = int(state["duration"])
        if state.get("duration_unit"):
            highlights["preferred_duration_unit"] = state["duration_unit"]
    if state.get("items"):
        items_list = state["items"]
        highlights["last_items_raw"] = ", ".join(
            f"{i.get('qty',1)}x {i.get('name')}" for i in items_list
        )
        # Always persist structured items so the next turn can hydrate them
        # (critical: duration button click needs items from the previous turn)
        highlights["last_cart_items"] = [
            {"product_id": it["product_id"], "qty": it.get("qty", 1), "name": it.get("name", "")}
            for it in items_list if it.get("product_id")
        ]
    if state.get("cart"):
        c = state["cart"]
        highlights["last_cart_items"] = [
            {
                "product_id": ln["product_id"],
                "qty": ln["qty"],
                "name": ln["name"],
                "monthly_rent": ln["disc_per_unit"],
            }
            for ln in c.get("lines", [])
        ]
        highlights["last_cart_monthly_total"] = c.get("disc_monthly_total")
        highlights["last_cart_savings"] = c.get("saving_per_month")
        highlights["last_cart_total_saving"] = c.get("total_saving")
    if state.get("last_cart_link"):
        highlights["last_cart_link"] = state["last_cart_link"]
    if state.get("sales_mode"):
        highlights["sales_mode"] = True

    if highlights:
        firestore_store.update_highlights(phone, highlights)

    # Append rating to persistent log (ArrayUnion) for historical tracking
    if state.get("rating"):
        firestore_store.append_note(
            phone,
            f"rating:{state['rating']}:session:{session_id}",
        )

    # Session summary
    if session_id:
        sess_update: dict[str, Any] = {"stage": state.get("stage")}
        if state.get("items"):
            sess_update["items_captured"] = True
        if state.get("duration"):
            sess_update["duration_chosen"] = int(state["duration"])
        if state.get("stage") == "CHECKED_OUT":
            sess_update["checkout_reached"] = True
        firestore_store.update_session(phone, session_id, sess_update)

    # Fallback RL log
    if state.get("fallback_triggered"):
        firestore_store.log_fallback_event(
            phone,
            session_id=session_id,
            last_stage=stage_before,
            inbound_payload=state.get("inbound"),
            last_cart_link=state.get("last_cart_link"),
            reason=state.get("fallback_reason") or "unknown",
        )

    return state


# --------------------------------------------------------------------------
# Conditional-edge router functions
# --------------------------------------------------------------------------

def route_branch(state: KuState) -> str:
    return state.get("branch") or "fallback_discount"


def after_extract(state: KuState) -> str:
    if state.get("fallback_reason") and not state.get("items"):
        return "fallback_discount"
    if state.get("items") and state.get("duration"):
        return "build_cart"
    if state.get("items"):
        return "duration_prompt"
    return "fallback_discount"
