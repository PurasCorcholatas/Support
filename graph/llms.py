from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic

llm_diagnosis = ChatAnthropic(
    model_name="claude-sonnet-4-6",
    temperature=0,
    timeout=60,
    stop=None,
    max_retries=3,
)

llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0,
    max_retries=3,
)


tools = []
