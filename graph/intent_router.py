

def detect_intent(text:str):
    
    text = text.lower()
    
    if any(word in text for word in ["hola","buenas","hello"]):
        return "greeting_flow"
    
    if any(word in text for word in ["contraseña","password","clave"]):
        return "password_flow"
    
    if any(word in text for word in ["ticket","soporte","problema"]):
        return "diagnosis_flow"
    
    if any(word in text for word in ["humano","asesor","persona"]):
        return "human"
    
    return "chat_general"