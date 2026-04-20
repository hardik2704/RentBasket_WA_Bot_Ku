"""LangGraph wiring for the Ku bot.

```
START -> classify_inbound
  ├── greeting ─────┐
  ├── how_works ────┤
  ├── reviews ──────┤
  ├── checkout ─────┤
  ├── build_cart ───┤
  ├── extract_items ┤── (conditional: build_cart | duration_prompt | fallback_discount)
  └── fallback_discount ─┐
                         │
     duration_prompt ────┤
                         ▼
                   write_firestore -> END
```
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, StateGraph

import graph_nodes as gn
from graph_state import KuState


def _build() -> StateGraph:
    g = StateGraph(KuState)

    g.add_node("classify_inbound", gn.classify_inbound)
    g.add_node("greeting", gn.greeting)
    g.add_node("how_works", gn.how_works)
    g.add_node("reviews", gn.reviews)
    g.add_node("extract_items", gn.extract_items)
    g.add_node("duration_prompt", gn.duration_prompt)
    g.add_node("build_cart", gn.build_cart_node)
    g.add_node("checkout", gn.checkout)
    g.add_node("fallback_discount", gn.fallback_discount)
    g.add_node("write_firestore", gn.write_firestore)

    g.set_entry_point("classify_inbound")
    g.add_conditional_edges(
        "classify_inbound",
        gn.route_branch,
        {
            "greeting": "greeting",
            "how_works": "how_works",
            "reviews": "reviews",
            "extract_items": "extract_items",
            "build_cart": "build_cart",
            "checkout": "checkout",
            "fallback_discount": "fallback_discount",
        },
    )

    g.add_conditional_edges(
        "extract_items",
        gn.after_extract,
        {
            "build_cart": "build_cart",
            "duration_prompt": "duration_prompt",
            "fallback_discount": "fallback_discount",
        },
    )

    for node in ("greeting", "how_works", "reviews", "duration_prompt",
                 "build_cart", "checkout", "fallback_discount"):
        g.add_edge(node, "write_firestore")
    g.add_edge("write_firestore", END)

    return g


@lru_cache(maxsize=1)
def get_graph():
    """Return the compiled graph (singleton)."""
    return _build().compile()
