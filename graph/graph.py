from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_openai import ChatOpenAI


from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage, AIMessage
from langgraph.checkpoint.memory import MemorySaver
from typing import List, Literal, Annotated, Optional
from typing_extensions import TypedDict
from dotenv import load_dotenv

from sqlalchemy import select, insert, update
import re

from config.db import SessionLocal
from models.users import users
from models.conversations import conversation
from models.messages import messages
from models.tickets import tickets
from services.mcp_client import get_mcp_tools


from security.profile_engine import (
    build_account_profile,
    generate_adaptive_questions
)
from security.question_engine import (
    QUESTION_BANK,
    select_initial_quetions,
    select_additional_question,
    get_question_text
)

from security.scoring_engine import calculate_security_score

from services.zimbra_service import ZimbraService
from services.zammad_services import ZammadService

import smtplib
from email.mime.text import MIMEText
import os

load_dotenv()

memory_saver = MemorySaver()

async def init_llm_with_tools():

    tools = await get_mcp_tools()

    llm_with_tools = llm.bind_tools(tools)

    return llm_with_tools


llm = ChatOpenAI(
    model="gpt-4.1",
    temperature=0,
)





class State(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], add_messages]
    intent: Literal[
        "chat_general",
        "crear_ticket",
        "human",
        "silence",
        "estado_ticket",
        "password_flow",
        "greeting_flow"]
    
    
    greeting_step: Optional[Literal[
        "start",
        "wait_user_reply",
        "confirm_branch",
        "waiting_problem"
    ]]
    
    
    branch: Optional[str]
    
    conversation_status: Optional[Literal[
        "bot_active",
        "human_active",
        "closed"
    ]]
    
    diagnosis_step: Optional[int]
    diagnosis_history: Optional[List[str]]
    diagnosis_summary: Optional[str]
    
    
    support_option_step: Optional[Literal[
        "offer_options",

    ]]
    
    password_step: Optional[Literal[
            "confirm_owner",
            "ask_email",
            "ask_recent_change",
            "ask_send_email",
            "dynamic_question",
            "waiting_confirmation",
            "confirmation_continue",
            "done"
        ]
    ]
    security_risk: Optional[int]
    real_data: Optional[dict]
    security_questions: Optional[List[str]]
    user_answers: Optional[dict]
    
    
   
    thread_id: Optional[str]
    
    ticket_step: Optional[Literal[
        "ask_user_info",
        "ask_title", 
        "ask_description",
        "ask_email",
        "done"
        ]
    ]

    

    ticket_status_step: Optional[Literal["ask_id"]]
    
    description: Optional[str]
    current_question_index: Optional[int]
    email: Optional[str]
    validation_summary: Optional[dict]

   
def langgraph(mensaje: str, thread_id: str):

    db = SessionLocal()

    state: State = {
        "messages": [HumanMessage(content=mensaje)],
        "intent": "chat_general",
        "thread_id": thread_id,
        "conversation_status": "bot_active"
    }

    result = graph.invoke(
        state,
        config={"configurable": {"thread_id": thread_id}}
    )

    final_message = result["messages"][-1].content

    
    stmt_user = select(users).where(users.c.phone_number == thread_id)
    user = db.execute(stmt_user).fetchone()

    if user:
        stmt_conv = select(conversation).where(
            conversation.c.users == user.id,
            conversation.c.status == "open"
        )
        conv = db.execute(stmt_conv).fetchone()

        if conv:
            conversation_id = conv.id

            db.execute(
                insert(messages).values(
                    conversation_id=conversation_id,
                    sender="user",
                    message_text=mensaje,
                    company=user.company
                )
            )

            db.execute(
                insert(messages).values(
                    conversation_id=conversation_id,
                    sender="bot",
                    message_text=final_message,
                    company=user.company
                )
            )

        db.commit()
        
    return final_message




def extract_sede(user_message):

    response = llm.invoke([
        SystemMessage(
            content="""
    Extrae únicamente el nombre de la sede mencionada por el usuario.

    Devuelve SOLO el nombre de la sede.
    """
        ),
        HumanMessage(content=user_message)
    ])

    sede = str(response.content).lower()

    sede = re.sub(r"[^a-záéíóúñ\s]", "", sede).strip()

    return sede


def greeting_flow(state: State):

    db = SessionLocal()

    thread_id = state.get("thread_id")
    step = state.get("greeting_step", "start")

    messages_state = state.get("messages", [])
    last_user_message = str(messages_state[-1].content).lower().strip()

    stmt_user = select(users).where(users.c.phone_number == thread_id)
    user = db.execute(stmt_user).fetchone()

    name = user.name if user and user.name else ""
    sede = user.sede if user and user.sede else None


    
    if step == "start":

        prompt = f"""
        El usuario se llama {name}.

        El usuario acaba de decir:
        "{last_user_message}"

        Respóndele el saludo de forma natural y pregúntale cómo está.
        No preguntes aún por la sede.
        """

        return {
            "greeting_step": "wait_user_reply",
            "messages": [conversational_response(prompt)]
        }


    
    elif step == "wait_user_reply":

        if sede:

            prompt = f"""
            El usuario respondió: "{last_user_message}"

            Respóndele de forma amable y pregúntale si se encuentra en la sede {sede}.
            """

            return {
                "greeting_step": "confirm_branch",
                "messages": [conversational_response(prompt)]
            }

        else:

            prompt = f"""
            El usuario respondió: "{last_user_message}"

            Respóndele de forma natural y pregúntale desde qué sede se comunica.
            """

            return {
                "greeting_step": "confirm_branch",
                "messages": [conversational_response(prompt)]
            }


    
    elif step == "confirm_branch":

        if sede and last_user_message in ["si","sí","correcto"]:

            prompt = f"""
            El usuario confirmó que está en la sede {sede}.

            Respóndele de forma natural y pregúntale en qué problema necesita ayuda.
            """

            return {
                "greeting_step": "waiting_problem",
                "messages":[conversational_response(prompt)]
            }


        if "no" in last_user_message:

            sede_detected = extract_sede(last_user_message)

            if sede_detected:

                db.execute(
                    update(users)
                    .where(users.c.phone_number == thread_id)
                    .values(sede=sede_detected)
                )
                db.commit()

                prompt = f"""
                Entendí que ahora estás en la sede {sede_detected}.

                Confirma la sede y pregúntale en qué problema necesita ayuda.
                """

                return {
                    "greeting_step": "waiting_problem",
                    "messages":[conversational_response(prompt)]
                }


            prompt = """
            El usuario dijo que no está en la sede registrada.

            Pregúntale desde qué sede se comunica.
            """

            return {
                "greeting_step": "confirm_branch",
                "messages":[conversational_response(prompt)]
            }


        
    sede_detected = extract_sede(last_user_message)

    if user:

        db.execute(
            update(users)
            .where(users.c.phone_number == thread_id)
            .values(sede=sede_detected)
        )

        db.commit()


    prompt = f"""
    El usuario indicó que ahora está en la sede {sede_detected}.

    No le digas que actualizaste la base de datos.
    Solo responde de forma natural y pregúntale en qué problema necesita ayuda.
    """

    return {
            "greeting_step": "waiting_problem",
            "messages": [conversational_response(prompt)]
    }

    return {}

        
    
    
def conversational_response(prompt):

    response = llm.invoke([
        SystemMessage(
            content="""
Eres un agente de soporte técnico humano.

Hablas de forma:
- natural
- conversacional
- amable

Nunca suenes como un bot.

Responde corto y natural.
"""
        ),
        HumanMessage(content=prompt)
    ])

    return AIMessage(content=response.content)
    
def diagnosis_flow(state:State):
    
    step = state.get("diagnosis_step") or 0
    history = state.get("diagnosis_history") or []
    messages_state = state.get("messages", [])
    last_user_message = str(messages_state[-1].content)
    
    if any(word in last_user_message.lower() for word in ["ticket", "crear_ticket"]):
        return {
            "intent": "crear_ticket",
            "diagnosis_step": None,
            "support_option_step": None
        }
        
    history_text = "\n".join(history)
        
    if step >= 3:
        severity = detected_incident_severity(history_text)
    
        return{
            "diagnosis_step": None,
            "support_option_step":"offer_options",
            "severity": severity,
            "messages":[
                AIMessage(
                    "Con lo que me cuentas ya tengo una idea del problema. \n\n"
                    "Podemos hacer dos cosas:\n"
                    "1. Crear un ticket para que soporte lo revise\n"
                    "2. Conectarte con un agente (puede tardar un poco)\n\n"
                    "¿Que prefieres?"
                )
            ]
        }
        
   
        
      
        
        
    prompt = f"""
        Eres un ingeniero senior de soporte técnico con mucha experiencia diagnosticando problemas en sistemas reales.

        Tu estilo debe ser:
        - natural
        - técnico
        - conversacional
        - como un ingeniero de soporte real hablando con un usuario

        Tu objetivo NO es resolver el problema directamente.
        Tu trabajo es INVESTIGAR el problema haciendo preguntas técnicas inteligentes.

        Debes recopilar información suficiente para que otro ingeniero pueda resolver el problema.

        

        PROBLEMA ORIGINAL (historial de la conversación)

        {history_text}

        

        ÚLTIMO MENSAJE DEL USUARIO

        {last_user_message}

        

        INSTRUCCIONES IMPORTANTES

        Analiza cuidadosamente lo que dijo el usuario.

        Si el usuario ya mencionó:
        - un error
        - un mensaje del sistema
        - un log
        - un comportamiento específico

        NO vuelvas a preguntar lo mismo.

        Si el usuario ya dio información técnica,
        haz una pregunta que profundice más en el problema.

        Piensa como un ingeniero investigando un incidente real.

        Tu pregunta debe ayudar a identificar:

        - dónde ocurre el problema
        - cuándo ocurre
        - qué sistema está involucrado
        - qué acción lo dispara
        - qué error aparece

        

        EJEMPLOS DE BUENAS PREGUNTAS

        Usuario: "sale error 500"
        Bot: "¿Ese error aparece al iniciar sesión o al cargar alguna página específica?"

        Usuario: "no responde por ssh"
        Bot: "¿El servidor responde a ping desde tu red?"

        Usuario: "sale authentication failed"
        Bot: "¿Ese error aparece al acceder al correo o al panel administrativo?"

        Usuario: "la caja no abre"
        Bot: "¿La caja muestra algún mensaje de error en el sistema POS?"

        

        REGLAS IMPORTANTES

        - Haz SOLO UNA pregunta
        - La pregunta debe ser clara y técnica
        - No hagas preguntas genéricas como "¿puedes explicar mejor?"
        - No repitas preguntas anteriores
        - Máximo 20 palabras
        - Responde SOLO con la pregunta
        """
    
    response = llm.invoke([
        SystemMessage(content="Eres un ingeniero de soporte experto y experimentado"),
            HumanMessage(content=prompt)
    ])
    
    question = str(getattr(response, "content", response)).strip()
        
    return{
        "diagnosis_step": step + 1,
        "diagnosis_history": history + [
            f"usuario: {last_user_message}",
            f"bot: {question}"
        ],
        "messages": [AIMessage(content=question)]
    }

def offer_support_options(state:State):
    
    messages_state = state.get("messages", [])
    last = str(messages_state[-1].content).lower()
    
    
    if state.get("support_option_step") != "waiting_choice":
        
        return {
            "support_option_step": "waiting_choice",
            "messages":[
                AIMessage(
                    content=
                    "Con lo que me cuentas ya tengo una idea del problema.\n\n"
                    "Podemos hacer dos cosas:\n"
                    "1. Crear un ticket para que soporte lo revise\n"
                    "2. Conectarte con un agente (puede tardar un poco)\n\n"
                    "¿Qué prefieres?"
                )
            ]
        }
        
    if "ticket" in last or "1" in last:
        return {
            "intent": "crear_ticket",
            "support_option_step": None,
            "diagnosis_step": None
        }

    if "agente" in last or "humano" in last or "2" in last:
        return {
            "intent": "human",
            "support_option_step": None,
            "diagnosis_step": None
        }

    return{
        "support_option_step": "waiting_choice",
        "messages":[
            AIMessage(
                content="Puedes elegir crear ticket o hablar con un agente"
            )
        ]
    }

def detected_incident_severity(history_text):
    
    prompt = f"""
    Eres un ingeniero senior de soporte tecnico.
    
    Analiza el incidente descrito y determina su criticidad.
    
    INCIDENTE
    {history_text}
    
    Clasifica la criticidad como una de estas tres opciones:
    
    baja
    media
    alta
    
    Guia general:
    
    alta:
    - sistemas caidos
    - servidores no responden
    - VPN caido
    - red corportativo caido
    - problemas que afectan operaciones criticas
    
    media:
    - errores en aplicaciones
    - correo no funciona
    - problemas de conexion
    - errores que afectan a algunos usuarios
    
    
    baja:
    - consultas
    - configuracion 
    - problemas menores
    - acceso o contraseña
    
    Responde SOLO con una palabra:
    
    baja 
    media 
    alta
    
    
    
    """
    
    response = llm.invoke([
            SystemMessage(
                content="Eres un experto en soport IT."
            ),
            HumanMessage(content=prompt)
            
        ])
        
    return str(response.content).strip().lower()


def router(state: State):

    
    if state.get("conversation_status") == "human_active":
        return {"intent": "silence"}

    if state.get("password_step"):
        return {"intent": "password_flow"}

    greeting_step = state.get("greeting_step")
        
    if greeting_step and greeting_step != "waiting_problem":
        return {"intent": "greeting_flow"}
    
    if greeting_step == "waiting_problem" and not state.get("diagnosis_step"):
        
        messages = state.get("messages", [])
        last_message = messages[-1] if messages else None
        last = messages[-1].content if messages else ""
        return {
            "intent": "diagnosis_flow",
            "diagnosis_step": 0,
            "diagnosis_history":[
                f"problema inicial del usuario: {last}"
            ]
        }
        
    

    messages = state.get("messages", [])

    if not state.get("greeting_step") and len(messages) == 1:
        return {
            "intent": "greeting_flow",
            "greeting_step": "start"
        }
        
    if state.get("support_option_step"):
        return{"intent": "support_options"}

    
    
    if state.get("diagnosis_step") is not None:
        return{"intent": "diagnosis_flow"}
    
    
  
    last_message = state.get("messages", [])[-1]
    user_text = str(last_message.content).lower() if last_message else ""

    password_keywords = [
        "contraseña",
        "clave",
        "password",
        "no puedo entrar",
        "no puedo ingresar",
        "error autenticacion",
        "error autenticación",
        "correo no funciona",
        "no funciona mi correo",
        "problema de acceso",
        "no puedo acceder al correo"
    ]

    
    if any(word in user_text for word in password_keywords):

        if not state.get("password_step"):
            return {
                "intent": "password_flow",
                "password_step": "confirmation_continue"
            }

        return {"intent": "password_flow"}

    
    if state.get("ticket_step"):
        return {"intent": "crear_ticket"}

    if state.get("ticket_status_step") == "ask_id":
        return {"intent": "estado_ticket"}

    if "ticket" in user_text and "estado" in user_text:
        return {
            "intent": "estado_ticket",
            "ticket_status_step": "ask_id"
        }

    if "ticket" in user_text:
        return {"intent": "crear_ticket"}

    if "humano" in user_text or "agente" in user_text:
        return {"intent": "human"}
    prompt = f"""
Clasifica el mensaje:

"{user_text}"

Opciones:
- password_flow
- crear_ticket
- estado_ticket
- human
- chat_general

Responde solo una palabra.
"""

    response = llm.invoke([HumanMessage(content=prompt)])
    intent = str(response.content).strip().lower().split()[0]

    return {
        "intent": intent,
        "conversation_status": "bot_active"
    }

def chat_general(state: State, config):

    system_prompt = """
Eres un agente de soporte técnico humano.

Tu estilo debe ser:
- conversacional
- natural
- profesional
- amigable

Nunca digas frases como:
"describe tu problema"
"proporcione información"

Usa frases naturales como:
- cuéntame qué está pasando
- revisemos eso
- entiendo, veamos qué sucede

Haz preguntas de diagnóstico cuando haya un problema.
"""

    messages_state = state.get("messages", [])

    if not any(isinstance(m, SystemMessage) for m in messages_state):
        messages_state = [SystemMessage(content=system_prompt)] + messages_state

    response = llm.invoke(messages_state)

    return {"messages": [response]}




def handle_password_issue(state: State):

    messages_state = state.get("messages", [])
    last_message = str(messages_state[-1].content).strip().lower()
    step = state.get("password_step")
    risk = state.get("security_risk", 0)

    if re.search(r"\b(mi\s)?(clave|contraseña|password)\s+es\b", last_message):
        return {
            "messages": [
                AIMessage(content="Por seguridad, nunca compartas tu contraseña. El equipo de soporte jamás te la pedirá.")
            ]
        }

    if not step:
        return {
            "password_step": "confirm_owner",
            "security_risk": 0,
            "messages": [
                AIMessage(content="¿Esta solicitud corresponde unicamente a tu cuenta corporativa? (si/no) ")
            ]
        }

    if step == "confirm_owner":
        if last_message not in ["si", "sí", "yes"]:
            return {
                **reset_password_flow(),
                "intent": "chat_general",
                "messages": [
                    AIMessage(content="Solo puedo ayudarte con solicitudes de tu propia cuenta")
                ]
            }

        return {
            "password_step": "ask_email",
            "messages": [
                AIMessage(content="Indicame tu correo corporativo")
            ]
        }

    if step == "ask_email":

        if not re.match(r"^[^@]+@[^@]+\.[^@]+$", last_message):
            return {
                "password_step": "ask_email",
                "messages": [
                    AIMessage(content="Formato de correo invalido")
                ]
            }

        try:
            zimbra = ZimbraService(
                url="https://correo.serviunix.com/service/soap",
                email=last_message,
                password=os.environ.get("ZIMBRA_PASSWORD")
            )
            
            
            zimbra.authenticate()

            real_data = {
                "folders": zimbra.get_user_folders(),
                "last_sent_subjects": zimbra.get_last_sent_subjects(),
                "signature": zimbra.get_user_signature(),
                "contacts": zimbra.get_contacts(),
                "filters": zimbra.get_filters(),
            }

        except Exception as e:
            print("Error conectando con Zimbra:", e)
            return {"intent": "human"}

        return {
            "password_step": "ask_recent_change",
            "email": last_message,
            "real_data": real_data,  
            "messages": [
                AIMessage(
                    content="¿Has cambiado tu contraseña en los últimos 3 meses? (si/no)"
                )
            ]
        }


    if step == "confirmation_continue":

        if last_message not in ["si", "sí", "no"]:
            return {
                "password_step": "confirmation_continue",
                "messages":[
                    AIMessage(
                        content="""Parece que tu problema es relacionado con acceso o contraseña del correo.

                                ¿Quieres que te ayude a recuperarla ahora mismo? (si/no)
                                 """
                    )
                ]
            }

        if last_message in ["si", "sí"]:
            return{
                "password_step": "confirm_owner",
                "messages": [
                    AIMessage(
                        content="Perfecto. Primero confirmemos algo. ¿Esta solicitud corresponde únicamente a tu cuenta corporativa? (si/no)"
                    )
                ]
            }

        if last_message == "no":
            return {
                **reset_password_flow(),
                "intent": "crear_ticket",
                "ticket_step": "ask_description",
                "messages": [
                    AIMessage(
                        content="Entiendo. Entonces crearé un ticket para que soporte revise tu caso."
                    )
                ]
            }



    if step == "ask_recent_change":

        if last_message not in ["si", "sí", "no"]:
            return {
                "password_step": "ask_recent_change",
                "messages": [
                    AIMessage(
                        content="Responde únicamente si o no. ¿Has cambiado tu contraseña en los últimos 3 meses?"
                    )
                ]
            }

        if last_message in ["no"]:
            return {
                "password_step": "waiting_confirmation",
                "messages": [
                    AIMessage(
                        content=(
                            "Perfecto. Sigue estos pasos para restablecer tu contraseña:\n\n"
                            "1. Ingresa a https://correo.serviunix.com\n"
                            "2. Haz clic en '¿Olvidaste tu contraseña?'\n"
                            "3. Sigue las instrucciones enviadas a tu correo alternativo.\n\n"
                            "Si el problema persiste, indícamelo."
                        )
                    )
                ]
            }

        if last_message in ["si", "sí"]:
            return {
                "password_step": "dynamic_question",
                "security_questions": select_initial_quetions(),
                "user_answers": {},
                "current_question_index": 0,
                "messages": [
                    AIMessage(
                        content="Perfecto. Vamos a validar tu identidad con unas preguntas de seguridad."
                    )
                ]
            }
        
    if step == "waiting_confirmation":

        if last_message in ["si", "sí"]:
            return {
                **reset_password_flow(),
                "messages": [
                    AIMessage(
                        content="Perfecto, me alegra que se haya solucionado tu problema."
                    )
                ]
            }

        if last_message == "no":
            real_data = state.get("real_data") or {}
            profile = build_account_profile(real_data)
            select_questions = generate_adaptive_questions(profile, QUESTION_BANK)

            if len(select_questions) < 2:
                select_questions = select_initial_quetions()

            first_question = select_questions[0]

            return {
                "password_step": "dynamic_question",
                "security_questions": select_questions,
                "user_answers": {},
                "current_question_index": 0,
                "intent": "password_flow",
                "messages": [
                    AIMessage(
                        content=f"Entiendo. Vamos a validar tu identidad con unas preguntas de seguridad.\n\n{get_question_text(first_question)}"
                    )
                ]
            }

        return {
            "password_step": "waiting_confirmation",
            "messages": [
                AIMessage(
                    content="¿Lograste restablecer tu contraseña con esos pasos? Responde si o no."
                )
            ]
        }



    if step == "dynamic_question":

        questions = state.get("security_questions") or []
        answers = state.get("user_answers") or {}
        index = state.get("current_question_index") or 0

        if not questions or index >= len(questions):
            return {"intent": "human"}

        current_question_key = questions[index]
        answers[current_question_key] = last_message
        next_index = index + 1

        if next_index < len(questions):

            next_question_key = questions[next_index]

            return {
                "password_step": "dynamic_question",
                "security_questions": questions,
                "user_answers": answers,
                "current_question_index": next_index,
                "intent": "password_flow",
                "messages": [
                    AIMessage(content=get_question_text(next_question_key))
                ]
            }

        email = state.get("email")

        
        real_data = state.get("real_data", {})
        
        
        score , risk_analysis = calculate_security_score(
            answers,
            real_data,
            QUESTION_BANK
        )
        
        print("Respuestas usuario:", answers)
        print("Datos reales:", real_data)
        print("Score:", score)
        print("Análisis:", risk_analysis)

        validation_summary = {
            "tipo": "password_reset",
            "riesgo": score,
            "analisis_detallado": risk_analysis,
            "respuestas_usuario": answers,
        }


        if score >= 70:
            return {
                "security_risk": score,
                "validation_summary": validation_summary,
                "messages": [
                    AIMessage(
                        content="Validación completada correctamente. Tu solicitud será procesada."
                    )
                ]
            }


        if 40 <= score < 70:

            additional = select_additional_question(
                questions,
                state.get("real_data"),
                QUESTION_BANK
            )

            if not additional:
                return {
                    "intent": "human",
                    "security_risk": score,
                    "validation_summary": validation_summary
                }

            
            return {
                "password_step": "dynamic_question",
                "security_questions": questions + [additional],
                "user_answers": answers,
                "current_question_index": len(questions),
                "security_risk": score,
                "messages": [
                    AIMessage(content=get_question_text(additional))
                ]
            }


        return {
            "intent": "human",
            "security_risk": score,
            "validation_summary": validation_summary
        }

def escalate_human(state: State):

    db = SessionLocal()

    thread_id = state.get("thread_id")
    messages_state = state.get("messages", [])

    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ["SMTP_PORT"])
    smtp_email = os.environ["SMTP_EMAIL"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    destino = os.environ["DESTINO_SOPORTE"]

    
    stmt_user = select(users).where(users.c.phone_number == thread_id)
    user = db.execute(stmt_user).fetchone()

    if user:
        nombre = user.name
        empresa = user.company
        correo = user.email
    else:
        nombre = "No registrado"
        empresa = "No registrada"
        correo = "No disponible"

    
    historial = ""
    for msg in messages_state:
        role = "Usuario" if msg.type == "human" else "Bot"
        historial += f"{role}: {msg.content}\n"

    
    chatwoot_base_url = os.environ.get("CHATWOOT_URL")
    chatwoot_link = f"{chatwoot_base_url}/app/accounts/1/conversations/{thread_id}"

    
    cuerpo = f"""
 ESCALACIÓN A SOPORTE HUMANO


INFORMACIÓN DEL CLIENTE

Nombre: {nombre}
Empresa: {empresa}
Correo: {correo}
Teléfono / ID Conversación: {thread_id}


CONTEXTO COMPLETO DE LA CONVERSACIÓN

{historial}


ACCESO DIRECTO A CHATWOOT

{chatwoot_link}


Este caso requiere intervención manual.
"""

    msg = MIMEText(cuerpo)
    msg["Subject"] = f" Escalación Soporte - {nombre} ({empresa})"
    msg["From"] = smtp_email
    msg["To"] = destino

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_email, smtp_password)
            server.send_message(msg)
    except Exception as e:
        print("Error enviando correo:", e)

    return {
    "messages": [
        AIMessage(content="Te estoy conectando con un técnico. En breve continuará contigo.")
    ],
    "conversation_status": "human_active"
}

def silence(state: State):
    return {"messages": []}


def normalize(text: str):
    """"
    Limpia texto para comparacion:
    - minusculas 
    - sin espacios extras
    - sin caracteres especiales
    """
    
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9áéíóúñ]', '', text)
    return text

def reset_password_flow():
    return {
        "description": None,
        "ticket_step":None,
        "password_step": None,
        "security_questions": None,
        "user_answers": None,
        "current_question_index": None,
        "security_risk": None,
        "real_data": None
    }


builder = StateGraph(State)
builder.add_node("router", router)
builder.add_node("chat_general", chat_general)
builder.add_node("greeting_flow", greeting_flow)
builder.add_node("diagnosis_flow", diagnosis_flow)
builder.add_node("support_options", offer_support_options)
builder.add_node("escalate_human", escalate_human)
builder.add_node("handle_password_issue", handle_password_issue)
builder.add_node("silence", silence)
builder.add_edge(START, "router")
builder.add_conditional_edges(
    "router",
    lambda state: state["intent"],
    {
        "greeting_flow": "greeting_flow",
        "chat_general": "chat_general",
        "human": "escalate_human",
        "password_flow": "handle_password_issue",
        "silence": "silence",
        "diagnosis_flow": "diagnosis_flow",
        "support_options": "support_options"
        
    }
)

builder.add_conditional_edges(
    "handle_password_issue",
    lambda state: state.get("intent", "chat_general"),
    {
    
        "human": "escalate_human",
        "chat_general": END,
        "password_flow": END,
    }
)

builder.add_edge("greeting_flow", END)
builder.add_edge("check_status_ticket", END)
builder.add_edge("escalate_human", END)
builder.add_edge("silence", END)
graph = builder.compile(checkpointer=memory_saver)