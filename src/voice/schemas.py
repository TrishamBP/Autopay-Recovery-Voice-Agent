"""Validated structured output of the conversation model. The model's JSON is untrusted:
unknown intents, preferences or actions fail validation instead of being guessed."""

from pydantic import BaseModel, ConfigDict, Field

from src.voice.vocabulary import BackendAction, ConversationIntent, ResolutionPreference

MAX_SPOKEN_CHARS = 400


class AgentTurnOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    spoken_response: str = Field(min_length=1, max_length=2000)
    intent: ConversationIntent
    resolution_preference: ResolutionPreference | None = None
    requires_backend_action: bool = False
    backend_action: BackendAction | None = None
    needs_human_escalation: bool = False
    end_call: bool = False
    # Only for REQUEST_CALLBACK: local IST wall-clock time the customer asked for.
    callback_time: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")

    @property
    def spoken(self) -> str:
        return self.spoken_response[:MAX_SPOKEN_CHARS]


# Which proposed action is consistent with which intent. The backend runs an action only when
# the validated intent maps to it AND the stage/policy allows it; a mismatched
# `backend_action` from the model is ignored.
INTENT_ACTIONS: dict[ConversationIntent, BackendAction] = {
    ConversationIntent.RETRY_PAYMENT: BackendAction.CHECK_RETRY_ELIGIBILITY,
    ConversationIntent.REQUEST_PAYMENT_LINK: BackendAction.REQUEST_PAYMENT_LINK,
    ConversationIntent.ALREADY_PAID: BackendAction.RECORD_PAYMENT_CLAIM,
    ConversationIntent.DISPUTE_PAYMENT: BackendAction.RECORD_DISPUTE,
    ConversationIntent.REQUEST_CALLBACK: BackendAction.SCHEDULE_CALLBACK,
    ConversationIntent.REQUEST_HUMAN_SUPPORT: BackendAction.ESCALATE_TO_HUMAN,
    ConversationIntent.DO_NOT_CALL: BackendAction.RECORD_DO_NOT_CALL,
}
