"""Knowledge-base retriever.

Lean Chroma-backed retriever over the static knowledge base + canonical info
blocks (How Renting Works, Why RentBasket, Latest Reviews).

Design:
  - Persistent on disk at `data/chroma_db/` so cold boots don't re-embed.
  - Warm singleton in-process: the Chroma collection is loaded once and held.
  - Used only as a fallback *before* the 5%-discount recovery fires — for
    off-script questions ("what's the refund policy?", "do you deliver to
    Noida?"). Deterministic buttons never call this.
"""

from __future__ import annotations

import logging
import os

from config import CHROMA_DIR, EMBEDDING_MODEL, OPENAI_API_KEY
from knowledge_base import RENTBASKET_KNOWLEDGE_BASE

log = logging.getLogger(__name__)

_vectorstore = None
_unavailable_reason: str | None = None


def _build_documents():
    """Seed documents for the KB — core T&C + canonical blurbs."""
    from langchain_core.documents import Document

    # Inline canonical blurbs so we don't circular-import from graph_nodes.
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
    return [
        Document(page_content=RENTBASKET_KNOWLEDGE_BASE, metadata={"type": "tnc"}),
        Document(page_content=how_renting_works, metadata={"type": "how_renting_works"}),
        Document(page_content=why_rentbasket, metadata={"type": "why_rentbasket"}),
    ]


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
