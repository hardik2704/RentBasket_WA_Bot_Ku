"""Central configuration for the lean Ku bot.

Loads env, exposes pricing constants, WhatsApp creds, cart-link base URL, and a
JWT token for the lead-shopping API. All values are read once at import time;
nothing here should mutate at runtime.
"""

import os
import time

import jwt
from dotenv import load_dotenv

load_dotenv()

# ---------- WhatsApp Cloud API ----------
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "12345")
WHATSAPP_VERSION = os.getenv("VERSION", "v23.0")
WA_API_BASE = f"https://graph.facebook.com/{WHATSAPP_VERSION}"

# ---------- OpenAI ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
LLM_EXTRACTOR_MODEL = os.getenv("LLM_EXTRACTOR_MODEL", "gpt-4o")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

# ---------- Public base URL (used for /catalogue image delivery) ----------
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", "http://localhost:5000")

# ---------- Pricing (single source of truth) ----------
GLOBAL_DISCOUNT = 0.30               # 30% off MRP
RETAINED_FACTOR = 1 - GLOBAL_DISCOUNT
UPFRONT_EXTRA_DISCOUNT = 0.10        # extra 10% for 12-month upfront
UPFRONT_RETAINED_FACTOR = 1 - UPFRONT_EXTRA_DISCOUNT
DISCOUNT_PCT = int(round(GLOBAL_DISCOUNT * 100))
UPFRONT_PCT = int(round(UPFRONT_EXTRA_DISCOUNT * 100))

# ---------- Lead-shopping cart link ----------
CART_LINK_BASE_URL = os.getenv("CART_LINK_BASE_URL", "https://testqr.rentbasket.com/lead-shopping")
CART_LINK_REFERRAL_CODE = os.getenv("CART_LINK_REFERRAL_CODE", "ATFU1NTg1")
FALLBACK_DISCOUNT_REFERRAL_CODE = os.getenv("FALLBACK_DISCOUNT_REFERRAL_CODE") or CART_LINK_REFERRAL_CODE

RENTBASKET_JWT_SECRET = os.getenv("RENTBASKET_JWT_SECRET", "7QX2M9A4L5Z8R1T6C3K0H2F9D8P7B4")


def _mint_jwt() -> str:
    payload = {
        "iat": int(time.time()),
        "exp": int(time.time()) + (10 * 365 * 24 * 60 * 60),  # 10 years
        "data": {"id": 1, "email": "vijaymahen@gmail.com"},
    }
    return jwt.encode(payload, RENTBASKET_JWT_SECRET, algorithm="HS256")


RENTBASKET_JWT = os.getenv("RENTBASKET_JWT") or _mint_jwt()

# ---------- Firestore ----------
# Three supported modes (checked in order):
# 1. FIREBASE_CREDENTIALS_JSON env var — base64-encoded JSON (recommended for Render)
# 2. FIREBASE_CREDENTIALS_PATH env var — path to JSON file
# 3. GOOGLE_APPLICATION_CREDENTIALS env var — standard Google ADC path
# 4. Fallback: ./firebase-credentials.json (local dev)
_creds_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
_creds_path_explicit = os.getenv("FIREBASE_CREDENTIALS_PATH")
_creds_path_google = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

FIREBASE_CREDENTIALS_PATH = "./firebase-credentials.json"
FIREBASE_CREDENTIALS_SOURCE = "default_path"

if _creds_json:
    import base64
    import json
    import tempfile
    try:
        # Strip whitespace/newlines and fix missing padding (common copy-paste issues)
        cleaned = "".join(_creds_json.split())
        padding_needed = (-len(cleaned)) % 4
        if padding_needed:
            cleaned += "=" * padding_needed
        decoded_bytes = base64.b64decode(cleaned)
        # Value may already be JSON (user pasted raw JSON into the env var by mistake)
        if decoded_bytes[:1] == b"{":
            decoded = decoded_bytes.decode("utf-8")
        else:
            decoded = decoded_bytes.decode("utf-8")
        creds_obj = json.loads(decoded)
        _tmpdir = tempfile.gettempdir()
        FIREBASE_CREDENTIALS_PATH = os.path.join(_tmpdir, "firebase-creds.json")
        with open(FIREBASE_CREDENTIALS_PATH, "w") as f:
            json.dump(creds_obj, f)
        FIREBASE_CREDENTIALS_SOURCE = "env_base64_json"
        print(
            f"[config] Firebase creds decoded from FIREBASE_CREDENTIALS_JSON -> "
            f"{FIREBASE_CREDENTIALS_PATH} (project_id={creds_obj.get('project_id','?')}, "
            f"client_email={creds_obj.get('client_email','?')})",
            flush=True,
        )
    except Exception as e:
        # Maybe the user pasted raw JSON directly (not base64). Try that.
        try:
            creds_obj = json.loads(_creds_json)
            import tempfile as _tf
            _tmpdir = _tf.gettempdir()
            FIREBASE_CREDENTIALS_PATH = os.path.join(_tmpdir, "firebase-creds.json")
            with open(FIREBASE_CREDENTIALS_PATH, "w") as f:
                json.dump(creds_obj, f)
            FIREBASE_CREDENTIALS_SOURCE = "env_raw_json"
            print(
                f"[config] Firebase creds parsed as raw JSON (not base64) -> "
                f"{FIREBASE_CREDENTIALS_PATH} (project_id={creds_obj.get('project_id','?')})",
                flush=True,
            )
        except Exception as e2:
            print(
                f"[config] ERROR decoding FIREBASE_CREDENTIALS_JSON. "
                f"Base64 error: {type(e).__name__}: {e}. "
                f"JSON error: {type(e2).__name__}: {e2}. "
                f"Hint: value must be base64(service_account.json). "
                f"On macOS: cat firebase-credentials.json | base64 | tr -d '\\n'",
                flush=True,
            )
            FIREBASE_CREDENTIALS_PATH = _creds_path_explicit or _creds_path_google or "./firebase-credentials.json"
            FIREBASE_CREDENTIALS_SOURCE = "fallback_after_decode_error"
elif _creds_path_explicit:
    FIREBASE_CREDENTIALS_PATH = _creds_path_explicit
    FIREBASE_CREDENTIALS_SOURCE = "env_path"
    print(f"[config] Firebase creds path from FIREBASE_CREDENTIALS_PATH: {FIREBASE_CREDENTIALS_PATH}", flush=True)
elif _creds_path_google:
    FIREBASE_CREDENTIALS_PATH = _creds_path_google
    FIREBASE_CREDENTIALS_SOURCE = "env_google_adc"
    print(f"[config] Firebase creds path from GOOGLE_APPLICATION_CREDENTIALS: {FIREBASE_CREDENTIALS_PATH}", flush=True)
else:
    print(f"[config] No Firebase creds env var set, falling back to {FIREBASE_CREDENTIALS_PATH}", flush=True)

SESSION_GAP_HOURS = int(os.getenv("SESSION_GAP_HOURS", "24"))

# ---------- Bot ----------
BOT_NAME = os.getenv("BOT_NAME", "Ku")
DEDUP_TTL_SECONDS = int(os.getenv("DEDUP_TTL_SECONDS", "300"))
DEDUP_MAX_IDS = int(os.getenv("DEDUP_MAX_IDS", "500"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))

# ---------- Paths ----------
HERE = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(HERE, "public")
CHROMA_DIR = os.path.join(HERE, "data", "chroma_db")
CATALOGUE_IMAGE_PATH = os.path.join(PUBLIC_DIR, "RentBasket_Catalogue.png")
CATALOGUE_IMAGE_URL = f"{SERVER_BASE_URL.rstrip('/')}/catalogue"
