import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, Header, BackgroundTasks
from pydantic import BaseModel

from config import API_AUTH_KEY
from services.gcs import get_pending_folders
from services.notifications import send_pushover
from services.listing_service import process_folder_listing, preview_folder_listing_data

app = FastAPI(title="Snazzy Boutique Listing Agent")

class PromptExperimentRequest(BaseModel):
    folder_name: str
    prompt: str

is_processing = False

async def process_folders_in_background(folders: list[str]):
    global is_processing
    try:
        total_folders = len(folders)
        for idx, folder_name in enumerate(folders, start=1):
            try:
                await process_folder_listing(folder_name, item_index=idx, total_items=total_folders)
            except Exception as e:
                error_msg = str(e)
                logger.error(f"Error listing {folder_name}: {e}", exc_info=True)
                try:
                    send_pushover(f"❌ Error listing {folder_name}: {error_msg}")
                except Exception as pe:
                    logger.error(f"Failed to send pushover notification: {pe}", exc_info=True)
            finally:
                logger.info(f"{idx}/{len(folders)} folder '{folder_name}' processed")
    finally:
        is_processing = False

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
        logger.error(f"Error listing {folder_name}: {e}", exc_info=True)
        error_msg = str(e)
        send_pushover(f"❌ Error listing {folder_name}: {error_msg}")
        return {"status": "error", "error_msg": error_msg}

@app.post("/list-all-items")
async def list_all_items(background_tasks: BackgroundTasks, x_api_key: str = Header(None)):
    # 1. Simple Auth Check
    if x_api_key != API_AUTH_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")

    global is_processing
    if is_processing:
        return {
            "status": "already_running",
            "message": "A background task is already processing folders. Please try again later.",
            "processed": 0,
        }

    folders = get_pending_folders()
    logger.info(f"Found {len(folders)} folders to process")
    if not folders:
        return {"status": "success", "message": "No pending folders found.", "processed": 0}

    is_processing = True
    background_tasks.add_task(process_folders_in_background, folders)

    return {
        "status": "success",
        "message": f"Successfully queued up {len(folders)} folders for processing",
        "processed": len(folders),
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
        logger.error(f"Error previewing folder {payload.folder_name}: {e}", exc_info=True)
        return {"status": "error", "error_msg": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
