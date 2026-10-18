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
        "- description: Shopify-ready item description using HTML tags only for formatting. Do not use Markdown. Maximum 2 sentences.\n"
        "- brand: Found on tag, stamp, engraving, or logo.\n"
        "- size: Sizing specification. For clothing, use standard sizes (e.g. S, M, L, XL) or numerical sizes. For shoes, use standard shoe sizes (e.g. \"US 8\" or \"EU 39\"). For one-size accessories (such as bags, hats, sunglasses, scarves), use exactly \"One Size\".\n"
        "- measurements: Detailed flat lay measurements. For clothing, always specify length (including total length/outseam for pants, trousers, shorts, and skirts) in addition to other relevant measurements like pit-to-pit, waist, inseam, and rise. For bags, specify width, height, depth, and strap drop. For shoes, specify insole length, heel height, etc. Put each measurement on a separate line separated by <br> tags. Do not use bullet points, numbered items, or paragraph tags (<p>).\n"
        "- material: From care label, material composition, or stamps (e.g. leather, canvas, 100% cotton).\n"
        "- department: Target department. Must be exactly one of: \"Women\", \"Teens\", or \"Unisex Adults\".\n"
        "- target_gender: Female, Male, or Unisex.\n"
        "- product_category: Shopify product category based on image, description and type of product. Follow the standard Shopify taxonomy breadcrumb. NEVER use Baby & Children's or Maternity categories. Always pick the adult or Women's equivalent. Examples:\n"
        "  - Apparel & Accessories > Clothing > Clothing Tops\n"
        "  - Apparel & Accessories > Clothing > Tops > Shirts & Tops (for shirts/tops, prefer a Women's or adult path)\n"
        "  - Apparel & Accessories > Clothing > Outerwear > Coats & Jackets\n"
        "  - Apparel & Accessories > Handbags, Wallets & Cases > Handbags\n"
        "  - Apparel & Accessories > Shoes\n"
        "  - Apparel & Accessories > Clothing Accessories > Hats\n"
        "  - Apparel & Accessories > Clothing Accessories > Sunglasses\n"
        "- fit_and_features: Brief. For multiple items, place each on a separate line using <br> tags. Do not use bullet points, numbered items, or paragraph tags (<p>).\n"
        "- style_notes: Brief. For multiple items, place each on a separate line using <br> tags. Do not use bullet points, numbered items, or paragraph tags (<p>).\n"
        "- tags: Array of 5 styling vibes (for example: \"vintage\", \"dark academia\").\n"
        "- price: Suggested resale price based on brand and condition.\n"
        "- retail: Retail price of the item.\n\n"
        "Output requirements:\n"
        "- Return JSON only (no prose, no code fences).\n"
        "- Ensure the JSON is parseable.\n"
        "- For fields with multiple items or multiple lines (such as measurements, fit_and_features, style_notes), format them simply on separate lines using <br> tags. Do NOT use bullet points (no <ul>, <li>, or bullet characters), numbered lists, or paragraph tags (<p>).\n"
        "- IMPORTANT: Use raw HTML tags (e.g. <br>). Do NOT use HTML entities or escaped characters like &lt; or &gt;."
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


ORDER_PROMPT = (
    "Classify each of the numbered images (in the order given, starting at 0) for an online clothing listing.\n"
    "For each image return a category and a view:\n"
    "- category: \"model\" (item worn by a real person, full or three-quarter body views; NOT a mannequin or dress form), "
    "\"mannequin\" (item worn on a mannequin/dress form), \"flat\" (item laid flat or hung, no person or mannequin, "
    "not a close-up), \"tag\" (close-up of a clothing tag, label, care label, or brand marking), or \"other\" "
    "(details, close-ups of the item on a model or mannequin, flaws, textures, anything else).\n"
    "- view: \"front\", \"side\", \"back\", or \"other\".\n"
    "Return JSON only, no prose: a list of objects like "
    "[{\"index\": 0, \"category\": \"mannequin\", \"view\": \"front\"}, ...] with one entry per image."
)

_VIEW_RANK = {"front": 0, "side": 1, "back": 2}


def order_image_paths(gcs_paths: List[str], classifications: list) -> List[str]:
    """Orders paths: model front/side/back, then mannequin front/side/back, then (only if there
    are no model or mannequin photos) flat front/back, then tags, then the rest.

    Sorting is stable, so original order is kept within each group.
    """
    info = {}
    for item in classifications:
        try:
            info[int(item["index"])] = (str(item.get("category", "")).lower(), str(item.get("view", "")).lower())
        except (KeyError, TypeError, ValueError):
            continue

    categories = {info.get(i, ("", ""))[0] for i in range(len(gcs_paths))}
    has_person_or_mannequin = bool(categories & {"model", "mannequin"})

    def key(i: int):
        category, view = info.get(i, ("other", "other"))
        if category == "model" and view in _VIEW_RANK:
            return (0, _VIEW_RANK[view], i)
        if category == "mannequin" and view in _VIEW_RANK:
            return (1, _VIEW_RANK[view], i)
        if category == "flat" and not has_person_or_mannequin and view in ("front", "back"):
            # Only front and back lead for flat photos (no sides).
            return (2, _VIEW_RANK[view], i)
        if category == "tag":
            return (3, 0, i)
        if category == "flat" and view == "side" and not has_person_or_mannequin:
            return (5, 0, i)
        return (4, 0, i)

    return [gcs_paths[i] for i in sorted(range(len(gcs_paths)), key=key)]


def classify_images(gcs_paths: List[str], prompt_override: str | None = None) -> list:
    """Sends the images to Gemini and returns [{index, category, view}, ...]. Raises on failure."""
    contents = [prompt_override.strip() if prompt_override else ORDER_PROMPT]
    for i, path in enumerate(gcs_paths):
        contents.append(f"Image {i}:")
        contents.append(types.Part.from_bytes(mime_type="image/jpeg", data=bucket.blob(path).download_as_bytes()))
    response = client.models.generate_content(model="gemini-flash-latest", contents=contents)
    clean_json = response.text.replace('```json', '').replace('```', '').strip()
    return json.loads(clean_json)


async def sort_images_for_posting(gcs_paths: List[str]) -> List[str]:
    """Uses Gemini to classify images and returns them in posting order.

    Falls back to the original order if classification fails.
    """
    if len(gcs_paths) < 2:
        return list(gcs_paths)
    try:
        ordered = order_image_paths(gcs_paths, classify_images(gcs_paths))
        logger.info(f"Image posting order: {ordered}")
        return ordered
    except Exception as e:
        logger.warning(f"Failed to classify images for ordering, using original order: {e}")
        return list(gcs_paths)


