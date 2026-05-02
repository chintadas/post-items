import argparse
import json
import os
import sys

import requests
from dotenv import load_dotenv


def build_body_html(product_data: dict) -> str:
    body_sections = [
        f"<div>{product_data.get('description', '')}</div>",
        f"<p><strong>Size:</strong> {product_data.get('size', '')}</p>",
        f"<p><strong>Measurements:</strong> {product_data.get('measurements', '')}</p>",
        f"<p><strong>Material:</strong> {product_data.get('material', '')}</p>",
        f"<div><strong>Fit & Features:</strong> {product_data.get('fit_and_features', '')}</div>",
        f"<div><strong>Style Notes:</strong> {product_data.get('style_notes', '')}</div>",
    ]
    return "".join(body_sections)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview LLM product JSON for a folder without posting to Shopify.",
    )
    parser.add_argument(
        "--folder",
        required=True,
        help="Top-level GCS folder name to analyze (for example: item-123).",
    )
    parser.add_argument(
        "--prompt",
        help="Prompt text to send directly.",
    )
    parser.add_argument(
        "--prompt-file",
        help="Path to a text file containing the prompt.",
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8000/preview-listing",
        help="Preview endpoint URL.",
    )
    parser.add_argument(
        "--api-key",
        help="API key for x-api-key header. Defaults to API_AUTH_KEY from environment.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--minimal",
        action="store_true",
        help="Print only product_data on successful responses.",
    )
    return parser.parse_args()


def load_prompt(args: argparse.Namespace) -> str:
    if args.prompt and args.prompt_file:
        raise ValueError("Use either --prompt or --prompt-file, not both.")
    if not args.prompt and not args.prompt_file:
        raise ValueError("Provide one of --prompt or --prompt-file.")

    if args.prompt:
        return args.prompt.strip()

    with open(args.prompt_file, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def main() -> int:
    load_dotenv()
    args = parse_args()

    try:
        prompt = load_prompt(args)
    except ValueError as exc:
        print(f"Argument error: {exc}")
        return 2

    if not prompt:
        print("Prompt cannot be empty.")
        return 2

    api_key = args.api_key or os.getenv("API_AUTH_KEY")
    if not api_key:
        print("Missing API key. Set API_AUTH_KEY in env or pass --api-key.")
        return 2

    payload = {
        "folder_name": args.folder,
        "prompt": prompt,
    }
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            args.url,
            headers=headers,
            json=payload,
            timeout=args.timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Request failed: {exc}")
        return 1

    try:
        response_json = response.json()
    except ValueError:
        print("Server returned non-JSON response:")
        print(response.text)
        return 1

    if args.minimal and response_json.get("status") == "success":
        output = response_json.get("product_data", {})
    else:
        output = response_json

    if response_json.get("status") == "success":
        product_data = response_json.get("product_data", {})
        body_html = build_body_html(product_data)
        preview_path = os.path.join(os.path.dirname(__file__), "description_preview.md")
        with open(preview_path, "w", encoding="utf-8") as handle:
            handle.write(body_html)

    print(json.dumps(output, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
