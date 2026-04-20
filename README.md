# RentBasket WhatsApp Bot — Ku (Lean Edition)

Production-grade minimal WhatsApp bot that implements the 5-stage rental flow:

1. Greeting + catalogue image + "How Renting Works?" button
2. "How Renting Works?" -> 4-step info
3. User shares items (typed list OR voice note) -> items echoed
4. Duration picked (3 / 6 / 12 buttons, or extracted from the items message)
5. Cart shown with [Checkout] [Reviews] buttons -> Checkout sends lead-shopping link

Agentic core is a LangGraph StateGraph. State + conversation log + user highlights are persisted in Firestore (append-only). Any unmatched path routes to a `fallback_discount` node that sends a 5% recovery link and flags the session for manual RL review.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill .env with WhatsApp creds, OpenAI key, public base URL, Firestore credentials path
python webhook_server.py  # local dev
```

Expose via `ngrok http 5000` and register the URL in the Meta developer console.

## Deploy

`Procfile` is configured for Render / Railway:

```
web: gunicorn webhook_server:app --workers 2 --threads 4 --timeout 60
```
