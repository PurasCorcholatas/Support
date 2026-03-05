import requests
from xml.etree import ElementTree as ET


class ZimbraService:

    def __init__(self, url, email, password):
        self.url = url
        self.email = email
        self.password = password
        self.auth_token = None

    

    def get_user_folders(self):

        payload = {
            "Header": {
                "context": {
                    "_jsns": "urn:zimbra",
                    "authToken": self.auth_token
                }
            },
            "Body": {
                "GetFolderRequest": {
                    "_jsns": "urn:zimbraMail"
                }
            }
        }

        headers = {"Content-Type": "application/json"}

        response = requests.post(self.url, json=payload, headers=headers)
        data = response.json()

        folders = []

        try:
            root_folders = data["Body"]["GetFolderResponse"]["folder"]

            
            if isinstance(root_folders, list):
                root_folders = root_folders[0]

            def extract(folder):
                if "name" in folder:
                    folders.append(folder["name"])

                if "folder" in folder:
                    subfolders = folder["folder"]

                    if isinstance(subfolders, dict):
                        extract(subfolders)
                    else:
                        for f in subfolders:
                            extract(f)

            extract(root_folders)

        except Exception as e:
            print("Error:", e)

        return folders


    def get_last_sent_subjects(self, limit=5):

        payload = {
            "Header": {
                "context": {
                    "_jsns": "urn:zimbra",
                    "authToken": self.auth_token
                }
            },
            "Body": {  
                "SearchRequest": {
                    "_jsns": "urn:zimbraMail",
                    "query": "in:sent",
                    "types": "message",
                    "limit": limit
                }
            }
        }

        headers = {"Content-Type": "application/json"}
        response = requests.post(self.url, json=payload, headers=headers)
        data = response.json()

        subjects = []

        try:
            search = data.get("Body", {}).get("SearchResponse", {})
            msgs = search.get("m", [])

            if isinstance(msgs, dict):
                msgs = [msgs]

            for msg in msgs:
                subject = msg.get("su", "")
                if subject:
                    subjects.append(subject)

        except Exception as e:
            print("Error obteniendo enviados:", e)

        return subjects


    def get_user_signature(self):

        payload = {
            "Header": {
                "context": {
                    "_jsns": "urn:zimbra",
                    "authToken": self.auth_token
                }
            },
            "Body": {
                "GetSignaturesRequest": {
                    "_jsns": "urn:zimbraAccount"
                }
            }
        }

        headers = {"Content-Type": "application/json"}
        response = requests.post(self.url, json=payload, headers=headers)
        data = response.json()

        try:
            sig_response = data.get("Body", {}).get("GetSignaturesResponse", {})
            signatures = sig_response.get("signature", [])

            if not signatures:
                return ""

            content = signatures[0].get("content", [])

            if isinstance(content, list) and content:
                return content[0].get("_content", "")

            if isinstance(content, dict):
                return content.get("_content", "")

        except Exception as e:
            print("Error obteniendo firma:", e)

        return ""

    def get_calendar_events(self, limit=5):

        payload = {
            "Header": {
                "context": {
                    "_jsns": "urn:zimbra",
                    "authToken": self.auth_token
                }
            },
            "Body": {
                "SearchRequest": {
                    "_jsns": "urn:zimbraMail",
                    "types": "appointment",
                    "limit": limit
                }
            }
        }

        headers = {"Content-Type": "application/json"}
        response = requests.post(self.url, json=payload, headers=headers)
        data = response.json()

        events = []

        try:
            search = data.get("Body", {}).get("SearchResponse", {})
            appts = search.get("appt", [])

            if isinstance(appts, dict):
                appts = [appts]

            for appt in appts:
                subject = appt.get("su", "")
                if subject:
                    events.append(subject)

        except Exception as e:
            print("Error obteniendo citas:", e)

        return events


    def get_contacts(self, limit=10):

        payload = {
            "Header": {
                "context": {
                    "_jsns": "urn:zimbra",
                    "authToken": self.auth_token
                }
            },
            "Body": {
                "SearchRequest": {
                    "_jsns": "urn:zimbraMail",
                    "query": "in:contacts",
                    "types": "contact",
                    "limit": limit
                }
            }
        }

        headers = {"Content-Type": "application/json"}
        response = requests.post(self.url, json=payload, headers=headers)
        data = response.json()

        contacts = []

        try:
            search = data.get("Body", {}).get("SearchResponse", {})
            items = search.get("cn", [])

            if isinstance(items, dict):
                items = [items]

            for contact in items:
                attrs = contact.get("a", [])
                for attr in attrs:
                    if attr.get("n") == "email":
                        contacts.append(attr.get("_content"))

        except Exception as e:
            print("Error obteniendo contactos:", e)

        return contacts
        
    def get_filters(self):

        payload = {
            "Header": {
                "context": {
                    "_jsns": "urn:zimbra",
                    "authToken": self.auth_token
                }
            },
            "Body": {
                "GetFilterRulesRequest": {  
                    "_jsns": "urn:zimbraMail"
                }
            }
        }

        headers = {"Content-Type": "application/json"}
        response = requests.post(self.url, json=payload, headers=headers)
        data = response.json()

        filters = []

        try:
            response_body = data.get("Body", {}).get("GetFilterRulesResponse", {})
            rules = response_body.get("filterRules", [{}])

            if rules:
                rule_list = rules[0].get("filterRule", [])

                if isinstance(rule_list, dict):
                    rule_list = [rule_list]

                for rule in rule_list:
                    filters.append(rule.get("name"))

        except Exception as e:
            print("Error obteniendo filtros:", e)

        return filters



    def authenticate(self):

        payload = {
            "Body": {
                "AuthRequest": {
                    "_jsns": "urn:zimbraAccount",
                    "account": {
                        "by": "name",
                        "_content": self.email
                    },
                    "password": self.password
                }
            }
        }

        headers = {
            "Content-Type": "application/json"
        }

        response = requests.post(self.url, json=payload, headers=headers)

        print("STATUS:", response.status_code)
        print("RESPONSE:", response.text)

        data = response.json()

        try:
            self.auth_token = data["Body"]["AuthResponse"]["authToken"][0]["_content"]
            print("Autenticación exitosa")
        except Exception:
            raise Exception("Error autenticando. Revisa usuario/clave.")
        






    def get_security_data(self) -> dict:
        """
        Obtiene informacion relevante del buzon para validacion
        """

        folders = self.get_user_folders()
        sent_subjects = self.get_last_sent_subjects()
        filters = self.get_filters()
        events = self.get_calendar_events()
        contacts = self.get_contacts()
        signature = self.get_user_signature()

        return {
            "folders": folders,
            "last_sent_subjects": sent_subjects,
            "filters": filters,
            "calendar_events": events,
            "contacts": contacts,
            "signature": [signature] if signature else []
        }