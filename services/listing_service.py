import logging
import shopify

from config import SHOPIFY_SHOP_URL
from services.gcs import (
    get_image_paths_for_folder,
    get_video_paths_for_folder,
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
    update_product_category,
    set_category_metafields,
)
from services.notifications import send_pushover

logger = logging.getLogger(__name__)

async def process_folder_listing(folder_name: str, item_index: int = 1, total_items: int = 1) -> dict:
    """Lists one folder to Shopify and returns operation metadata."""
    image_paths = get_image_paths_for_folder(folder_name)
    if not image_paths:
        raise ValueError("No images found in that folder.")
    video_paths = get_video_paths_for_folder(folder_name)

    logger.info(f"Found {len(image_paths)} images for folder '{folder_name}': {image_paths}")
    data = await analyze_images_via_vlm(image_paths, generate_dummy=False)

    activate_shopify_session_with_fresh_token()
    new_product = shopify.Product()
    new_product.title = data["title"]
    body_sections = [
        f"<div>{data['description']}</div>",
        f"<p><strong>Size:</strong> {data['size']}</p>",
        f"<p><strong>Approximate Measurements:</strong> {data['measurements']}</p>",
        f"<p><strong>Material:</strong> {data['material']}</p>",
    ]
    
    body_sections.extend([
        f"<div><strong>Fit & Features:</strong> {data['fit_and_features']}</div>",
        f"<div><strong>Style Notes:</strong> {data['style_notes']}</div>",
        f"<div class='usually-ships'>Usually ships within 24 hours.</div>"
    ])
    if data.get("retail"):
        body_sections.append(f"<p><strong>Retails for:</strong> {data['retail']}</p>")
    new_product.body_html = "\n\n".join(body_sections)
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
            logger.info("Set inventory quantity to 1 for variant.")
        except Exception as e:
            logger.warning(f"Failed to set inventory quantity: {e}")

    # Set the Shopify product category (standardized taxonomy)
        try:
            update_product_category(new_product.id, data["product_category"])
            # After category is set, we can set the specific category metafields
            set_category_metafields(
                new_product.id,
                data.get("target_gender"),
                data.get("size"),
                data["product_category"],
                data.get("retail")
            )
        except Exception as e:
            logger.warning(f"Failed to set Shopify product category or metafields: {e}")

    # Add images sequentially after product creation
    # Attempting to add many images in the initial product.save() 
    # can cause silent failures or dropped images.
    for path in image_paths:
        try:
            ensure_image_size_limit(path)
        except Exception as e:
            logger.warning(f"Error checking/resizing image {path}: {e}")

        img = shopify.Image()
        img.product_id = new_product.id
        
        # Pass bust_cache=True so the cache-busting timestamp is properly signed by GCS
        img.src = generate_signed_url(path, bust_cache=True)
        if not img.save():
            errors = img.errors.full_messages() if hasattr(img, "errors") else "Unknown error"
            logger.warning(f"Failed to attach image {path} to product {new_product.id}. Errors: {errors}")
        else:
            logger.info(f"Attached image {path} to product {new_product.id}")

    logger.info(f"Shopify save successful for folder '{folder_name}'.")
    logger.info(f"Shopify product id: {new_product.id}")

    published_count = publish_product_to_all_channels(new_product.id)
    if published_count:
        logger.info(f"Published product {new_product.id} to {published_count} Shopify channel(s).")

    if video_paths:
        try:
            logger.info(f"Attempting to upload {len(video_paths)} video(s) to Shopify...")
            upload_videos_to_shopify(new_product.id, video_paths)
            logger.info(f"Successfully uploaded {len(video_paths)} video(s) to Shopify product media.")
        except Exception as e:
            logger.error(f"Failed to upload videos to Shopify: {e}", exc_info=True)

    move_folder_to_listed(folder_name)
    shop_domain = get_shop_domain(SHOPIFY_SHOP_URL)
    store_name = shop_domain.split(".")[0]
    admin_url = f"https://admin.shopify.com/store/{store_name}/products/{new_product.id}"
    msg = f"✅ {item_index}/{total_items} Published: {data['title']} ({data['brand']}) as a draft.\n{admin_url}"
    send_pushover(msg)
    return {"status": "success", "product_id": new_product.id, "title": data["title"]}

async def preview_folder_listing_data(folder_name: str, prompt: str) -> dict:
    """Runs prompt + images through Gemini and returns parsed listing JSON only."""
    image_paths = get_image_paths_for_folder(folder_name)
    if not image_paths:
        raise ValueError("No images found in that folder.")

    logger.info(f"Previewing LLM output for folder '{folder_name}' with {len(image_paths)} images.")
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
