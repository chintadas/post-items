import traceback
import shopify

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

from config import API_AUTH_KEY, SHOPIFY_SHOP_URL
from services.gcs import (
    get_image_paths_for_folder,
    get_video_paths_for_folder,
    get_pending_folders,
    ensure_image_size_limit,
    generate_signed_url,
    move_folder_to_listed,
)
from services.gemini import analyze_images_via_vlm
from services.shopify import (
    activate_shopify_session_with_fresh_token,
    set_inventory_quantity,
    upload_videos_to_shopify,
    publish_product_to_all_channels,
    get_shop_domain,
)
from services.notifications import send_pushover

app = FastAPI(title="Snazzy Boutique Listing Agent")

class PromptExperimentRequest(BaseModel):
    folder_name: str
    prompt: str

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
        try:
            print(f"Attempting to upload {len(video_paths)} video(s) to Shopify...")
            upload_videos_to_shopify(new_product.id, video_paths)
            print(f"✅ Successfully uploaded {len(video_paths)} video(s) to Shopify product media.")
        except Exception as e:
            print(f"⚠️ ERROR: Failed to upload videos to Shopify: {e}")
            import traceback
            traceback.print_exc()

    move_folder_to_listed(folder_name)
    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    store_name = shop_domain.split(".")[0]
    admin_url = f"https://admin.shopify.com/store/{store_name}/products/{new_product.id}"
    msg = f"✅ Published: {data['title']} ({data['brand']}) as a draft.\n{admin_url}"
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
