"""Domain data form on the storage hub: validated, change-emitting KV domains."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any, cast

from animas import Context

from mth_components.storage.service import storage_backend_service_key
from mth_components.storage_domain.domain import DomainImpl
from mth_components.storage_domain.error import DomainError
from mth_components.storage_domain.spec import descriptor_of


def _parse_record(domain: str, table: str, key: str, validate: Any) -> Any:
    """Run one schema validation, translating failure to invalid-record."""
    try:
        return validate()
    except Exception as error:
        slot = "global" if not table else f"record '{key}' in table '{table}'"
        raise DomainError(
            "invalid-record",
            f"domain '{domain}': stored {slot} does not match its schema",
            detail={"table": table, "key": key},
            cause=error,
        ) from error


class DomainFacility:
    """Opens declared domains over routed backends, one handle per name."""

    def __init__(self, ctx: Context, config: Mapping[str, Any]) -> None:
        self.ctx = ctx
        self.config = config
        self._domains: dict[str, DomainImpl] = {}
        self._reserved: set[str] = set()

    async def open(self, spec: Mapping[str, Any]) -> DomainImpl:
        """Open one declared domain, failing the whole call on any error."""
        name = spec["name"]
        if name in self._reserved:
            raise DomainError("already-open", f"domain '{name}' is already open")
        self._reserved.add(name)
        try:
            backend_name = (self.config.get("routes") or {}).get(name) or self.config[
                "backend"
            ]
            backend = self.ctx.storage.backend.get(backend_name)
            kv = getattr(backend, "kv", None)
            if kv is None:
                raise DomainError(
                    "facet-unsupported",
                    f"backend '{backend_name}' routed for domain '{name}' "
                    "has no kv facet",
                )
            unit = await kv.open(descriptor_of(spec))
            try:
                snapshot = await unit.loadAll()
                tables: dict[str, dict[str, Any]] = {}
                for table, table_spec in spec["tables"].items():
                    records: dict[str, Any] = {}
                    for key, raw in list((snapshot["tables"].get(table) or {}).items()):
                        try:
                            records[key] = _parse_record(
                                name,
                                table,
                                key,
                                lambda value=raw, schema=table_spec["valueSchema"]: (
                                    schema.validate(value)
                                ),
                            )
                        except DomainError as error:
                            backup = getattr(unit, "backupRecord", None)
                            if (
                                spec.get("invalidRecords") != "backup-and-skip"
                                or backup is None
                            ):
                                raise
                            moved = await backup(table, key)
                            self.ctx.logger.error(
                                f"domain '{name}': stored record '{key}' in table "
                                f"'{table}' failed schema validation; moved to "
                                f"'{moved}' and treated as absent. Cause: "
                                f"{error.cause}"
                            )
                    tables[table] = records

                global_spec = spec.get("global")
                if global_spec is None:
                    global_value: Any = None
                elif snapshot["global"] is None:
                    global_value = global_spec["initial"]
                else:
                    global_value = _parse_record(
                        name,
                        "",
                        "",
                        lambda: global_spec["schema"].validate(snapshot["global"]),
                    )

                def on_closed() -> None:
                    self._domains.pop(name, None)
                    self._reserved.discard(name)

                domain = DomainImpl(
                    self.ctx,
                    cast(dict[str, Any], spec),
                    unit,
                    tables,
                    global_value,
                    on_closed,
                )
                self._domains[name] = domain
                return domain
            except BaseException:
                await unit.close()
                raise
        except BaseException:
            self._reserved.discard(name)
            raise

    def get(self, name: str) -> DomainImpl | None:
        """Return an open domain by name, or ``None``."""
        return self._domains.get(name)

    async def close_all(self) -> None:
        """Close every still-open domain (unmount path)."""
        await asyncio.gather(
            *[domain.close() for domain in list(self._domains.values())]
        )


async def apply(ctx: Context, config: Mapping[str, Any]) -> None:
    """Mount the domain form once the configured backend services are live."""
    backend_names = list(
        dict.fromkeys([config["backend"], *(config.get("routes") or {}).values()])
    )
    keys = [storage_backend_service_key(name) for name in backend_names]

    def setup(domain_ctx: Context) -> None:
        facility = DomainFacility(domain_ctx, config)

        def install() -> Callable[[], Any]:
            unmount = domain_ctx.storage.mount("domain", facility)

            async def cleanup() -> None:
                await facility.close_all()
                unmount()

            return cleanup

        domain_ctx.effect(install)
        ctx.provide("storageDomain", facility)

    await cast(Any, ctx.inject(keys, setup))


__all__ = ["DomainFacility", "apply"]
