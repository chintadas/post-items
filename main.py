import os
import json
import shopify
import requests
from datetime import timedelta
from typing import List

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from dotenv import load_dotenv

from google.cloud import storage
import google.generativeai as genai

# Load environment variables from .env file
load_dotenv()

app = FastAPI(title="Snazzy Boutique Listing Agent")

# --- Configuration & Credentials ---
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
SHOPIFY_SHOP_URL = os.getenv("SHOPIFY_SHOP_URL")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN")
API_AUTH_KEY = os.getenv("API_AUTH_KEY") # Shared secret with your iPhone

# --- Initialize Clients ---
# GCS (Uses GOOGLE_APPLICATION_CREDENTIALS env var for service account JSON)
storage_client = storage.Client()
bucket = storage_client.bucket(GCS_BUCKET_NAME)

# Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash') # Using Flash for speed/cost

# Shopify Session
session = shopify.Session(SHOPIFY_SHOP_URL, "2024-04", SHOPIFY_ACCESS_TOKEN)
shopify.ShopifyResource.activate_session(session)

# --- Helper Functions ---

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

async def analyze_images_via_vlm(gcs_paths: List[str]):
    """Sends GCS image paths to Gemini for structured listing data."""
    # Note: Gemini 1.5+ can ingest GCS URIs directly
    prompt = """
    Analyze these clothing images (front, back, tags). 
    Return a JSON object exactly with these keys:
    - title: Professional product name
    - description: Engaging description with style notes
    - brand: Found on tag
    - size: Found on tag
    - material: From care label
    - tags: List of 5 styling vibes (e.g. 'vintage', 'dark academia')
    - price: Suggested resale price based on brand/condition
    """
    
    # Construct parts for the model
    contents = [prompt]
    for path in gcs_paths:
        contents.append({"mime_type": "image/jpeg", "data": bucket.blob(path).download_as_bytes()})
    
    response = model.generate_content(contents)
    # Strip any markdown formatting Gemini might add
    clean_json = response.text.replace('```json', '').replace('```', '').strip()
    return json.loads(clean_json)

# --- API Endpoints ---

@app.post("/list-item/{folder_name}")
async def list_item(folder_name: str, x_api_key: str = Header(None)):
    # 1. Simple Auth Check
    if x_api_key != API_AUTH_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    try:
        # 2. Get Blobs from GCS Folder
        prefix = f"uploads/{folder_name}/"
        blobs = list(bucket.list_blobs(prefix=prefix))
        
        if not blobs:
            raise HTTPException(status_code=404, detail="No images found in that folder.")

        image_paths = [b.name for b in blobs if b.name.lower().endswith(('.jpg', '.jpeg', '.png'))]

        # 3. Analyze with AI
        data = await analyze_images_via_vlm(image_paths)

        # 4. Create Shopify Product
        new_product = shopify.Product()
        new_product.title = data['title']
        new_product.body_html = f"<p>{data['description']}</p><p><b>Material:</b> {data['material']}</p>"
        new_product.vendor = data['brand']
        new_product.tags = ",".join(data['tags'])
        new_product.status = "draft"
        
        # Add Price and Size
        variant = shopify.Variant({'price': data['price'], 'option1': data['size']})
        new_product.variants = [variant]
        
        # Attach Signed URLs for Shopify to pull
        new_product.images = [{"src": generate_signed_url(path)} for path in image_paths]
        
        if new_product.save():
            # 5. Notify Success
            msg = f"✅ Published: {data['title']} ({data['brand']}) as a draft."
            send_pushover(msg)
            return {"status": "success", "product_id": new_product.id}
        
        raise Exception("Failed to save to Shopify")

    except Exception as e:
        error_msg = f"❌ Error listing {folder_name}: {str(e)}"
        send_pushover(error_msg)
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
