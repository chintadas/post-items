import os
from dotenv import load_dotenv

load_dotenv()

GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
SHOPIFY_SHOP_URL = os.getenv("SHOPIFY_SHOP_URL")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET")
SHOPIFY_API_VERSION = "2026-04"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN")
API_AUTH_KEY = os.getenv("API_AUTH_KEY")

def validate_required_config() -> None:
    """Fail fast with a clear message when required config is missing."""
    required_config = {
        "GCS_BUCKET_NAME": GCS_BUCKET_NAME,
        "SHOPIFY_SHOP_URL": SHOPIFY_SHOP_URL,
        "SHOPIFY_CLIENT_ID": SHOPIFY_CLIENT_ID,
        "SHOPIFY_CLIENT_SECRET": SHOPIFY_CLIENT_SECRET,
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "API_AUTH_KEY": API_AUTH_KEY,
    }
    missing = [key for key, value in required_config.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Set them in your .env or shell before starting the app."
        )

validate_required_config()
