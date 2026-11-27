"""Unit tests for the credential authorization seam."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from animas import Context
from magitech_harness.agent.cancellation import AbortController

from mth_components.authorization import (
    AUTHORIZATION_SETTLED,
    AuthorizationDeclinedError,
    AuthorizationError,
    AuthorizationService,
)
from mth_components.authorization.component import apply


class _Credentials:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.records: dict[str, dict[str, Any]] = {}

    async def modify_record(self, key: str, mutation: Any) -> None:
        current = self.records.get(key)
        updated = mutation(current)
        if updated is None:
            self.records.pop(key, None)
        else:
            self.records[key] = dict(updated)
        self.ctx.emit("credentials/record-updated", key)

    def describe_record(self, key: str) -> dict[str, Any]:
        record = self.records.get(key)
        return {"configured": record is not None}


def _ctx() -> tuple[Context, _Credentials]:
    ctx = Context()
    credentials = _Credentials(ctx)
    ctx.provide("credentials", credentials)
    return ctx, credentials


def _flow(
    key: str = "llm-demo/main",
    *,
    run: Any = None,
    methods: Any = None,
) -> Any:
    async def default_run(session: Any) -> None:
        await session.commit({"kind": "api-key", "key": "secret"})

    return SimpleNamespace(
        key=key,
        label="Demo",
        methods=methods or [{"id": "oauth", "label": "Sign in"}],
        run=run or default_run,
    )


def _interaction(**overrides: Any) -> Any:
    notices: list[Any] = []

    def notify(notice: Any) -> None:
        notices.append(notice)

    async def prompt(spec: Any) -> str:
        return "answer"

    return SimpleNamespace(
        notify=overrides.get("notify", notify),
        prompt=overrides.get("prompt", prompt),
        notices=notices,
    )


def test_error_taxonomy() -> None:
    error = AuthorizationError("bad", "NO_FLOW")
    assert error.name == "AuthorizationError"
    assert error.code == "NO_FLOW"
    declined = AuthorizationDeclinedError()
    assert declined.code == "DECLINED"
    assert "declined" in str(declined)


def test_registry_lifecycle() -> None:
    ctx, _credentials = _ctx()
    service = AuthorizationService(ctx)
    flow = _flow()
    dispose = service.register_flow(flow)
    assert service.list() == [
        {
            "key": flow.key,
            "label": "Demo",
            "methods": flow.methods,
            "inFlight": False,
        }
    ]
    assert service.describe(flow.key)["inFlight"] is False
    assert service.describe("missing") is None
    with pytest.raises(AuthorizationError, match="already registered"):
        service.register_flow(_flow())
    dispose()
    assert service.list() == []
    dispose()


@pytest.mark.asyncio
async def test_begin_validation() -> None:
    ctx, _credentials = _ctx()
    service = AuthorizationService(ctx)
    with pytest.raises(AuthorizationError, match="no authorization flow"):
        await service.begin({"key": "missing", "interaction": _interaction()})
    service.register_flow(_flow())
    with pytest.raises(AuthorizationError, match="offers no method"):
        await service.begin(
            {"key": "llm-demo/main", "method": "bogus", "interaction": _interaction()}
        )
    pre_aborted = AbortController()
    pre_aborted.abort()
    assert await service.begin(
        {
            "key": "llm-demo/main",
            "signal": pre_aborted.signal,
            "interaction": _interaction(),
        }
    ) == {"status": "cancelled"}

    blocked = SimpleNamespace(
        key="llm-demo/main",
        label="Blocked",
        methods=[{"id": "oauth", "label": "Sign in"}],
        run=lambda session: asyncio.sleep(0.05),
    )
    busy = AuthorizationService(ctx)
    busy.register_flow(blocked)
    task = asyncio.ensure_future(
        busy.begin({"key": "llm-demo/main", "interaction": _interaction()})
    )
    await asyncio.sleep(0)
    with pytest.raises(AuthorizationError, match="already running"):
        await busy.begin({"key": "llm-demo/main", "interaction": _interaction()})
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_successful_attempt_and_settlement() -> None:
    ctx, credentials = _ctx()
    service = AuthorizationService(ctx)
    seen: list[Any] = []

    async def observer(key: Any, settlement: Any) -> None:
        seen.append((key, settlement))

    ctx.on(AUTHORIZATION_SETTLED, observer)
    interaction = _interaction()
    flow = _flow()

    async def run(session: Any) -> None:
        interaction.notify({"message": "Continue"})
        answer = await session.prompt({"kind": "text", "message": "Code?"})
        await session.commit({"kind": "api-key", "key": answer})

    service.register_flow(_flow(run=run))
    result = await service.begin({"key": flow.key, "interaction": interaction})
    assert result == {"status": "authorized"}
    await asyncio.sleep(0)
    assert seen == [(flow.key, "authorized")]
    assert credentials.records[flow.key]["key"] == "answer"
    assert interaction.notices == [{"message": "Continue"}]


@pytest.mark.asyncio
async def test_cancellation_paths() -> None:
    ctx, _credentials = _ctx()
    service = AuthorizationService(ctx)
    interaction = _interaction()

    async def decline(spec: Any) -> str:
        raise AuthorizationDeclinedError()

    service.register_flow(
        _flow(run=lambda session: session.prompt({"kind": "text", "message": "x"}))
    )
    assert await service.begin(
        {"key": "llm-demo/main", "interaction": _interaction(prompt=decline)}
    ) == {"status": "cancelled"}

    async def slow(session: Any) -> None:
        await asyncio.sleep(1)
        await session.commit({"kind": "api-key"})

    service = AuthorizationService(ctx)
    service.register_flow(_flow(run=slow))
    controller = AbortController()
    task = asyncio.ensure_future(
        service.begin(
            {
                "key": "llm-demo/main",
                "interaction": interaction,
                "signal": controller.signal,
            }
        )
    )
    await asyncio.sleep(0)
    service.cancel("llm-demo/main")
    assert await task == {"status": "cancelled"}


@pytest.mark.asyncio
async def test_commit_contract_failures() -> None:
    ctx, _credentials = _ctx()
    service = AuthorizationService(ctx)
    service.register_flow(_flow(run=lambda session: asyncio.sleep(0)))
    with pytest.raises(AuthorizationError, match="without committing"):
        await service.begin({"key": "llm-demo/main", "interaction": _interaction()})

    async def delete(session: Any) -> None:
        await session.commit({"kind": "api-key"})
        await session.commit(None)  # type: ignore[arg-type]

    service = AuthorizationService(ctx)
    service.register_flow(_flow(run=delete))
    with pytest.raises(AuthorizationError, match="deleted its credential"):
        await service.begin({"key": "llm-demo/main", "interaction": _interaction()})


@pytest.mark.asyncio
async def test_listener_and_surface_failures_are_contained() -> None:
    ctx, _credentials = _ctx()
    service = AuthorizationService(ctx)

    def bad_listener(key: Any, settlement: Any) -> None:
        raise RuntimeError("watcher failed")

    ctx.on(AUTHORIZATION_SETTLED, bad_listener)

    def bad_notify(notice: Any) -> None:
        raise RuntimeError("surface failed")

    async def run(session: Any) -> None:
        session.notify({"message": "hello"})
        await session.commit({"kind": "api-key"})

    service.register_flow(_flow(run=run))
    result = await service.begin(
        {"key": "llm-demo/main", "interaction": _interaction(notify=bad_notify)}
    )
    assert result == {"status": "authorized"}


@pytest.mark.asyncio
async def test_commit_after_withdrawal_is_refused() -> None:
    ctx, _credentials = _ctx()
    service = AuthorizationService(ctx)
    controller = AbortController()

    async def late_commit(session: Any) -> None:
        controller.abort()
        with pytest.raises(AuthorizationError, match="no longer active"):
            await session.commit({"kind": "api-key"})
        session.signal.abort()
        raise RuntimeError("flow failed after withdrawal")

    service.register_flow(_flow(run=late_commit))
    result = await service.begin(
        {
            "key": "llm-demo/main",
            "interaction": _interaction(),
            "signal": controller.signal,
        }
    )
    assert result == {"status": "cancelled"}


@pytest.mark.asyncio
async def test_component_apply() -> None:
    ctx, _credentials = _ctx()
    await apply(ctx)
    assert isinstance(ctx.authorization, AuthorizationService)


@pytest.mark.asyncio
async def test_remaining_authorization_branches(tmp_path: Any) -> None:
    ctx, credentials = _ctx()
    service = AuthorizationService(ctx)

    def sync_listener(key: Any, settlement: Any) -> None:
        return None

    async def failing_async_listener(key: Any, settlement: Any) -> None:
        raise RuntimeError("async watcher failed")

    ctx.on(AUTHORIZATION_SETTLED, sync_listener)
    ctx.on(AUTHORIZATION_SETTLED, failing_async_listener)

    def sync_flow(session: Any) -> None:
        ctx.emit("credentials/record-updated", "other/key")
        credentials.records["llm-demo/main"] = {"kind": "api-key"}
        ctx.emit("credentials/record-updated", "llm-demo/main")

    service.register_flow(_flow(run=sync_flow))
    assert await service.begin(
        {"key": "llm-demo/main", "interaction": _interaction()}
    ) == {"status": "authorized"}
    assert credentials.records["llm-demo/main"] == {"kind": "api-key"}
    await asyncio.sleep(0)

    def failing_flow(session: Any) -> None:
        raise RuntimeError("flow broke")

    service = AuthorizationService(ctx)
    service.register_flow(_flow(run=failing_flow))
    with pytest.raises(RuntimeError, match="flow broke"):
        await service.begin({"key": "llm-demo/main", "interaction": _interaction()})

    async def stale_commit(session: Any) -> None:
        service._running["llm-demo/main"] = SimpleNamespace(
            committing=False, signal=session.signal
        )
        with pytest.raises(AuthorizationError, match="no longer active"):
            await session.commit({"kind": "api-key"})
        session.signal.abort()
        raise RuntimeError("flow failed after withdrawal")

    service = AuthorizationService(ctx)
    service.register_flow(_flow(run=stale_commit))
    assert await service.begin(
        {"key": "llm-demo/main", "interaction": _interaction()}
    ) == {"status": "cancelled"}

    async def committing_withdrawal(session: Any) -> None:
        service._running["llm-demo/main"].committing = True
        session.signal.abort()
        await session.commit({"kind": "api-key"})

    service = AuthorizationService(ctx)
    service.register_flow(_flow(run=committing_withdrawal))
    assert await service.begin(
        {"key": "llm-demo/main", "interaction": _interaction()}
    ) == {"status": "cancelled"}

    async def caller_withdrawal(session: Any) -> None:
        service._running["llm-demo/main"].committing = True
        controller.abort()
        await asyncio.sleep(0)
        raise RuntimeError("caller withdrew")

    controller = AbortController()
    service = AuthorizationService(ctx)
    service.register_flow(_flow(run=caller_withdrawal))
    with pytest.raises(RuntimeError, match="caller withdrew"):
        await service.begin(
            {
                "key": "llm-demo/main",
                "interaction": _interaction(),
                "signal": controller.signal,
            }
        )


@pytest.mark.asyncio
async def test_reviewer_defects_closed() -> None:
    ctx, _credentials = _ctx()
    service = AuthorizationService(ctx)
    service.register_flow(_flow())
    with pytest.raises(AuthorizationError, match="offers no method"):
        await service.begin(
            {"key": "llm-demo/main", "method": "", "interaction": _interaction()}
        )

    before = {
        task for task in asyncio.all_tasks() if task is not asyncio.current_task()
    }
    assert await service.begin(
        {"key": "llm-demo/main", "interaction": _interaction()}
    ) == {"status": "authorized"}
    await asyncio.sleep(0)
    leaked = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task() and task not in before
    ]
    assert leaked == []

    errors: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()

    def capture(_loop: Any, context: dict[str, Any]) -> None:
        errors.append(context)

    loop.set_exception_handler(capture)

    async def late_failure(session: Any) -> None:
        session.signal.abort()
        await asyncio.sleep(0)
        raise RuntimeError("orphan failed")

    failing = AuthorizationService(ctx)
    failing.register_flow(_flow(run=late_failure))
    assert await failing.begin(
        {"key": "llm-demo/main", "interaction": _interaction()}
    ) == {"status": "cancelled"}
    await asyncio.sleep(0.01)
    loop.set_exception_handler(previous)
    assert errors == []

    def causal_failure(session: Any) -> None:
        try:
            raise KeyError("root cause")
        except KeyError as error:
            raise RuntimeError("flow failed") from error

    causal = AuthorizationService(ctx)
    causal.register_flow(_flow(run=causal_failure))
    with pytest.raises(RuntimeError, match="flow failed") as caught:
        await causal.begin({"key": "llm-demo/main", "interaction": _interaction()})
    assert isinstance(caught.value.__cause__, KeyError)
