from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage
from ..llms import llm
from ..state import State
from config.logger import logger

async def summarizer(state: State):
    """
    Nodo que resume la conversación si excede los 8 mensajes
    para mantener el contexto limpio y ahorrar tokens.
    """
    messages = state.get("messages", [])
    
    # Solo resumimos si hay más de 8 mensajes
    if len(messages) <= 8:
        return {}

    logger.info(f"--- RESUMIENDO CONTEXTO (Total mensajes: {len(messages)}) ---")

    existing_summary = state.get("summary", "")
    
    if existing_summary:
        summary_context = f"Este es un resumen de la conversación hasta ahora: {existing_summary}\n\nNuevos mensajes a resumir:"
    else:
        summary_context = "Resume la siguiente conversación de soporte técnico de forma muy concisa, manteniendo datos clave como nombre, empresa, sede y el problema principal:"

    # Dejamos los últimos 2 mensajes fuera del resumen para mantener la frescura
    messages_to_summarize = messages[:-2]
    
    # Preparamos el prompt de resumen
    summary_prompt = [
        SystemMessage(content=summary_context),
        *messages_to_summarize,
        HumanMessage(content="Genera un resumen nuevo y actualizado que combine lo anterior con estos mensajes.")
    ]

    response = await llm.ainvoke(summary_prompt)
    new_summary = response.content

    # Instrucción para eliminar los mensajes que ya resumimos
    # LangGraph usa RemoveMessage con el ID del mensaje para borrarlo del estado
    delete_messages = [RemoveMessage(id=m.id) for m in messages_to_summarize if hasattr(m, 'id') and m.id]
    
    # Si por alguna razón los mensajes no tienen ID (mensajes nuevos en el primer turno), 
    # LangGraph los maneja, pero aquí nos aseguramos.
    
    return {
        "summary": new_summary,
        "messages": delete_messages
    }
