import os
from dotenv import load_dotenv
from zammad_py import ZammadAPI
import requests

load_dotenv()

base_url = "https://serviunix-helpdesk.zammad.com"
token = os.getenv("ZAMMAD_HTTP_TOKEN")

variations = [
    base_url,
    base_url + "/",
    base_url + "/api/v1"
]

print(f"--- Probando variaciones de URL ---")

for url in variations:
    print(f"\n>> Probando con: {url}")
    try:
        client = ZammadAPI(url=url, http_token=token)
        me = client.user.me()
        print(f"   ✅ ¡ÉXITO! Conectado como: {me.get('login')}")
        print(f"   💡 LA URL CORRECTA ES: {url}")
    except Exception as e:
        print(f"   ❌ FALLÓ")
        if hasattr(e, 'response') and e.response is not None:
            print(f"      Status: {e.response.status_code}")
            print(f"      URL final intentada por la librería: {e.response.url}")
        else:
            print(f"      Error: {e}")
