import logging
import requests
from config import PUSHOVER_API_TOKEN, PUSHOVER_USER_KEY

logger = logging.getLogger(__name__)

def send_pushover(message: str):
    """Sends a push notification to your iPhone."""
    url = "https://api.pushover.net/1/messages.json"
    data = {
        "token": PUSHOVER_API_TOKEN,
        "user": PUSHOVER_USER_KEY,
        "message": message
    }
    try:
        response = requests.post(url, data=data, timeout=10)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to send Pushover notification: {e}")


