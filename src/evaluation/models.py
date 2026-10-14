"""`call_scenario`: the workbook's `Call_Scenarios` sheet, imported for evaluation only.

It describes expected agent behaviour per customer. It must NEVER be visible to the voice agent:
it is excluded from the SQL-query allowlist and from every tool.
"""

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.common.enums import AgentToolAction, CustomerIntent
from src.common.types import enum_column
from src.database import Base


class CallScenario(Base):
    __tablename__ = "call_scenario"

    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customer.customer_id", ondelete="RESTRICT"), primary_key=True
    )
    scenario_name: Mapped[str] = mapped_column(String(200), unique=True)
    customer_persona: Mapped[str] = mapped_column(Text)
    likely_opening_response: Mapped[str] = mapped_column(Text)
    likely_customer_intent: Mapped[CustomerIntent] = mapped_column(
        enum_column(CustomerIntent, "likely_customer_intent")
    )
    expected_agent_behavior: Mapped[str] = mapped_column(Text)
    expected_tool_action: Mapped[AgentToolAction] = mapped_column(
        enum_column(AgentToolAction, "expected_tool_action")
    )
    expected_final_outcome: Mapped[str] = mapped_column(Text)
    evaluation_notes: Mapped[str] = mapped_column(Text)
