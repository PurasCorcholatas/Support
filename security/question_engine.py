import random 

QUESTION_BANK = {
    
    "folder_name":{
        "question": "Indicame el nombre o parte del nombre de una carpeta personalizada que hayas creado",
        "weight": 2,
        "type": "folders",
        "strength": "strong"
    },
    
    "send_email":{
        "question": "Menciona una palabra clave de uno de los últimos correos que hayas enviado.",
        "fallback": "¿Recuerdas algún tema reciente sobre el que hayas enviado un correo?",
        "weight": 2,
        "type": "last_sent_subjects",
        "strength": "strong"
    },
    
    
    "filter":{
        "question": "¿Tienes alguna regla o filtro configurado? Menciona parte del nombre o lo que hace.",
        "weight": 2,
        "type": "filters",
        "strength": "strong"
    },
    
    
    
    "meeting":{
        "question": "Menciona una palabra clave del título de alguna cita que hayas creado recientemente.",
        "fallback": "¿Tienes alguna reunión recurrente? Menciona parte del nombre.",
        "weight": 1,
        "type": "calendar_events",
        "strength": "medium"
    },
    
    
    
    "contact":{
        "question": "Menciona el nombre de algún contacto que hayas guardado manualmente.",
        "weight": 1,
        "type": "contacts",
        "strength": "medium"
    },
    
    
    "corporate_signature":{
        "question": "¿Cómo inicia tu firma corporativa?",
        "weight": 1,
        "type": "signature",
        "strength": "medium"
    },
    
    
}


def select_initial_quetions():
    strong_questions = [
        key for key, value in QUESTION_BANK.items()
        if value["strength"] == "strong"
    ]
    
    medium_questions = [
        key for key , value in QUESTION_BANK.items()
        if value["strength"] == "medium"   
    ]
    
    selected = []
    
    selected += random.sample(strong_questions, 2)
    selected += random.sample(medium_questions, 1)
    
    return selected



def select_additional_question(current_questions, real_data, question_bank):

    for key, config in question_bank.items():

        if key in current_questions:
            continue

        data_type = config.get("type")
        real_values = real_data.get(data_type)

        
        if isinstance(real_values, list) and len(real_values) == 0:
            continue

        if real_values is None or real_values == "":
            continue

        return key

    return None


def get_question_text(question_key: str) -> str:
    return QUESTION_BANK[question_key]["question"]