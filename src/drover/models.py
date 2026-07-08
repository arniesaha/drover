from typing import List, Optional, Dict, Any, Union
from pydantic import BaseModel, Field
from datetime import datetime


class Message(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]


class ToolCall(BaseModel):
    tool_name: str
    input: Dict[str, Any]


class AgentEvent(BaseModel):
    id: str = Field(..., description="Unique ID for this event")
    session_id: str = Field(..., description="The session this event belongs to")
    timestamp: datetime
    agent_id: str = Field(
        ..., description="E.g., 'max-claude', 'nix-openclaw', 'max-pimono'"
    )

    # Core event attributes
    event_type: str = Field(
        ...,
        description="'user_message', 'assistant_message', 'tool_call', 'tool_result', 'system_event'",
    )

    # Payload specifics (only populated when relevant)
    message: Optional[Message] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_result: Optional[Dict[str, Any]] = None

    # Telemetry
    # token_usage shape varies across Claude Code releases / harnesses:
    # older builds wrote {input_tokens: 123}, newer ones nest sub-dicts
    # (cache breakdowns), bare strings ("standard"), or arrays. We store
    # whatever they hand us instead of forcing one shape — analytics that
    # care can normalize at read time.
    token_usage: Optional[Dict[str, Any]] = None
    cost_usd: Optional[float] = None

    # Catch-all for raw data
    raw_data: Dict[str, Any] = Field(default_factory=dict)
