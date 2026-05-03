import os
import json
import traceback
import shopify
import requests
from urllib.parse import urlparse
from datetime import timedelta
from typing import List
import io
from PIL import Image

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
Analyze these clothing images (front, back, and tags).

Return exactly one valid JSON object with exactly these keys (no extras):
- title: Professional product name including size.
- description: Shopify-ready item description using HTML tags only for formatting. Do not use Markdown. Maximum 2 sentences
- brand: Found on tag.
- size: Found on tag.
- measurements: based on size. use html tags if needed.
- material: From care label.
- fit_and_features: brief and use html tags if needed.
- style_notes: brief and use html tags if needed.
- tags: Array of 5 styling vibes (for example: "vintage", "dark academia").
- price: Suggested resale price based on brand and condition.
- retail: Retail price of the item.

Output requirements:
- Return JSON only (no prose, no code fences).
- Ensure the JSON is parseable.
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

def ensure_image_size_limit(blob_name: str, max_megapixels: float = 19.5) -> None:
    """
    Downloads image from GCS, strips all hidden metadata (including MPO formats),
    resizes if it exceeds max_megapixels, and overwrites the blob in GCS.
    """
    blob = bucket.blob(blob_name)
    image_data = blob.download_as_bytes()
    
    with Image.open(io.BytesIO(image_data)) as img:
        width, height = img.size
        megapixels = (width * height) / 1_000_000
        
        # Unconditionally create a brand new clean image to strip ALL hidden metadata/MPO layers
        clean_img = Image.new("RGB", img.size)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            clean_img.paste(img.convert("RGBA"), mask=img.convert("RGBA"))
        else:
            clean_img.paste(img)
            
        target_img = clean_img
        
        if megapixels > max_megapixels:
            scale_factor = (max_megapixels / megapixels) ** 0.5
            new_width = int(width * scale_factor)
            new_height = int(height * scale_factor)
            print(f"Resizing {blob_name} from {width}x{height} ({megapixels:.1f}MP) to {new_width}x{new_height}")
            target_img = clean_img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        else:
            print(f"Cleaning metadata from {blob_name} to ensure pure JPEG")
            
        output_buffer = io.BytesIO()
        target_img.save(output_buffer, format="JPEG", quality=85)
        
        # Upload back to GCS
        blob.upload_from_string(output_buffer.getvalue(), content_type="image/jpeg")
        print(f"✅ Successfully cleaned and updated {blob_name} in GCS.")

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

def generate_signed_url(blob_name: str, bust_cache: bool = False):
    """Generates a 15-minute temporary link for Shopify to fetch the image."""
    blob = bucket.blob(blob_name)
    kwargs = {
        "version": "v4",
        "expiration": timedelta(minutes=15),
        "method": "GET",
    }
    if bust_cache:
        import time
        kwargs["query_parameters"] = {"_bust": str(int(time.time()))}
        
    return blob.generate_signed_url(**kwargs)

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

def get_video_paths_for_folder(folder_name: str) -> List[str]:
    """Returns video blob paths for a given top-level folder."""
    prefix = f"{folder_name}/"
    blobs = list(bucket.list_blobs(prefix=prefix))
    return [b.name for b in blobs if b.name.lower().endswith((".mov",))]

def upload_videos_to_shopify(product_id: int, video_paths: List[str]) -> None:
    """Uploads GCS-hosted videos as Shopify product media via GraphQL."""
    if not video_paths:
        return

    access_token = fetch_shopify_access_token()
    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    graphql_url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    product_gid = f"gid://shopify/Product/{product_id}"

    media_inputs = []
    for path in video_paths:
        media_inputs.append(
            {
                "mediaContentType": "VIDEO",
                "originalSource": generate_signed_url(path),
            }
        )

    print(
        "Uploading Shopify videos with sources: "
        + json.dumps(
            [
                {"blob_path": path, "originalSource": media_inputs[idx]["originalSource"]}
                for idx, path in enumerate(video_paths)
            ],
            ensure_ascii=True,
        )
    )

    mutation = """
    mutation productCreateMedia($media: [CreateMediaInput!]!, $productId: ID!) {
      productCreateMedia(media: $media, productId: $productId) {
        media {
          alt
          mediaContentType
          status
        }
        mediaUserErrors {
          field
          message
        }
        product {
          id
        }
      }
    }
    """

    response = requests.post(
        graphql_url,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
        json={
            "query": mutation,
            "variables": {
                "media": media_inputs,
                "productId": product_gid,
            },
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    errors = payload.get("errors")
    if errors:
        print(f"Shopify GraphQL top-level errors: {json.dumps(errors, ensure_ascii=True)}")
        print(f"Shopify raw productCreateMedia payload: {json.dumps(payload, ensure_ascii=True)}")
        raise ValueError(f"Shopify GraphQL errors: {errors}")

    media_result = payload.get("data", {}).get("productCreateMedia", {})
    media_user_errors = media_result.get("mediaUserErrors", [])
    if media_user_errors:
        print(f"Shopify mediaUserErrors: {json.dumps(media_user_errors, ensure_ascii=True)}")
        print(
            "Shopify productCreateMedia debug context: "
            + json.dumps(
                {
                    "product_gid": product_gid,
                    "video_paths": video_paths,
                    "media_inputs": media_inputs,
                    "media_response": media_result.get("media", []),
                },
                ensure_ascii=True,
            )
        )
        raise ValueError(f"Shopify media user errors: {media_user_errors}")

def publish_product_to_all_channels(product_id: int) -> int:
    """Publishes a product to all available Shopify sales channels."""
    access_token = fetch_shopify_access_token()
    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    graphql_url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    product_gid = f"gid://shopify/Product/{product_id}"

    publications_query = """
    query GetPublications {
      publications(first: 250) {
        nodes {
          id
          name
        }
      }
    }
    """
    query_response = requests.post(
        graphql_url,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
        json={"query": publications_query},
        timeout=30,
    )
    query_response.raise_for_status()
    query_payload = query_response.json()
    if query_payload.get("errors"):
        raise ValueError(f"Shopify publication query errors: {query_payload['errors']}")

    publications = query_payload.get("data", {}).get("publications", {}).get("nodes", [])
    publication_ids = [publication.get("id") for publication in publications if publication.get("id")]
    if not publication_ids:
        print("No Shopify publications found; skipping channel publishing.")
        return 0

    publish_mutation = """
    mutation PublishToChannels($id: ID!, $input: [PublicationInput!]!) {
      publishablePublish(id: $id, input: $input) {
        publishable {
          availablePublicationsCount {
            count
          }
          resourcePublicationsCount {
            count
          }
        }
        shop {
          id
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    publish_input = [{"publicationId": publication_id} for publication_id in publication_ids]
    publish_response = requests.post(
        graphql_url,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
        json={
            "query": publish_mutation,
            "variables": {
                "id": product_gid,
                "input": publish_input,
            },
        },
        timeout=30,
    )
    publish_response.raise_for_status()
    publish_payload = publish_response.json()
    if publish_payload.get("errors"):
        raise ValueError(f"Shopify publish mutation errors: {publish_payload['errors']}")

    user_errors = (
        publish_payload.get("data", {})
        .get("publishablePublish", {})
        .get("userErrors", [])
    )
    if user_errors:
        raise ValueError(f"Shopify publish user errors: {user_errors}")

    return len(publication_ids)

def set_inventory_quantity(inventory_item_id: int, quantity: int = 1) -> None:
    """Sets the available inventory quantity using the inventorySetQuantities GraphQL mutation."""
    access_token = fetch_shopify_access_token()
    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    graphql_url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    
    # Fetch first active location using python shopify API
    locations = shopify.Location.find()
    if not locations:
        print("No locations found to set inventory.")
        return
        
    location_gid = f"gid://shopify/Location/{locations[0].id}"
    inventory_item_gid = f"gid://shopify/InventoryItem/{inventory_item_id}"
    
    import uuid
    idempotency_key = str(uuid.uuid4())
    
    mutation = f"""
    mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {{
      inventorySetQuantities(input: $input) @idempotent(key: "{idempotency_key}") {{
        userErrors {{
          field
          message
        }}
      }}
    }}
    """
    
    variables = {
        "input": {
            "name": "available",
            "reason": "correction",
            "quantities": [
                {
                    "inventoryItemId": inventory_item_gid,
                    "locationId": location_gid,
                    "quantity": quantity,
                    "changeFromQuantity": 0
                }
            ]
        }
    }
    
    response = requests.post(
        graphql_url,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
        json={"query": mutation, "variables": variables},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    
    if payload.get("errors"):
        raise ValueError(f"GraphQL errors setting inventory: {payload['errors']}")
        
    user_errors = payload.get("data", {}).get("inventorySetQuantities", {}).get("userErrors", [])
    if user_errors:
        raise ValueError(f"Inventory user errors: {user_errors}")

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
    video_paths = get_video_paths_for_folder(folder_name)

    print(f"Found {len(image_paths)} images for folder '{folder_name}': {image_paths}")
    data = await analyze_images_via_vlm(image_paths, generate_dummy=False)

    activate_shopify_session_with_fresh_token()
    new_product = shopify.Product()
    new_product.title = data["title"]
    body_sections = [
        f"<div>{data['description']}</div>",
        f"<p><strong>Size:</strong> {data['size']}</p>",
        f"<p><strong>Measurements:</strong> {data['measurements']}</p>",
        f"<p><strong>Material:</strong> {data['material']}</p>",
    ]
    
    if data.get("retail"):
        body_sections.append(f"<p><strong>Retails for:</strong> {data['retail']}</p>")
        
    body_sections.extend([
        f"<div><strong>Fit & Features:</strong> {data['fit_and_features']}</div>",
        f"<div><strong>Style Notes:</strong> {data['style_notes']}</div>",
        f"<div class='usually-ships'>Usually ships within 24 hours.</div>"
    ])
    new_product.body_html = "".join(body_sections)
    new_product.vendor = data["brand"]
    new_product.tags = ",".join(data["tags"])
    new_product.status = "draft"
    new_product.options = [{"name": "Size"}]

    variant = shopify.Variant(
        {
            "price": data["price"],
            "option1": data["size"],
            "inventory_management": "shopify",
        }
    )
    new_product.variants = [variant]

    if not new_product.save():
        raise Exception("Failed to save to Shopify")

    # Setting inventory_quantity directly on the variant is deprecated in newer APIs,
    # so we explicitly set it using the GraphQL mutation.
    if new_product.variants and getattr(new_product.variants[0], "inventory_item_id", None):
        try:
            set_inventory_quantity(new_product.variants[0].inventory_item_id, 1)
            print("Set inventory quantity to 1 for variant.")
        except Exception as e:
            print(f"⚠️ Failed to set inventory quantity: {e}")

    # Add images sequentially after product creation
    # Attempting to add many images in the initial product.save() 
    # can cause silent failures or dropped images.
    for path in image_paths:
        try:
            ensure_image_size_limit(path)
        except Exception as e:
            print(f"⚠️ Error checking/resizing image {path}: {e}")

        img = shopify.Image()
        img.product_id = new_product.id
        
        # Pass bust_cache=True so the cache-busting timestamp is properly signed by GCS
        img.src = generate_signed_url(path, bust_cache=True)
        if not img.save():
            errors = img.errors.full_messages() if hasattr(img, "errors") else "Unknown error"
            print(f"⚠️ Failed to attach image {path} to product {new_product.id}. Errors: {errors}")
        else:
            print(f"Attached image {path} to product {new_product.id}")

    print(f"✅ Shopify save successful for folder '{folder_name}'.")
    print(f"Shopify product id: {new_product.id}")

    published_count = publish_product_to_all_channels(new_product.id)
    if published_count:
        print(f"Published product {new_product.id} to {published_count} Shopify channel(s).")

    if video_paths:
        # upload_videos_to_shopify(new_product.id, video_paths)
        # print(f"Uploaded {len(video_paths)} .mov file(s) to Shopify product media.")
        print(f"Uploading disabled for now. {len(video_paths)} .mov file(s) to Shopify product media.")

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
