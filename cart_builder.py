"""Cart math + Step 2 message formatting + lead-shopping link builder.

Inputs come in as `items = [{"product_id": int, "qty": int, "name": str?}]`.
All pricing goes through `products.calculate_rent` + `products.apply_discount`
so every discount/upfront change is centralised in `config.py`.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

from config import (
    CART_LINK_BASE_URL,
    CART_LINK_REFERRAL_CODE,
    DISCOUNT_PCT,
    FALLBACK_DISCOUNT_REFERRAL_CODE,
    RENTBASKET_JWT,
    UPFRONT_PCT,
)
from products import apply_discount, calculate_rent, id_to_name

log = logging.getLogger(__name__)


def _inr(n: int | float) -> str:
    """Indian comma formatting: 12345 -> 12,345; 1234567 -> 12,34,567."""
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        return str(n)
    s = str(abs(n))
    if len(s) <= 3:
        out = s
    else:
        last3 = s[-3:]
        rest = s[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        out = ",".join(groups) + "," + last3
    return ("-" if n < 0 else "") + out


def build_cart(items: list[dict], duration: int, unit: str = "months") -> dict[str, Any] | None:
    """Compute per-line + total pricing for `items` at `duration` in given unit (months/days).

    Returns None if no valid priced items (caller should fallback).
    """
    if not items or duration < 1:
        return None

    lines: list[dict[str, Any]] = []
    mrp_monthly_total = 0
    disc_monthly_total = 0
    upfront_monthly_total = 0

    for it in items:
        try:
            pid = int(it["product_id"])
            qty = max(1, int(it.get("qty", 1)))
        except (TypeError, ValueError, KeyError):
            continue
        mrp = calculate_rent(pid, duration, unit=unit)
        if mrp is None:
            log.warning("No price for product_id=%s duration=%s", pid, duration)
            continue
        disc = apply_discount(mrp)
        upfront = apply_discount(mrp, upfront=True, upfront_percent=UPFRONT_PCT)

        line_mrp = mrp * qty
        line_disc = disc * qty
        line_upfront = upfront * qty

        mrp_monthly_total += line_mrp
        disc_monthly_total += line_disc
        upfront_monthly_total += line_upfront

        lines.append({
            "product_id": pid,
            "qty": qty,
            "name": it.get("name") or id_to_name.get(pid, f"Product #{pid}"),
            "mrp_per_unit": mrp,
            "disc_per_unit": disc,
            "upfront_per_unit": upfront,
            "line_mrp": line_mrp,
            "line_disc": line_disc,
            "line_upfront": line_upfront,
        })

    if not lines:
        return None

    saving_per_month = mrp_monthly_total - disc_monthly_total
    total_saving = saving_per_month * duration
    upfront_total_saving = (mrp_monthly_total - upfront_monthly_total) * duration

    return {
        "duration": duration,
        "lines": lines,
        "mrp_monthly_total": mrp_monthly_total,
        "disc_monthly_total": disc_monthly_total,
        "upfront_monthly_total": upfront_monthly_total,
        "saving_per_month": saving_per_month,
        "total_saving": total_saving,
        "upfront_total_saving": upfront_total_saving,
    }


def format_cart_text(cart: dict, unit: str = "months") -> str:
    duration = cart["duration"]
    unit_label = "Month" if unit == "months" else "Day"
    header = f"*Step 2 of 3: Cart Finalisation*\n\nYour Draft Cart: {duration} {unit_label} Rental\n\n"

    line_strs = []
    for line in cart["lines"]:
        name = line["name"]
        qty = line["qty"]
        disc = line["disc_per_unit"]
        mrp_line = line["line_mrp"]
        if qty > 1:
            line_strs.append(
                f"• {name} x{qty}:  Rs. {_inr(disc)} x {qty} = Rs. {_inr(line['line_disc'])}/mo "
                f"(was Rs. {_inr(mrp_line)})"
            )
        else:
            line_strs.append(
                f"• {name}:  Rs. {_inr(disc)}/mo  (was Rs. {_inr(mrp_line)})"
            )

    body = "\n".join(line_strs)

    unit_suffix = "/mo" if unit == "months" else "/day"
    totals = (
        f"\n\nMonthly Total: Rs. {_inr(cart['disc_monthly_total'])}  "
        f"(saving Rs. {_inr(cart['saving_per_month'])}{unit_suffix} vs MRP)\n"
        f"Total saving over {duration} {unit_label.lower()}s: Rs. {_inr(cart['total_saving'])}"
    )

    msg = header + body + totals

    if duration >= 12:
        msg += (
            f"\n\nPay upfront for the duration and it would cost "
            f"Rs. {_inr(cart['upfront_monthly_total'])}/mo  "
            f"(extra {UPFRONT_PCT}% off — save Rs. {_inr(cart['upfront_total_saving'])} total)"
        )

    return msg


def build_cart_link(items: list[dict], duration: int, *, unit: str = "months", fallback: bool = False) -> str:
    """URL that opens the curated cart on the RentBasket checkout site."""
    payload = []
    for it in items or []:
        try:
            pid = int(it["product_id"])
            qty = max(1, int(it.get("qty", 1)))
            payload.append({"amenity_type_id": pid, "count": qty, "duration": int(duration)})
        except (TypeError, ValueError, KeyError):
            continue

    # If we have nothing, use a generic 2-item sample so the link still opens.
    if not payload:
        payload = [
            {"amenity_type_id": 17, "count": 2, "duration": int(duration or 12)},
            {"amenity_type_id": 13, "count": 1, "duration": int(duration or 12)},
        ]

    referral = FALLBACK_DISCOUNT_REFERRAL_CODE if fallback else CART_LINK_REFERRAL_CODE
    items_q = quote(json.dumps(payload, separators=(",", ":")), safe=":,")
    return (
        f"{CART_LINK_BASE_URL}?token={RENTBASKET_JWT}"
        f"&referral_code={referral}"
        f"&items={items_q}"
    )


def format_sales_cart(cart: dict, unit: str = "months") -> str:
    """Full Order Confirmation format (SALES mode) — mirrors the website.

    Adds GST, security deposit, delivery/installation promo, and T&C.
    """
    duration = cart["duration"]
    unit_str = "/mo" if unit == "months" else ""

    order_lines = []
    for line in cart["lines"]:
        qty = line["qty"]
        name = line["name"]
        orig = line["mrp_per_unit"]
        disc = line["disc_per_unit"]
        qty_label = f"{qty}x" if qty > 1 else "1x"
        order_lines.append(
            f"• {qty_label} {name} ({duration} {unit})\n"
            f"  ~Rs. {_inr(orig)}{unit_str}~ *Rs. {_inr(disc)}{unit_str}* + GST"
        )

    total_disc = cart["disc_monthly_total"]
    total_savings = cart["saving_per_month"]
    gst = int(round(total_disc * 0.18))
    net_monthly = total_disc + gst

    transport = 400
    installation = 500
    security = min(int(round(total_disc * 2)), 15000)
    net_first_month = security + net_monthly

    sep = "\u2501" * 20  # ━━━━━

    return (
        f"*Order Confirmation*\n"
        f"{sep}\n\n"
        f"*Order Details*\n"
        + "\n".join(order_lines) +
        f"\n\n{sep}\n"
        f"*Monthly Rent*\n"
        f"Rent          Rs. {_inr(total_disc)}/mo\n"
        f"GST (18%)     Rs. {_inr(gst)}/mo\n"
        f"*Net Monthly  Rs. {_inr(net_monthly)}/mo*\n\n"
        f"{sep}\n"
        f"*One Time Charges*\n"
        f"Security Deposit   Rs. {_inr(security)} _(refundable)_\n"
        f"Delivery           ~Rs. {_inr(transport)}~ Rs. 0\n"
        f"Installation       ~Rs. {_inr(installation)}~ Rs. 0\n"
        f"*Net Payable (1st Month)   Rs. {_inr(net_first_month)}*\n\n"
        f"{sep}\n"
        f"You save *Rs. {_inr(total_savings)}/month* x {duration} {unit} = "
        f"*Rs. {_inr(total_savings * duration)}* on this cart!\n\n"
        f"*Terms & Conditions*\n"
        f"- Products are in mint condition\n"
        f"- Standard maintenance included\n"
        f"- Free shipping & standard installation\n"
        f"- Complete KYC before delivery"
    )


def format_items_found(items: list[dict]) -> str:
    """'Got it! I found: <1x X, 2x Y>.' style confirmation line."""
    if not items:
        return "Got it! I couldn't match any items yet — could you tell me what you'd like to rent?"
    parts = []
    for it in items:
        name = it.get("name") or id_to_name.get(it.get("product_id"), f"Product #{it.get('product_id')}")
        qty = max(1, int(it.get("qty", 1)))
        parts.append(f"{qty}x {name}")
    return f"Got it! I found: {', '.join(parts)}."


# Display helpers exposed for other modules
__all__ = [
    "build_cart",
    "format_cart_text",
    "format_sales_cart",
    "build_cart_link",
    "format_items_found",
    "DISCOUNT_PCT",
    "UPFRONT_PCT",
]
