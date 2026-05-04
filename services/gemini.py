import json
from typing import List
import google.genai as genai
from google.genai import types
from config import GEMINI_API_KEY
from services.gcs import bucket

client = genai.Client(api_key=GEMINI_API_KEY)

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
- IMPORTANT: Use raw HTML tags (e.g. <p>, <ul>, <li>). Do NOT use HTML entities or escaped characters like &lt; or &gt;.
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

