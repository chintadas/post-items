import json
from typing import List
import google.genai as genai
from google.genai import types
from config import GEMINI_API_KEY
from services.gcs import bucket

client = genai.Client(api_key=GEMINI_API_KEY)

DEFAULT_LISTING_PROMPT = """
Analyze these clothing or accessory images (front, back, tags, and branding).

Return exactly one valid JSON object with exactly these keys (no extras):
- title: Professional product name. Include the clothing size (e.g. "Size M") or shoe size (e.g. "Size US 8") in the title for clothing/shoes. For one-size accessories (like bags or hats), do not include the size in the title.
- description: Shopify-ready item description. Use simple HTML like <div> or <br> for breaks, but keep the content "plain-text friendly" (use '-' for bullets instead of <ul>). Maximum 2 sentences.
- brand: Found on tag, stamp, engraving, or logo.
- size: Sizing specification. For clothing, use standard sizes (e.g. S, M, L, XL) or numerical sizes. For shoes, use standard shoe sizes (e.g. "US 8" or "EU 39"). For one-size accessories (such as bags, hats, sunglasses, scarves), use exactly "One Size".
- measurements: Detailed flat lay measurements. For clothing, specify pit-to-pit, length, waist, inseam, etc. For bags, specify width, height, depth, and strap drop. For shoes, specify insole length, heel height, etc. Use simple <br> for line breaks.
- material: From care label, material composition, or stamps (e.g. leather, canvas, 100% cotton).
- target_gender: Female, Male, or Unisex.
- product_category: Shopify product category based on image, description and type of product. Follow the standard Shopify taxonomy breadcrumb. Examples:
  - Apparel & Accessories > Clothing > Clothing Tops
  - Apparel & Accessories > Clothing > Outerwear > Coats & Jackets
  - Apparel & Accessories > Handbags, Wallets & Cases > Handbags
  - Apparel & Accessories > Shoes
  - Apparel & Accessories > Clothing Accessories > Hats
  - Apparel & Accessories > Clothing Accessories > Sunglasses
- fit_and_features: brief, use simple '-' for bullets and <br> for breaks.
- style_notes: brief, use simple '-' for bullets and <br> for breaks.
- tags: Array of 5 styling vibes (for example: "vintage", "dark academia").
- price: Suggested resale price based on brand and condition.
- retail: Retail price of the item.

Output requirements:
- Return JSON only (no prose, no code fences).
- Ensure the JSON is parseable.
- IMPORTANT: Use simple HTML tags (e.g. <div>, <br>). Avoid complex nested tags like <ul> or <li> to ensure formatting is preserved when cross-listing to plain-text platforms like Poshmark.
"""
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
    
    response = client.models.generate_content(model="gemini-flash-latest", contents=contents)
    print(f"Raw Gemini response: {response.text}")
    # Strip any markdown formatting Gemini might add
    clean_json = response.text.replace('```json', '').replace('```', '').strip()
    try:
        return json.loads(clean_json)
    except json.JSONDecodeError:
        # Return the model's raw text upstream as the error message source.
        raise ValueError(response.text)

