import os
import json
import traceback
import shopify
import requests
from urllib.parse import urlparse
from datetime import timedelta
from typing import List

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from dotenv import load_dotenv

from google.cloud import storage
import google.genai as genai
from google.genai import types

# Load environment variables from .env file
load_dotenv()

app = FastAPI(title="Snazzy Boutique Listing Agent")

# --- Configuration & Credentials ---
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
SHOPIFY_SHOP_URL = os.getenv("SHOPIFY_SHOP_URL")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET")
SHOPIFY_API_VERSION = "2026-04"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN")
API_AUTH_KEY = os.getenv("API_AUTH_KEY") # Shared secret with your iPhone

DEFAULT_LISTING_PROMPT = """
Analyze these clothing images (front, back, tags).
Return a JSON object exactly with these keys:
- title: Professional product name along with size
- description: Give poshmark description for this item. Give fit and features, material, size, measurements, and style tags, secondhand price, and retail. Engaging description with style notes.
- brand: Found on tag
- size: Found on tag
- material: From care label
- tags: List of 5 styling vibes (e.g. 'vintage', 'dark academia')
- price: Suggested resale price based on brand/condition
- retail: Retail price of the item
"""

class PromptExperimentRequest(BaseModel):
    folder_name: str
    prompt: str

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

# --- Initialize Clients ---
# GCS (Uses GOOGLE_APPLICATION_CREDENTIALS env var for service account JSON)
storage_client = storage.Client()
bucket = storage_client.bucket(GCS_BUCKET_NAME)

# Gemini
client = genai.Client(api_key=GEMINI_API_KEY)

# --- Helper Functions ---

def get_shop_domain(shop_url: str) -> str:
    """Normalizes SHOPIFY_SHOP_URL into a bare shop domain."""
    parsed = urlparse(shop_url)
    if parsed.netloc:
        return parsed.netloc
    cleaned = shop_url.replace("https://", "").replace("http://", "")
    return cleaned.split("/")[0]

def fetch_shopify_access_token() -> str:
    """Fetches a short-lived Shopify Admin access token via client credentials."""
    if not SHOPIFY_CLIENT_ID or not SHOPIFY_CLIENT_SECRET:
        raise ValueError("SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET must be set.")

    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    token_url = f"https://{shop_domain}/admin/oauth/access_token"
    response = requests.post(
        token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": SHOPIFY_CLIENT_ID,
            "client_secret": SHOPIFY_CLIENT_SECRET,
        },
        timeout=20,
    )
    response.raise_for_status()
    token_payload = response.json()
    access_token = token_payload.get("access_token")
    if not access_token:
        raise ValueError(f"No access_token in Shopify response: {token_payload}")
    return access_token

def activate_shopify_session_with_fresh_token() -> None:
    """Fetches a fresh access token and activates Shopify session."""
    access_token = fetch_shopify_access_token()
    session = shopify.Session(get_shop_domain(SHOPIFY_SHOP_URL), SHOPIFY_API_VERSION, access_token)
    shopify.ShopifyResource.activate_session(session)

def generate_signed_url(blob_name: str):
    """Generates a 15-minute temporary link for Shopify to fetch the image."""
    blob = bucket.blob(blob_name)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=15),
        method="GET",
    )

def send_pushover(message: str):
    """Sends a push notification to your iPhone."""
    url = "https://api.pushover.net/1/messages.json"
    data = {
        "token": PUSHOVER_API_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "message": message
    }
    requests.post(url, data=data)

async def analyze_images_via_vlm(
    gcs_paths: List[str],
    generate_dummy: bool = False,
    prompt_override: str | None = None,
):
    """Sends GCS image paths to Gemini for structured listing data."""
    if generate_dummy:
        return {
            "title": "Vintage Denim Jacket",
            "description": "Classic vintage denim jacket with a relaxed fit and timeless styling.",
            "brand": "Levi's",
            "size": "M",
            "material": "100% Cotton",
            "tags": ["vintage", "casual", "streetwear", "classic", "layering"],
            "price": "49.99"
        }

    prompt = prompt_override.strip() if prompt_override else DEFAULT_LISTING_PROMPT
    
    # Construct parts for the model
    contents = [prompt]
    for path in gcs_paths:
        contents.append(types.Part.from_bytes(mime_type="image/jpeg", data=bucket.blob(path).download_as_bytes()))
        print(f"Added image for analysis: {path}")
    
    response = client.models.generate_content(model="gemini-2.5-flash", contents=contents)
    print(f"Raw Gemini response: {response.text}")
    # Strip any markdown formatting Gemini might add
    clean_json = response.text.replace('```json', '').replace('```', '').strip()
    try:
        return json.loads(clean_json)
    except json.JSONDecodeError:
        # Return the model's raw text upstream as the error message source.
        raise ValueError(response.text)

def get_image_paths_for_folder(folder_name: str) -> List[str]:
    """Returns image blob paths for a given top-level folder."""
    prefix = f"{folder_name}/"
    blobs = list(bucket.list_blobs(prefix=prefix))
    image_paths = [b.name for b in blobs if b.name.lower().endswith((".jpg", ".jpeg", ".png"))]
    return image_paths

def move_folder_to_listed(folder_name: str) -> None:
    """Moves all blobs under folder_name/ to listed/folder_name/."""
    source_prefix = f"{folder_name}/"
    destination_prefix = f"listed/{folder_name}/"
    blobs = list(bucket.list_blobs(prefix=source_prefix))

    for blob in blobs:
        destination_name = blob.name.replace(source_prefix, destination_prefix, 1)
        bucket.copy_blob(blob, bucket, destination_name)
        blob.delete()

def get_pending_folders() -> List[str]:
    """Returns top-level folders that are not already in listed/."""
    folder_names = set()
    for blob in bucket.list_blobs():
        parts = blob.name.split("/")
        if len(parts) < 2:
            continue
        top_level = parts[0].strip()
        if not top_level or top_level == "listed":
            continue
        folder_names.add(top_level)
    return sorted(folder_names)

async def process_folder_listing(folder_name: str) -> dict:
    """Lists one folder to Shopify and returns operation metadata."""
    image_paths = get_image_paths_for_folder(folder_name)
    if not image_paths:
        raise ValueError("No images found in that folder.")

    print(f"Found {len(image_paths)} images for folder '{folder_name}': {image_paths}")
    data = await analyze_images_via_vlm(image_paths, generate_dummy=False)

    activate_shopify_session_with_fresh_token()
    new_product = shopify.Product()
    new_product.title = data["title"]
    new_product.body_html = f"<p>{data['description']}</p><p><b>Material:</b> {data['material']}</p>"
    new_product.vendor = data["brand"]
    new_product.tags = ",".join(data["tags"])
    new_product.status = "draft"

    variant = shopify.Variant({"price": data["price"], "option1": data["size"]})
    new_product.variants = [variant]
    new_product.images = [{"src": generate_signed_url(path)} for path in image_paths]

    if not new_product.save():
        raise Exception("Failed to save to Shopify")

    print(f"✅ Shopify save successful for folder '{folder_name}'.")
    print(f"Shopify product id: {new_product.id}")

    move_folder_to_listed(folder_name)
    msg = f"✅ Published: {data['title']} ({data['brand']}) as a draft."
    send_pushover(msg)
    return {"status": "success", "product_id": new_product.id, "title": data["title"]}

async def preview_folder_listing_data(folder_name: str, prompt: str) -> dict:
    """Runs prompt + images through Gemini and returns parsed listing JSON only."""
    image_paths = get_image_paths_for_folder(folder_name)
    if not image_paths:
        raise ValueError("No images found in that folder.")

    print(f"Previewing LLM output for folder '{folder_name}' with {len(image_paths)} images.")
    data = await analyze_images_via_vlm(
        image_paths,
        generate_dummy=False,
        prompt_override=prompt,
    )
    return {
        "status": "success",
        "folder_name": folder_name,
        "image_count": len(image_paths),
        "product_data": data,
    }

# --- API Endpoints ---

@app.post("/list-item/{folder_name}")
async def list_item(folder_name: str, x_api_key: str = Header(None)):
    # 1. Simple Auth Check
    if x_api_key != API_AUTH_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")
    try:
        result = await process_folder_listing(folder_name)
        return result

    except Exception as e:
        print(f"Error listing {folder_name}: {e}")
        print(traceback.format_exc())
        error_msg = str(e)
        send_pushover(f"❌ Error listing {folder_name}: {error_msg}")
        return {"status": "error", "error_msg": error_msg}

@app.post("/list-all-items")
async def list_all_items(x_api_key: str = Header(None)):
    # 1. Simple Auth Check
    if x_api_key != API_AUTH_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    folders = get_pending_folders()
    if not folders:
        return {"status": "success", "message": "No pending folders found.", "processed": 0, "results": []}

    results = []
    success_count = 0

    for folder_name in folders:
        try:
            folder_result = await process_folder_listing(folder_name)
            results.append({"folder": folder_name, **folder_result})
            success_count += 1
        except Exception as e:
            error_msg = str(e)
            print(f"Error listing {folder_name}: {e}")
            print(traceback.format_exc())
            send_pushover(f"❌ Error listing {folder_name}: {error_msg}")
            results.append({"folder": folder_name, "status": "error", "error_msg": error_msg})

    return {
        "status": "success",
        "processed": len(folders),
        "successful": success_count,
        "failed": len(folders) - success_count,
        "results": results,
    }

@app.post("/preview-listing")
async def preview_listing(payload: PromptExperimentRequest, x_api_key: str = Header(None)):
    # 1. Simple Auth Check
    if x_api_key != API_AUTH_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    try:
        return await preview_folder_listing_data(
            folder_name=payload.folder_name,
            prompt=payload.prompt,
        )
    except Exception as e:
        print(f"Error previewing folder {payload.folder_name}: {e}")
        print(traceback.format_exc())
        return {"status": "error", "error_msg": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
