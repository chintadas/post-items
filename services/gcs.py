import io
import os
from typing import List
from datetime import timedelta
from PIL import Image
from google.cloud import storage
from config import GCS_BUCKET_NAME

storage_client = storage.Client()
bucket = storage_client.bucket(GCS_BUCKET_NAME)

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
