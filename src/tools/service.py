"""Tool executor: validate -> bind context -> idempotency -> run in ONE transaction -> record.

The domain mutation, its audit events and the `tool_execution` row commit together. A rejected
or failed tool is rolled back and then recorded in a separate transaction, so a failure is never
reported (or stored) as success. Results go back to the model only after the commit.
"""

import logging

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.common.enums import ToolExecutionStatus
from src.common.exceptions import BusinessRuleViolation, NotFoundError, ValidationFailedError
from src.customers.models import Customer
from src.tools.models import ToolExecution
from src.tools.registry import ToolContext, ToolRegistry, ToolRuntime
from src.tools.schemas import ToolResult

logger = logging.getLogger(__name__)

_DOMAIN_REJECTIONS = (BusinessRuleViolation, ValidationFailedError, NotFoundError)


def _replay(execution: ToolExecution) -> ToolResult:
    status = "succeeded" if execution.status == ToolExecutionStatus.SUCCEEDED else "rejected"
    return ToolResult(
        tool_name=execution.tool_name,
        status=status,
        data=execution.result_summary or {},
        error_category=execution.error_category,
        execution_id=execution.id,
        replayed=True,
    )


async def _record(
    session: AsyncSession,
    *,
    name: str,
    context: ToolContext,
    customer_id: str | None,
    arguments: dict[str, object] | None,
    status: ToolExecutionStatus,
    error_category: str,
    idempotency_key: str | None,
    clock_now: object,
) -> str:
    async with session.begin():
        execution = ToolExecution(
            call_session_id=context.call_session_id,
            customer_id=customer_id,
            tool_name=name,
            arguments=arguments,
            status=status,
            error_category=error_category,
            idempotency_key=idempotency_key,
            started_at=clock_now,
            completed_at=clock_now,
        )
        session.add(execution)
    return execution.id


async def execute_tool(
    session: AsyncSession,
    registry: ToolRegistry,
    runtime: ToolRuntime,
    *,
    name: str,
    raw_args: dict[str, object],
    context: ToolContext,
    idempotency_key: str | None = None,
) -> ToolResult:
    """`session` must not be inside a transaction; the executor owns transaction boundaries."""
    now = runtime.clock()

    def rejected(category: str, message: str, execution_id: str | None = None) -> ToolResult:
        return ToolResult(
            tool_name=name,
            status="rejected",
            error_category=category,
            message=message,
            execution_id=execution_id,
        )

    spec = registry.get(name)
    if spec is None:
        return rejected("unknown_tool", f"Tool {name!r} does not exist")
    try:
        args = spec.args_model.model_validate(raw_args)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
            for e in exc.errors(include_url=False, include_input=False)
        ]
        return rejected("invalid_arguments", "; ".join(problems)[:500])
    if spec.side_effecting and not idempotency_key:
        return rejected("missing_idempotency_key", "Side-effecting tools need an idempotency key")
    arguments = args.model_dump(mode="json")
    key = idempotency_key if spec.side_effecting else None

    if await session.get(Customer, context.customer_id) is None:
        await session.rollback()
        return rejected("customer_not_found", "The bound customer does not exist")
    if key is not None:
        previous = await session.scalar(
            select(ToolExecution).where(
                ToolExecution.tool_name == name, ToolExecution.idempotency_key == key
            )
        )
        if previous is not None:
            # Build the answer before rollback(): rollback expires loaded ORM objects.
            if previous.customer_id != context.customer_id or previous.arguments != arguments:
                outcome = rejected(
                    "idempotency_conflict",
                    "Idempotency key was already used with different arguments",
                )
            else:
                outcome = _replay(previous)
            await session.rollback()
            return outcome
    await session.rollback()  # end the read-only autobegun transaction

    try:
        async with session.begin():
            data = await spec.handler(session, context, args, runtime)
            execution = ToolExecution(
                call_session_id=context.call_session_id,
                customer_id=context.customer_id,
                tool_name=name,
                arguments=arguments,
                status=ToolExecutionStatus.SUCCEEDED,
                result_summary=data,
                idempotency_key=key,
                started_at=now,
                completed_at=runtime.clock(),
            )
            session.add(execution)
    except _DOMAIN_REJECTIONS as exc:
        category = {
            BusinessRuleViolation: "business_rule_violation",
            ValidationFailedError: "invalid_arguments",
            NotFoundError: "not_found",
        }[type(exc)]
        execution_id = await _record(
            session,
            name=name,
            context=context,
            customer_id=context.customer_id,
            arguments=arguments,
            status=ToolExecutionStatus.REJECTED,
            error_category=category,
            idempotency_key=key,
            clock_now=now,
        )
        return rejected(category, exc.message, execution_id)
    except IntegrityError:
        # A concurrent request with the same idempotency key won the race: replay its result.
        replay = None
        if key:
            previous = await session.scalar(
                select(ToolExecution).where(
                    ToolExecution.tool_name == name, ToolExecution.idempotency_key == key
                )
            )
            replay = _replay(previous) if previous is not None else None
        await session.rollback()
        if replay is not None:
            return replay
        logger.exception("tool execution hit a database constraint", extra={"tool": name})
        return ToolResult(
            tool_name=name,
            status="failed",
            error_category="conflict",
            message="The operation conflicted with existing data",
        )
    except Exception:
        logger.exception("tool execution failed", extra={"tool": name})
        # No idempotency key on failures: the caller may retry the same key safely.
        execution_id = await _record(
            session,
            name=name,
            context=context,
            customer_id=context.customer_id,
            arguments=arguments,
            status=ToolExecutionStatus.FAILED,
            error_category="internal_error",
            idempotency_key=None,
            clock_now=now,
        )
        return ToolResult(
            tool_name=name,
            status="failed",
            error_category="internal_error",
            message="The operation failed; nothing was changed",
            execution_id=execution_id,
        )
    return ToolResult(tool_name=name, status="succeeded", data=data, execution_id=execution.id)
