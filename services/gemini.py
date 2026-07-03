import json
import logging
from typing import List
import google.genai as genai
from google.genai import types
from config import GEMINI_API_KEY
from services.gcs import bucket

logger = logging.getLogger(__name__)

client = genai.Client(api_key=GEMINI_API_KEY)

import os

def load_default_prompt() -> str:
    """Loads the system prompt from prompt.txt as single source of truth, with robust inline fallback."""
    prompt_path = os.path.join(os.path.dirname(__file__), "..", "prompt.txt")
    if os.path.exists(prompt_path):
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            logger.warning(f"Failed to read prompt.txt: {e}. Using inline fallback.")
    
    # Inline fallback
    return (
        "Analyze these clothing or accessory images (front, back, tags, and branding).\n\n"
        "Return exactly one valid JSON object with exactly these keys (no extras):\n"
        "- title: Professional product name. Include the clothing size (e.g. \"Size M\") or shoe size (e.g. \"Size US 8\") in the title for clothing/shoes. For one-size accessories (like bags or hats), do not include the size in the title.\n"
        "- description: Shopify-ready item description. Use simple HTML like <div> or <br> for breaks, but keep the content \"plain-text friendly\" (use '-' for bullets instead of <ul>). Maximum 2 sentences.\n"
        "- brand: Found on tag, stamp, engraving, or logo.\n"
        "- size: Sizing specification. For clothing, use standard sizes (e.g. S, M, L, XL) or numerical sizes. For shoes, use standard shoe sizes (e.g. \"US 8\" or \"EU 39\"). For one-size accessories (such as bags, hats, sunglasses, scarves), use exactly \"One Size\".\n"
        "- measurements: Detailed flat lay measurements. For clothing, always specify length (including total length/outseam for pants, trousers, shorts, and skirts) in addition to other relevant measurements like pit-to-pit, waist, inseam, and rise. For bags, specify width, height, depth, and strap drop. For shoes, specify insole length, heel height, etc. Use simple <br> for line breaks.\n"
        "- material: From care label, material composition, or stamps (e.g. leather, canvas, 100% cotton).\n"
        "- target_gender: Female, Male, or Unisex.\n"
        "- product_category: Shopify product category based on image, description and type of product. Follow the standard Shopify taxonomy breadcrumb. Examples:\n"
        "  - Apparel & Accessories > Clothing > Clothing Tops\n"
        "  - Apparel & Accessories > Clothing > Outerwear > Coats & Jackets\n"
        "  - Apparel & Accessories > Handbags, Wallets & Cases > Handbags\n"
        "  - Apparel & Accessories > Shoes\n"
        "  - Apparel & Accessories > Clothing Accessories > Hats\n"
        "  - Apparel & Accessories > Clothing Accessories > Sunglasses\n"
        "- fit_and_features: brief, use simple '-' for bullets and <br> for breaks.\n"
        "- style_notes: brief, use simple '-' for bullets and <br> for breaks.\n"
        "- tags: Array of 5 styling vibes (for example: \"vintage\", \"dark academia\").\n"
        "- price: Suggested resale price based on brand and condition.\n"
        "- retail: Retail price of the item.\n\n"
        "Output requirements:\n"
        "- Return JSON only (no prose, no code fences).\n"
        "- Ensure the JSON is parseable.\n"
        "- IMPORTANT: Use simple HTML tags (e.g. <div>, <br>). Avoid complex nested tags like <ul> or <li> to ensure formatting is preserved when cross-listing to plain-text platforms like Poshmark."
    )

DEFAULT_LISTING_PROMPT = load_default_prompt()
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
        logger.info(f"Added image for analysis: {path}")
    
    response = client.models.generate_content(model="gemini-flash-latest", contents=contents)
    logger.info(f"Raw Gemini response: {response.text}")
    # Strip any markdown formatting Gemini might add
    clean_json = response.text.replace('```json', '').replace('```', '').strip()
    try:
        return json.loads(clean_json)
    except json.JSONDecodeError:
        # Return the model's raw text upstream as the error message source.
        raise ValueError(response.text)

