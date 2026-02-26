import requests

ZAMMAD_URL = "https://supportserviunix.zammad.com/api/v1"
ZAMMAD_TOKEN = "SHvYIyAGum3nVE_WC_9BhZuKa4zDfCSgAlums879U6HMgUOSMKsVxwoe2s4Y2Hon"

class ZammadService:

    @staticmethod
    def create_user_if_not_exists(email: str, first_name: str = "Cliente", last_name: str = ""):
        headers = {"Authorization": f"Token token={ZAMMAD_TOKEN}"}

        url_search = f"{ZAMMAD_URL}/users/search?query={email}"
        response = requests.get(url_search, headers=headers)
        if response.status_code != 200:
            raise Exception(f"Error buscando usuario: {response.text}")

        users = response.json()
        if users:
            return users[0]["id"]

        url_create = f"{ZAMMAD_URL}/users"
        payload = {
            "login": email,
            "email": email,
            "firstname": first_name,
            "lastname": last_name,
            "state": "new"
        }

        response = requests.post(url_create, json=payload, headers=headers)
        if response.status_code != 201:
            raise Exception(f"Error creando usuario: {response.text}")

        return response.json()["id"]


    @staticmethod
    def create_ticket(title: str, body: str, customer_email: str):
        user_id = ZammadService.create_user_if_not_exists(customer_email)

        url = f"{ZAMMAD_URL}/tickets"
        headers = {
            "Authorization": f"Token token={ZAMMAD_TOKEN}",
            "Content-Type": "application/json"
        }

        payload = {
            "title": title,
            "group": "Users",
            "customer_id": user_id,
            "article": {
                "subject": title,
                "body": body,
                "type": "note",
                "internal": False
            }
        }

        response = requests.post(url, json=payload, headers=headers)

        if response.status_code != 201:
            print("Error detallado:", response.text)
            raise Exception("Error creando ticket en Zammad")

        return response.json()


    @staticmethod
    def get_ticket(ticket_id: int):
        headers = {"Authorization": f"Token token={ZAMMAD_TOKEN}"}

        url = f"{ZAMMAD_URL}/tickets/{ticket_id}"

        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            print("Error consultando ticket:", response.text)
            raise Exception("Error consultando ticket en Zammad")

        return response.json()