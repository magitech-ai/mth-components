"""Credential authorization seam (`ctx.authorization`).

Ports ``packages/credentials/authorization/src/{index,types}.ts``: a registry
of credential-obtaining flows with one attempt per key, a neutral interaction
vocabulary for notices/prompts, and confirmation that the flow actually
committed its record during the attempt. The seam owns the conversation, never
the protocol.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from types import SimpleNamespace
from typing import Any, ClassVar, Protocol, TypedDict, cast

from animas import Context, Service
from magitech_harness.agent.cancellation import AbortController

AUTHORIZATION_SETTLED = "authorization/settled"


class AuthorizationError(Exception):
    """Stable failure taxonomy for authorization."""

    name = "AuthorizationError"

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class AuthorizationDeclinedError(AuthorizationError):
    """A human declined a prompt, as opposed to a surface failure."""

    name = "AuthorizationDeclinedError"

    def __init__(self, message: str = "the authorization prompt was declined") -> None:
        super().__init__(message, "DECLINED")


class AuthorizationMethod(TypedDict):
    id: str
    label: str


class AuthorizationNotice(TypedDict, total=False):
    message: str
    url: str
    code: str


class AuthorizationPromptOption(TypedDict):
    id: str
    label: str


class AuthorizationPrompt(TypedDict, total=False):
    kind: str
    message: str
    placeholder: str
    options: list[AuthorizationPromptOption]


class AuthorizationInteraction(Protocol):
    def notify(self, notice: AuthorizationNotice) -> None: ...

    def prompt(self, prompt: AuthorizationPrompt) -> Awaitable[str]: ...


class AuthorizationFlow(Protocol):
    key: str
    label: str
    methods: list[AuthorizationMethod]

    def run(self, session: Any) -> Awaitable[None]: ...


class AuthorizationSession(Protocol):
    method: str
    signal: Any

    def commit(self, record: dict[str, Any]) -> Awaitable[None]: ...

    def notify(self, notice: AuthorizationNotice) -> None: ...

    def prompt(self, prompt: AuthorizationPrompt) -> Awaitable[str]: ...


class _Running:
    def __init__(self) -> None:
        self.signal = AbortController().signal
        self.committing = False


class AuthorizationService(Service):
    """Registry of credential flows with one live attempt per key."""

    inject: ClassVar[list[str]] = ["credentials"]

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx, "authorization")
        self._flows: dict[str, AuthorizationFlow] = {}
        self._running: dict[str, _Running] = {}

    def register_flow(self, flow: AuthorizationFlow) -> Callable[[], None]:
        """Register one flow per key; the disposer withdraws it and its attempt."""

        def install() -> Callable[[], None]:
            if flow.key in self._flows:
                raise AuthorizationError(
                    f'an authorization flow for "{flow.key}" is already registered',
                    "DUPLICATE_FLOW",
                )
            self._flows[flow.key] = flow

            def dispose() -> None:
                self._flows.pop(flow.key, None)
                self.cancel(flow.key)

            return dispose

        return cast(
            Callable[[], None], self.ctx.effect(install, "authorization.registerFlow()")
        )

    def list(self) -> list[dict[str, Any]]:
        """One entry per flow in registration order."""
        return [self._entry(flow) for flow in self._flows.values()]

    def describe(self, key: str) -> dict[str, Any] | None:
        """Describe one registered flow, or ``None``."""
        flow = self._flows.get(key)
        return None if flow is None else self._entry(flow)

    def _entry(self, flow: AuthorizationFlow) -> dict[str, Any]:
        return {
            "key": flow.key,
            "label": flow.label,
            "methods": list(flow.methods),
            "inFlight": flow.key in self._running,
        }

    def cancel(self, key: str) -> None:
        """Withdraw the attempt for *key*, if it is not committing."""
        running = self._running.get(key)
        if running is not None and not running.committing:
            running.signal.abort()

    async def begin(self, request: Mapping[str, Any]) -> dict[str, str]:
        """Run one attempt and report ``authorized`` or ``cancelled``."""
        key = request["key"]
        flow = self._flows.get(key)
        if flow is None:
            raise AuthorizationError(
                f'no authorization flow is registered for "{key}"', "NO_FLOW"
            )
        requested_method = request.get("method")
        method = flow.methods[0]["id"] if requested_method is None else requested_method
        if not any(candidate["id"] == method for candidate in flow.methods):
            raise AuthorizationError(
                f'authorization flow for "{key}" offers no method "{method}"',
                "UNKNOWN_METHOD",
            )
        if key in self._running:
            raise AuthorizationError(
                f'an authorization attempt for "{key}" is already running',
                "ALREADY_IN_FLIGHT",
            )
        caller_signal = request.get("signal")
        if caller_signal is not None and caller_signal.aborted:
            return {"status": "cancelled"}
        running = _Running()
        self._running[key] = running

        def withdraw() -> None:
            if not running.committing:
                running.signal.abort(getattr(caller_signal, "reason", None))

        dispose = (
            caller_signal.on_abort(withdraw)
            if caller_signal is not None
            else lambda: None
        )
        settlement = "failed"
        try:
            outcome = await self._attempt(flow, method, running, request["interaction"])
            settlement = outcome["status"]
            return outcome
        finally:
            dispose()
            self._running.pop(key, None)
            self._settle(key, settlement)

    def _settle(self, key: str, settlement: str) -> None:
        listeners = list(self.ctx.events._listeners.get(AUTHORIZATION_SETTLED, []))
        for listener in listeners:
            try:
                returned = listener.callback(key, settlement)
                if inspect.isawaitable(returned):
                    task = asyncio.ensure_future(returned)

                    def observe(future: Any) -> None:
                        if not future.cancelled() and future.exception() is not None:
                            self.ctx.logger.warning(
                                f"authorization: an authorization/settled listener for "
                                f'"{key}" failed'
                            )

                    task.add_done_callback(observe)
            except Exception as error:
                self.ctx.logger.warning(
                    f'authorization: an authorization/settled listener for "{key}" '
                    f"failed: {error}"
                )

    async def _attempt(  # noqa: PLR0915
        self,
        flow: AuthorizationFlow,
        method: str,
        running: _Running,
        interaction: AuthorizationInteraction,
    ) -> dict[str, str]:
        observed = {"declined": False, "committed": False}

        def on_record(key: Any) -> None:
            if key == flow.key:
                observed["committed"] = True

        unwatch = self.ctx.on("credentials/record-updated", on_record)
        withdrawn = asyncio.Event()

        def on_withdraw() -> None:
            withdrawn.set()

        unwatch_signal = running.signal.on_abort(on_withdraw)
        try:

            async def commit(record: dict[str, Any]) -> None:
                if running.signal.aborted:
                    running.signal.throw_if_aborted()
                current = self._running.get(flow.key)
                if current is not running:
                    raise AuthorizationError(
                        "authorization attempt is no longer active", "CANCELLED"
                    )
                running.committing = True
                credentials = self.ctx.get("credentials", None, strict=True)
                await credentials.modify_record(flow.key, lambda _current: record)

            async def prompt(spec: AuthorizationPrompt) -> str:
                try:
                    result = interaction.prompt(spec)
                    return await result if inspect.isawaitable(result) else result
                except AuthorizationDeclinedError:
                    observed["declined"] = True
                    raise

            def notify(notice: AuthorizationNotice) -> None:
                try:
                    interaction.notify(notice)
                except Exception as error:
                    self.ctx.logger.warning(
                        "authorization: the interaction surface failed to render "
                        "a notice"
                    )
                    self.ctx.logger.warning(error)

            task = asyncio.ensure_future(
                self._call_flow(flow, method, running.signal, commit, notify, prompt)
            )
            waiter = asyncio.ensure_future(withdrawn.wait())
            try:
                done, _ = await asyncio.wait(
                    {task, waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if task not in done:
                    # The orphan keeps running; consume its eventual failure so
                    # it cannot surface as an unretrieved task exception.
                    task.add_done_callback(
                        lambda finished: (
                            finished.exception() if not finished.cancelled() else None
                        )
                    )
                    return {"status": "cancelled"}
                try:
                    await task
                except Exception:
                    if running.signal.aborted or observed["declined"]:
                        return {"status": "cancelled"}
                    raise
            finally:
                if not waiter.done():
                    waiter.cancel()
        finally:
            unwatch_signal()
            unwatch()
        if not observed["committed"]:
            raise AuthorizationError(
                f'authorization flow for "{flow.key}" resolved without committing '
                "a credential record in this attempt",
                "NOT_COMMITTED",
            )
        credentials = self.ctx.get("credentials", None, strict=True)
        stored = credentials.describe_record(flow.key)
        if not stored.get("configured"):
            raise AuthorizationError(
                f'authorization flow for "{flow.key}" deleted its credential record '
                "instead of committing one",
                "NOT_COMMITTED",
            )
        return {"status": "authorized"}

    async def _call_flow(  # noqa: PLR0917
        self,
        flow: AuthorizationFlow,
        method: str,
        signal: Any,
        commit: Any,
        notify: Any,
        prompt: Any,
    ) -> None:
        result = flow.run(
            SimpleNamespace(
                method=method,
                signal=signal,
                commit=commit,
                notify=notify,
                prompt=prompt,
            )
        )
        if inspect.isawaitable(result):
            await result


__all__ = [
    "AUTHORIZATION_SETTLED",
    "AuthorizationDeclinedError",
    "AuthorizationError",
    "AuthorizationService",
]
