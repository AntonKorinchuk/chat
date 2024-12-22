import requests

from config import TELEGRAM_TOKEN, WEBHOOK_URL


def set_webhook():
    requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook")

    response = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
        json={"url": WEBHOOK_URL},
    )

    info = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getWebhookInfo")

    print("Webhook setting response:", response.json())
    print("\nWebhook info:", info.json())


if __name__ == "__main__":
    set_webhook()
