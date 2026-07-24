import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

# 1. Setup mock environment variables so config.py's validation succeeds
os.environ["GCS_BUCKET_NAME"] = "test-bucket"
os.environ["SHOPIFY_SHOP_URL"] = "test-shop.myshopify.com"
os.environ["SHOPIFY_CLIENT_ID"] = "test-client-id"
os.environ["SHOPIFY_CLIENT_SECRET"] = "test-client-secret"
os.environ["GEMINI_API_KEY"] = "test-gemini-key"
os.environ["API_AUTH_KEY"] = "super-secret-api-key"

# Now import the app and TestClient
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)
API_HEADERS = {"x-api-key": "super-secret-api-key"}
INVALID_HEADERS = {"x-api-key": "wrong-key"}


# --- Tests for /list-item/{folder_name} ---

@patch("main.process_folder_listing", new_callable=AsyncMock)
def test_list_item_success(mock_process):
    mock_process.return_value = {"status": "success", "product_id": 12345}
    
    response = client.post("/list-item/folder-abc", headers=API_HEADERS)
    
    assert response.status_code == 200
    assert response.json() == {"status": "success", "product_id": 12345}
    mock_process.assert_called_once_with("folder-abc")


def test_list_item_unauthorized():
    response = client.post("/list-item/folder-abc", headers=INVALID_HEADERS)
    assert response.status_code == 403
    assert response.json() == {"detail": "Unauthorized"}


@patch("main.send_pushover")
@patch("main.process_folder_listing", new_callable=AsyncMock)
def test_list_item_error(mock_process, mock_pushover):
    mock_process.side_effect = Exception("Shopify API Error")
    
    response = client.post("/list-item/folder-abc", headers=API_HEADERS)
    
    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert "Shopify API Error" in response.json()["error_msg"]
    mock_pushover.assert_called_once_with("❌ Error listing folder-abc: Shopify API Error")


# --- Tests for /list-all-items ---

@patch("main.process_folder_listing", new_callable=AsyncMock)
@patch("main.get_pending_folders")
def test_list_all_items_success(mock_get_folders, mock_process):
    mock_get_folders.return_value = ["folder1", "folder2"]
    # Make folder1 succeed and folder2 fail
    mock_process.side_effect = [
        {"status": "success", "product_id": 111},
        Exception("Failed to upload folder2")
    ]
    
    with patch("main.send_pushover") as mock_pushover:
        response = client.post("/list-all-items", headers=API_HEADERS)
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["processed"] == 2
        assert data["message"] == "Successfully queued up 2 folders for processing"
        
        # TestClient runs background tasks synchronously before returning response in tests
        assert mock_process.call_count == 2
        mock_process.assert_any_call("folder1", item_index=1, total_items=2)
        mock_process.assert_any_call("folder2", item_index=2, total_items=2)
        mock_pushover.assert_called_once_with("❌ Error listing folder2: Failed to upload folder2")


@patch("main.get_pending_folders")
def test_list_all_items_no_folders(mock_get_folders):
    mock_get_folders.return_value = []
    
    response = client.post("/list-all-items", headers=API_HEADERS)
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["processed"] == 0
    assert "No pending folders found." in data["message"]


def test_list_all_items_unauthorized():
    response = client.post("/list-all-items", headers=INVALID_HEADERS)
    assert response.status_code == 403
    assert response.json() == {"detail": "Unauthorized"}


def test_list_all_items_already_processing():
    import main
    main.is_processing = True
    try:
        response = client.post("/list-all-items", headers=API_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "already_running"
        assert "already processing folders" in data["message"]
    finally:
        main.is_processing = False


# --- Tests for /preview-listing ---

@patch("main.preview_folder_listing_data", new_callable=AsyncMock)
def test_preview_listing_success(mock_preview):
    mock_preview.return_value = {"title": "Preview Product", "price": 19.99}
    
    payload = {
        "folder_name": "folder-preview",
        "prompt": "Write a funny description"
    }
    response = client.post("/preview-listing", json=payload, headers=API_HEADERS)
    
    assert response.status_code == 200
    assert response.json() == {"title": "Preview Product", "price": 19.99}
    mock_preview.assert_called_once_with(folder_name="folder-preview", prompt="Write a funny description")


def test_preview_listing_unauthorized():
    payload = {
        "folder_name": "folder-preview",
        "prompt": "Write a funny description"
    }
    response = client.post("/preview-listing", json=payload, headers=INVALID_HEADERS)
    assert response.status_code == 403
    assert response.json() == {"detail": "Unauthorized"}


@patch("main.preview_folder_listing_data", new_callable=AsyncMock)
def test_preview_listing_error(mock_preview):
    mock_preview.side_effect = Exception("Gemini API Quota Exceeded")
    
    payload = {
        "folder_name": "folder-preview",
        "prompt": "Write a funny description"
    }
    response = client.post("/preview-listing", json=payload, headers=API_HEADERS)
    
    assert response.status_code == 200
    assert response.json() == {"status": "error", "error_msg": "Gemini API Quota Exceeded"}


@pytest.mark.anyio
@patch("services.listing_service.send_pushover")
@patch("services.listing_service.get_shop_domain", return_value="test-store.myshopify.com")
@patch("services.listing_service.move_folder_to_listed")
@patch("services.listing_service.publish_product_to_all_channels", return_value=1)
@patch("services.listing_service.activate_shopify_session_with_fresh_token")
@patch("services.listing_service.shopify.Image")
@patch("services.listing_service.shopify.Variant")
@patch("services.listing_service.shopify.Product")
@patch("services.listing_service.analyze_images_via_vlm", new_callable=AsyncMock)
@patch("services.listing_service.get_video_paths_for_folder", return_value=[])
@patch("services.listing_service.get_image_paths_for_folder", return_value=["img1.jpg"])
async def test_process_folder_listing_pushover_message(
    mock_images, mock_videos, mock_vlm, mock_product_cls, mock_variant_cls, mock_image_cls,
    mock_shopify_session, mock_publish, mock_move, mock_shop_domain, mock_pushover
):
    from services.listing_service import process_folder_listing
    mock_product = MagicMock()
    mock_product.id = 999
    mock_product.save.return_value = True
    mock_product.variants = []
    mock_product_cls.return_value = mock_product

    mock_vlm.return_value = {
        "title": "Vintage Silk Blouse",
        "brand": "Gucci",
        "description": "A beautiful blouse",
        "size": "M",
        "measurements": "20x30",
        "material": "Silk",
        "fit_and_features": "Relaxed fit",
        "style_notes": "Chic",
        "price": "99.99",
        "tags": ["vintage", "silk"],
        "product_category": "Apparel",
    }

    await process_folder_listing("test_folder", item_index=3, total_items=10)

    mock_pushover.assert_called_once()
    msg = mock_pushover.call_args[0][0]
    assert "✅ 3/10 Published: Vintage Silk Blouse as a draft." in msg

