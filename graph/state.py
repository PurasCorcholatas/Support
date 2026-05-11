from typing import List, Literal, Optional, Annotated
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class State(TypedDict, total=False):

    human_escalated: Optional[bool]

    messages: Annotated[List[BaseMessage], add_messages]
    intent: Literal[
        "chat_general",
        "crear_ticket",
        "human",
        "estado_ticket",
        "greeting_flow"]

    greeting_step: Optional[Literal[
        "start",
        "wait_user_reply",
        "confirm_branch",
        "waiting_problem",
        "register_name",
        "register_company",
        "register_email",
        "register_sede",
        "register_company_sede",
        
    ]]

    branch: Optional[str]
    servicio: Optional[str]
    diagnosis_step: Optional[int]
    diagnosis_history: Optional[List[str]]
    diagnosis_summary: Optional[str]
    diagnosis_images: Optional[List[dict]]
    email_skipped: Optional[bool]
    
    support_option_step: Optional[Literal[
        "offer_options",
        "waiting_choice",
        "similar_detected",
        "waiting_similar"
    ]]

    support_option_retries: Optional[int]

    ticket_step: Optional[Literal[
        "ask_user_info",
        "ask_title",
        "ask_description",
        "ask_email",
        "done"
    ]]

    ticket_status_step: Optional[Literal["ask_id"]]

    description: Optional[str]
    current_question_index: Optional[int]
    email: Optional[str]
    validation_summary: Optional[dict]
    severity: Optional[str]

    email_request_step: Optional[Literal["ask_email"]]
    pending_intent: Optional[str]

    quick_fix_step: Optional[Literal["waiting_result"]]
    quick_fix_solution: Optional[str]

    thread_id: Optional[str]
    chatwoot_conversation_id: Optional[int]
