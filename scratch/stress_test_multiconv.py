import os
import sys
import asyncio
import json
import re
from unittest.mock import MagicMock, patch, AsyncMock
from dotenv import load_dotenv

# Add current directory to path
sys.path.append(os.getcwd())

# Load env
load_dotenv()
os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY") or ""
os.environ["ANTHROPIC_API_KEY"] = os.getenv("ANTHROPIC_API_KEY") or ""

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

# Import nodes
from graph.nodes.router import router
from graph.nodes.greeting import greeting_flow
from graph.nodes.diagnosis import diagnosis_flow
from graph.nodes.quick_fix import quick_fix_flow
from graph.nodes.support import offer_support_options
from graph.nodes.support import check_ticket_status

def safe_print(text):
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode('ascii', 'ignore').decode('ascii'))

# --- MOCKING ---
class MockResult:
    def __init__(self, data):
        self.data = data
    def fetchone(self):
        return self.data
    def all(self):
        return [self.data] if self.data else []

class MockDB:
    def __init__(self):
        self.users = {} # phone_number -> dict

    def execute(self, stmt, *args, **kwargs):
        stmt_str = str(stmt).lower()
        
        # Extract phone_number from stmt_str if possible
        phone_match = re.search(r"phone_number = '([^']+)'", stmt_str)
        if not phone_match:
            phone_match = re.search(r"where users.phone_number = :phone_number_1", stmt_str)
            # In some cases we might need to look at args if it's a bound param
            # But for simplicity in this mock, we'll try to find it in the string if it was literal
        
        phone = None
        if phone_match:
            try:
                phone = phone_match.group(1)
            except:
                pass
        
        # Try to get phone from kwargs or args if not in string
        if not phone:
            phone = kwargs.get("phone_number") or (args[0] if args else None)
            if hasattr(phone, "phone_number"): # handle case where it's a mapped object
                phone = phone.phone_number

        # SELECT
        if "select" in stmt_str and "users" in stmt_str:
            # We need to find which user we are looking for. 
            # In the simulation we use thread_id as phone.
            # This is a bit hacky but works for the mock.
            for p, data in self.users.items():
                if p in stmt_str:
                    return MockResult(MagicMock(**data))
            return MockResult(None)

        # INSERT
        if "insert into users" in stmt_str:
            # Very basic extraction of values
            # (In a real test we'd use a better mock, but here we just want it to work)
            pass

        # UPDATE
        if "update users" in stmt_str:
            pass

        return MockResult(None)
    
    def commit(self):
        pass

# A simpler stateful mock for the simulation
class SimpleMockDB:
    def __init__(self):
        self.user_data = {} # phone -> dict

    def execute(self, stmt, *args, **kwargs):
        phone = getattr(self, "current_phone", "unknown")
        
        # Try to get the string representation
        try:
            stmt_str = str(stmt).lower()
        except:
            stmt_str = ""

        # Try to extract parameters
        params = {}
        if hasattr(stmt, "compile"):
            try:
                params = stmt.compile().params
            except:
                pass
        if not params and args and isinstance(args[0], dict):
            params = args[0]
        if not params and kwargs:
            params = kwargs

        if "select" in stmt_str:
            data = self.user_data.get(phone)
            if data:
                return MockResult(MagicMock(**data))
            return MockResult(None)
        
        if "insert" in stmt_str or "update" in stmt_str:
            if phone not in self.user_data:
                self.user_data[phone] = {"phone_number": phone, "name": "pending", "company": "pending", "greeting_step": "start"}
            
            # Update data from params
            for k, v in params.items():
                if k in self.user_data[phone]:
                    self.user_data[phone][k] = v
            
            # Fallback for greeting_step in string
            for step in ["register_name", "register_company_sede", "waiting_problem", "confirm_branch"]:
                if step in stmt_str:
                    self.user_data[phone]["greeting_step"] = step

            return MockResult(None)
        return MockResult(None)

    def commit(self):
        pass

    def close(self):
        pass

mock_db = SimpleMockDB()

# --- PERSONAS ---
PERSONAS = {
    "Direct_Problem": (
        "ACTÚA COMO EL USUARIO HUMANO EN WHATSAPP. "
        "Tu primer mensaje es: 'Hola, quiero migrar mi correo de Zimbra a Office 365'. "
        "RECUERDA: TÚ ERES EL CLIENTE. El otro es el bot de soporte. "
        "NO DES BIENVENIDAS. NO PIDAS EL NOMBRE AL BOT. "
        "Si el bot te pide tu nombre, responde: 'Mi nombre es Juan'. "
        "Si el bot te pide tu empresa, responde: 'Trabajo en TechCorp'. "
        "NO USES EMOJIS."
    ),
    "Standard_Registration": (
        "ACTÚA COMO EL USUARIO HUMANO EN WHATSAPP. "
        "Empieza saludando: 'Hola'. "
        "Sigue el flujo de registro. Da tu nombre 'Juan' y empresa 'Serviunix' cuando te lo pidan. "
        "NO DES BIENVENIDAS AL BOT. TÚ ERES EL CLIENTE. "
        "Una vez registrado, di: 'Tengo un problema, el Outlook no me abre'. "
        "NO USES EMOJIS."
    ),
    "Mixed_Initial": (
        "ACTÚA COMO EL USUARIO HUMANO EN WHATSAPP. "
        "Tu primer mensaje es: 'Hola soy Simon de la empresa Serviunix y tengo un problema con mi clave'. "
        "NO USES EMOJIS."
    ),
    "Registered_Ticket": (
        "ACTÚA COMO EL USUARIO HUMANO EN WHATSAPP. "
        "Eres Juan de Serviunix. Ya estás registrado. "
        "Pregunta: '¿Cómo va mi ticket 43011?'. "
        "NO USES EMOJIS."
    )
}

async def run_simulation(persona_name, persona_desc, phone_number):
    safe_print(f"\n=== SIMULACION: {persona_name} ({phone_number}) ===")
    mock_db.current_phone = phone_number
    # Reset user for this sim
    mock_db.user_data[phone_number] = {"phone_number": phone_number, "name": "pending", "company": "pending", "greeting_step": "start"}
    if phone_number == "573003": # Pre-registered user
         mock_db.user_data[phone_number] = {"phone_number": phone_number, "name": "Juan", "company": "Serviunix", "greeting_step": "waiting_problem"}

    # User Actor (Simulates the human)
    user_actor = ChatOpenAI(model="gpt-4o", temperature=0.7)
    
    # Initial State
    state = {
        "messages": [],
        "thread_id": phone_number,
        "intent": None,
        "greeting_step": None,
        "diagnosis_step": None,
        "diagnosis_history": [],
    }

    chat_history = [SystemMessage(content=persona_desc + "\nRESPONDE SOLO COMO EL USUARIO HUMANO. NO ACTÚES COMO EL BOT. NO USES EMOJIS.")]

    nodes = {
        "greeting_flow": greeting_flow,
        "diagnosis_flow": diagnosis_flow,
        "router": router,
        "quick_fix": quick_fix_flow,
        "support_options": offer_support_options,
        "estado_ticket": check_ticket_status
    }

    for turn in range(8):
        # 1. User Actor generates message
        user_resp = await user_actor.ainvoke(chat_history)
        user_message = str(user_resp.content).strip()
        safe_print(f"USUARIO: {user_message}")
        
        # Add to state
        state["messages"].append(HumanMessage(content=user_message))
        chat_history.append(HumanMessage(content=user_message))

        # 2. Bot Processes (Router -> Node Chain)
        bot_message = ""
        nodes_executed = []
        
        while True:
            # Mocking DB and external calls inside nodes
            with patch('graph.nodes.router.SessionLocal', return_value=mock_db), \
                 patch('graph.nodes.greeting.SessionLocal', return_value=mock_db), \
                 patch('graph.nodes.diagnosis.SessionLocal', return_value=mock_db), \
                 patch('graph.nodes.support.SessionLocal', return_value=mock_db), \
                 patch('graph.nodes.greeting.update_chatwoot_contact', new_callable=AsyncMock), \
                 patch('graph.nodes.support.update_chatwoot_contact', new_callable=AsyncMock), \
                 patch('graph.nodes.support.escalate_human', new_callable=AsyncMock):
                
                # If we just started or a node finished, we might need router
                if not state.get("intent") or state.get("intent") == "end":
                    router_res = await router(state)
                    state.update(router_res)
                
                target_node = state.get("intent")
                if target_node in nodes_executed or target_node == "end" or not target_node:
                    break
                
                nodes_executed.append(target_node)
                safe_print(f"  [Graph] Executing: {target_node} | Step: {state.get('greeting_step') or state.get('diagnosis_step')}")
                
                node_func = nodes.get(target_node)
                if node_func:
                    if asyncio.iscoroutinefunction(node_func):
                        node_res = await node_func(state)
                    else:
                        node_res = node_func(state)
                    
                    state.update(node_res)
                    
                    if node_res.get("messages"):
                        bot_message = str(node_res["messages"][-1].content)
                else:
                    bot_message = f"(Error: Nodo {target_node} no implementado)"
                    break
                
                # Check for transitions (like in main.py)
                if target_node == "diagnosis_flow":
                    if state.get("intent") not in ["support_options", "quick_fix"]:
                        break # Normal end of turn
                    # else continue to next node in chain
                elif target_node == "greeting_flow":
                    break # End of turn
                elif target_node == "support_options":
                    if state.get("intent") not in ["support_agent", "human"]:
                        break
                else:
                    break
            
        if not bot_message:
            bot_message = "Entiendo. En qu ms te puedo ayudar?"
            
        safe_print(f"BOT: {bot_message}")
        chat_history.append(AIMessage(content=bot_message))

        if "gracias" in user_message.lower() and turn > 3:
            break
        if target_node == "quick_fix" or target_node == "support_options" or target_node == "end":
            # Just one more turn to see the result if it's quick_fix
            pass 

async def main():
    safe_print("INICIANDO SIMULACION MASIVA DE CONVERSACIONES")
    
    # 1. Escenario: Problema Directo (Usuario Nuevo)
    await run_simulation("Direct_Problem", PERSONAS["Direct_Problem"], "573001")
    
    # 2. Escenario: Registro Estándar (Usuario Nuevo)
    await run_simulation("Standard_Registration", PERSONAS["Standard_Registration"], "573001_v2")
    
    # 3. Escenario: Registro con datos mezclados
    await run_simulation("Mixed_Initial", PERSONAS["Mixed_Initial"], "573001_v3")
    
    # 4. Escenario: Consulta de Ticket (Usuario Registrado)
    await run_simulation("Registered_Ticket", PERSONAS["Registered_Ticket"], "573003")

if __name__ == "__main__":
    asyncio.run(main())
