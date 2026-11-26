"""Animas component entry point for the local credential provider."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from animas import Context

from mth_components.credentials_local.provider import (
    LocalCredentialProvider,
    resolve_spec,
)


async def apply(ctx: Context, **kwargs: Any) -> None:
    """Mount a file-backed provider on the existing credential service."""
    credentials = ctx.get("credentials", None, strict=False)
    if credentials is None:
        raise RuntimeError("credentials-local: the credentials service is not mounted")
    provider = LocalCredentialProvider(resolve_spec(kwargs))

    def on_reference(ref: Any) -> None:
        value = credentials._stored.get(str(ref))
        if value is None:
            provider.unset_ref(ref)
        else:
            provider.set_ref(ref, value)

    def on_record(key: Any) -> None:
        provider.save_record(str(key), credentials._records.get(str(key)))

    def setup() -> Callable[[], None]:
        # Hydrate the service's own tables so unset/delete_record emit their
        # events for credentials that came from the document.
        credentials._stored.update(provider.refs)
        credentials._records.update(
            {key: dict(value) for key, value in provider.records.items()}
        )
        release = credentials.set_provider(provider)  # type: ignore[arg-type]
        ref_disposer = ctx.on("credentials/reference-updated", on_reference)
        record_disposer = ctx.on("credentials/record-updated", on_record)

        def cleanup() -> None:
            ref_disposer()
            record_disposer()
            release()

        return cleanup

    ctx.effect(setup)
