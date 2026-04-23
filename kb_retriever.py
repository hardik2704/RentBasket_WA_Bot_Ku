"""Knowledge-base retriever.

Lean Chroma-backed retriever over the static knowledge base, product catalog,
and support policies.

Design:
  - Persistent on disk at `data/chroma_db/` so cold boots don't re-embed.
  - Warm singleton in-process: the Chroma collection is loaded once and held.
  - Used only as a fallback *before* the 5%-discount recovery fires — for
    off-script questions ("what's the refund policy?", "do you deliver to
    Noida?", "what sofas do you have?", "how much is a fridge?").
  - Product data is indexed so semantic search can answer product/pricing
    queries using real catalog data instead of hallucinating.
"""

from __future__ import annotations

import logging
import os

from config import CHROMA_DIR, EMBEDDING_MODEL, OPENAI_API_KEY
from data.knowledge_base import RENTBASKET_KNOWLEDGE_BASE
from data.support_policies import SUPPORT_POLICIES
from data.products import (
    id_to_name,
    id_to_price,
    category_to_id,
    PRODUCT_SYNONYMS,
    calculate_rent,
    get_all_categories,
)

log = logging.getLogger(__name__)

_vectorstore = None
_unavailable_reason: str | None = None


def _build_documents():
    """Seed documents for the KB — core T&C + products + support policies."""
    from langchain_core.documents import Document

    docs: list[Document] = []

    # ── 1. Core knowledge base (T&C, FAQs, company info) ──
    docs.append(
        Document(page_content=RENTBASKET_KNOWLEDGE_BASE, metadata={"type": "tnc"})
    )

    # ── 2. Canonical blurbs (How Renting Works, Why RentBasket) ──
    how_renting_works = (
        "Let's get you settled! Here is your 4-step journey with RentBasket:\n\n"
        "1. Select Products (2 mins) — Pick your items.\n"
        "2. Secure & Relax — Pay a one-time refundable deposit. 95% of our "
        "customers get their full deposit back.\n"
        "3. Free Installation (72 hrs) — We deliver and install everything for "
        "FREE within 72 hours.\n"
        "4. Enjoy & Live — Pay monthly rent and leave the maintenance to us. "
        "If you move, we'll relocate your items for zero extra cost."
    )
    why_rentbasket = (
        "Why RentBasket: 4.9 Google Star Rating, hyper-localization in Gurgaon "
        "and Noida, 95% full security refund track record, customer-first "
        "service."
    )
    docs.append(
        Document(page_content=how_renting_works, metadata={"type": "how_renting_works"})
    )
    docs.append(
        Document(page_content=why_rentbasket, metadata={"type": "why_rentbasket"})
    )

    # ── 3. Support policies ──
    for policy_key, policy_data in SUPPORT_POLICIES.items():
        desc = policy_data.get("description", "")
        points = "\n".join(f"• {p}" for p in policy_data.get("points", []))
        content = f"Support Policy — {policy_key.title()}\n{desc}\n\n{points}"
        docs.append(
            Document(page_content=content, metadata={"type": "support_policy", "policy": policy_key})
        )

    # ── 4. Product catalog — one document per category for semantic search ──
    categories = get_all_categories()
    for cat in categories:
        product_ids = category_to_id.get(cat, [])
        if not product_ids:
            continue

        lines = [f"RentBasket Product Category: {cat.title()}\n"]
        lines.append(f"Available {cat.title()} products for rent:\n")

        for pid in product_ids:
            name = id_to_name.get(pid, f"Product #{pid}")
            prices = id_to_price.get(pid, [])

            # Calculate representative monthly rents at key durations
            rent_3 = calculate_rent(pid, 3) or 0
            rent_6 = calculate_rent(pid, 6) or 0
            rent_12 = calculate_rent(pid, 12) or 0

            # Apply 30% discount (same as cart_builder)
            from data.products import apply_discount
            disc_3 = apply_discount(rent_3)
            disc_6 = apply_discount(rent_6)
            disc_12 = apply_discount(rent_12)

            lines.append(
                f"• {name} (ID: {pid})\n"
                f"  Monthly rent after 30% discount: "
                f"3mo: ₹{disc_3}/mo, 6mo: ₹{disc_6}/mo, 12mo: ₹{disc_12}/mo"
            )

        # Add synonyms so semantic search can match informal queries
        synonyms = PRODUCT_SYNONYMS.get(cat, [])
        if synonyms:
            lines.append(f"\nAlso known as: {', '.join(synonyms[:10])}")

        content = "\n".join(lines)
        docs.append(
            Document(page_content=content, metadata={"type": "product_catalog", "category": cat})
        )

    # ── 5. Full product list summary (for broad "what do you have?" queries) ──
    all_products_lines = ["RentBasket — Complete Product Catalog\n"]
    all_products_lines.append("We offer the following products on rent:\n")
    for cat in categories:
        pids = category_to_id.get(cat, [])
        names = [id_to_name.get(pid, "") for pid in pids if pid in id_to_name]
        if names:
            all_products_lines.append(f"• {cat.title()}: {', '.join(names)}")
    all_products_lines.append(
        "\nAll products come with free delivery, installation, and maintenance. "
        "Prices vary by rental duration — longer durations get better rates."
    )
    docs.append(
        Document(
            page_content="\n".join(all_products_lines),
            metadata={"type": "product_summary"},
        )
    )

    return docs


def ensure_vectorstore():
    """Load or build the persistent vectorstore once."""
    global _vectorstore, _unavailable_reason
    if _vectorstore is not None:
        return _vectorstore
    if _unavailable_reason:
        return None
    if not OPENAI_API_KEY:
        _unavailable_reason = "OPENAI_API_KEY not set"
        log.warning("KB disabled: %s", _unavailable_reason)
        return None
    try:
        from langchain_community.vectorstores import Chroma
        from langchain_openai import OpenAIEmbeddings
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        embeddings = OpenAIEmbeddings(model=EMBEDDING_MODEL)
        os.makedirs(CHROMA_DIR, exist_ok=True)
        has_existing = any(
            not n.startswith(".") for n in os.listdir(CHROMA_DIR)
        ) if os.path.isdir(CHROMA_DIR) else False

        if has_existing:
            _vectorstore = Chroma(
                persist_directory=CHROMA_DIR,
                embedding_function=embeddings,
                collection_name="rentbasket_kb",
            )
            log.info("KB loaded from disk (%s)", CHROMA_DIR)
        else:
            splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=100)
            chunks = splitter.split_documents(_build_documents())
            _vectorstore = Chroma.from_documents(
                documents=chunks,
                embedding=embeddings,
                persist_directory=CHROMA_DIR,
                collection_name="rentbasket_kb",
            )
            log.info("KB built from scratch (%d chunks) at %s", len(chunks), CHROMA_DIR)
        return _vectorstore
    except Exception as e:
        _unavailable_reason = str(e)
        log.warning("KB unavailable: %s", e)
        return None


def search(query: str, k: int = 3) -> str:
    """Return top-k concatenated chunks, or "" if unavailable."""
    vs = ensure_vectorstore()
    if not vs:
        return ""
    try:
        docs = vs.similarity_search(query, k=k)
        return "\n\n---\n\n".join(d.page_content for d in docs)
    except Exception as e:
        log.warning("KB search failed: %s", e)
        return ""


def rebuild_index():
    """Force rebuild the vector index (e.g. after product data changes).

    Call this from a management script or on deploy if data changes.
    """
    global _vectorstore, _unavailable_reason
    _vectorstore = None
    _unavailable_reason = None
    # Remove existing Chroma DB to force re-index
    import shutil
    if os.path.isdir(CHROMA_DIR):
        shutil.rmtree(CHROMA_DIR)
        log.info("Removed old Chroma DB at %s", CHROMA_DIR)
    return ensure_vectorstore()
