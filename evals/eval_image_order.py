"""CLI to test image ordering against GCS folders.

Examples:
  python evals/eval_image_order.py --folders gs://my-bucket/item-1 gs://my-bucket/item-2
  python evals/eval_image_order.py --folders-file evals/folders.txt --expected evals/expected.json --output evals/results.json

expected.json maps each folder (as given on the command line) to the expected
ordered list of filenames:
  {"gs://my-bucket/item-1": ["IMG_2.jpg", "IMG_1.jpg"]}
"""
import argparse
import json
import os
import sys
from typing import List, Tuple

from dotenv import load_dotenv

# Make `services` and `config` importable when run from any directory.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test image ordering for GCS folders (no Shopify).")
    parser.add_argument("--folders", nargs="*", default=[], help="gs://bucket/prefix links or bare folder names.")
    parser.add_argument("--folders-file", help="Text file with one folder per line.")
    parser.add_argument("--expected", help="JSON file mapping folder -> expected ordered filenames.")
    parser.add_argument("--prompt-file", help="Override the classification prompt with this file's contents.")
    parser.add_argument("--output", help="Write JSON results to this file.")
    return parser.parse_args()


def split_gcs_link(folder: str, default_bucket: str) -> Tuple[str, str]:
    """Returns (bucket_name, prefix) for a gs:// link or bare folder name."""
    if folder.startswith("gs://"):
        rest = folder[len("gs://"):]
        bucket_name, _, prefix = rest.partition("/")
    else:
        bucket_name, prefix = default_bucket, folder
    prefix = prefix.strip("/")
    return bucket_name, prefix + "/" if prefix else ""


def list_images(folder: str) -> Tuple[object, List[str]]:
    """Returns (bucket, sorted image blob names) without modifying any blobs."""
    from services import gcs

    bucket_name, prefix = split_gcs_link(folder, gcs.GCS_BUCKET_NAME)
    bucket = gcs.storage_client.bucket(bucket_name)
    names = [
        b.name for b in gcs.storage_client.list_blobs(bucket_name, prefix=prefix)
        if b.name.lower().endswith((".jpg", ".jpeg", ".png"))
    ]
    return bucket, sorted(names)


def run_folder(folder: str, prompt_override: str | None) -> dict:
    from services import gemini

    result = {
        "folder": folder,
        "input_order": [],
        "classifications": [],
        "sorted": [],
        "error": None,
    }
    try:
        bucket, paths = list_images(folder)
        if not paths:
            raise ValueError("No images found in folder.")
        result["input_order"] = [os.path.basename(p) for p in paths]

        # classify_images downloads via gemini.bucket; point it at this folder's bucket.
        gemini.bucket = bucket
        classifications = gemini.classify_images(paths, prompt_override)
        result["classifications"] = classifications
        ordered = gemini.order_image_paths(paths, classifications)
        result["sorted"] = [os.path.basename(p) for p in ordered]
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def first_difference(actual: List[str], expected: List[str]) -> str | None:
    for i in range(max(len(actual), len(expected))):
        a = actual[i] if i < len(actual) else None
        e = expected[i] if i < len(expected) else None
        if a != e:
            return f"position {i}: got {a}, expected {e}"
    return None


def main() -> int:
    load_dotenv()
    args = parse_args()

    folders = list(args.folders)
    if args.folders_file:
        with open(args.folders_file, "r", encoding="utf-8") as handle:
            folders.extend(line.strip() for line in handle if line.strip() and not line.startswith("#"))
    if not folders:
        print("Provide --folders and/or --folders-file.")
        return 2

    expected = {}
    if args.expected:
        with open(args.expected, "r", encoding="utf-8") as handle:
            expected = json.load(handle)

    prompt_override = None
    if args.prompt_file:
        with open(args.prompt_file, "r", encoding="utf-8") as handle:
            prompt_override = handle.read()

    results = []
    for folder in folders:
        result = run_folder(folder, prompt_override)
        if folder in expected:
            result["expected"] = expected[folder]
            result["match"] = result["error"] is None and result["sorted"] == expected[folder]
            if not result["match"] and result["error"] is None:
                result["first_difference"] = first_difference(result["sorted"], expected[folder])
        results.append(result)

    output = json.dumps(results, indent=2, ensure_ascii=True)
    print(output)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output)

    failed = any(r["error"] for r in results)
    if expected:
        checked = [r for r in results if "match" in r]
        matched = sum(1 for r in checked if r["match"])
        print(f"\n{matched}/{len(checked)} folders match expected order.", file=sys.stderr)
        failed = failed or matched != len(checked)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
