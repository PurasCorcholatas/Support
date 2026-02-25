import requests
import os

ZAMMAD_URL = "https://supportserviunix.zammad.com//api/v1"
ZAMMAD_TOKEN = os.getenv("ZAMMAD_TOKEN")


class ZammadService:

    @staticmethod
    def create_ticket(title: str, body: str, customer_email: str):

        url = f"{ZAMMAD_URL}/tickets"

        headers = {
            "Authorization": f"Token token={ZAMMAD_TOKEN}",
            "Content-Type": "application/json"
        }

        payload = {
            "title": title,
            "group": "Users",
            "customer": customer_email,
            "article": {
                "subject": title,
                "body": body,
                "type": "note",
                "internal": False
            }
        }

        response = requests.post(url, json=payload, headers=headers)

        if response.status_code != 201:
            raise Exception("Error creando ticket en Zammad")

        return response.json()

    @staticmethod
    def get_ticket(ticket_id: int):

        url = f"{ZAMMAD_URL}/tickets/{ticket_id}"

        headers = {
            "Authorization": f"Token token={ZAMMAD_TOKEN}"
        }

        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            raise Exception("Error consultando ticket")

        return response.json()