import requests
import json

TELEGRAM_BOT_TOKEN = "your telegram bot token here"
WEBHOOK_URL = "https://yourdomain.com/telegram_webhook"

def set_webhook(token, webhook_url):
    telegram_api_url = f"https://api.telegram.org/bot{token}/setWebhook"

    payload = {
        "url": webhook_url
    }

    response = requests.post(telegram_api_url, json=payload)
    print(json.dumps(response.json(), indent=4))

if __name__ == "__main__":
    set_webhook(TELEGRAM_BOT_TOKEN, WEBHOOK_URL)
