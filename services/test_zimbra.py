from zimbra_service import ZimbraService

zimbra = ZimbraService(
    url="https://correo.serviunix.com/service/soap",
    email="simon.restrepo@serviunix.com",
    password="fxz6mxz5XKD6bxc-grv"
)

zimbra.authenticate()

folders = zimbra.get_user_folders()

print("CARPETAS:")
print(folders)