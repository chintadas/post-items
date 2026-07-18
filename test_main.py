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
        mock_process.assert_any_call("folder1")
        mock_process.assert_any_call("folder2")
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
