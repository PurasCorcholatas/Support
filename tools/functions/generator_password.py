from langchain_core.tools import tool
import secrets
import string
from  datetime import datetime

PASSWORDS_GENERATES = set()
LOG_FILE = "auditoria_passwords.log"


@tool
def generator_pw(thread_id: str) -> str:
    """
    Genera una contraseña segura única.
    Registra auditoría en archivo.
    """

    caracteres = string.ascii_letters + string.digits + "!@#$%&*+-_?"
    longitud = 12

    while True:
        password = ''.join(secrets.choice(caracteres) for _ in range(longitud))

        if (
            any(c.isupper() for c in password) and
            any(c.islower() for c in password) and
            any(c.isdigit() for c in password) and
            any(c in "!@#$%&*+-_?" for c in password)
        ):
            if password not in PASSWORDS_GENERATES:
                PASSWORDS_GENERATES.add(password)

                
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(
                        f"{datetime.now()} | Usuario: {thread_id} | Password generada\n"
                    )

                return f" Su nueva contraseña segura es:\n\n{password}\n\nGuárdela en un lugar seguro."
            
            
            