"""Domain data form for the storage hub."""

from __future__ import annotations

from mth_components.storage_domain.domain import DOMAIN_CHANGED, DomainImpl, KvTableImpl
from mth_components.storage_domain.error import DOMAIN_ERROR_CODES, DomainError
from mth_components.storage_domain.facility import DomainFacility, apply
from mth_components.storage_domain.spec import (
    Validator,
    define_domain,
    descriptor_of,
    domain_table,
)

__all__ = [
    "DOMAIN_CHANGED",
    "DOMAIN_ERROR_CODES",
    "DomainError",
    "DomainFacility",
    "DomainImpl",
    "KvTableImpl",
    "Validator",
    "apply",
    "define_domain",
    "descriptor_of",
    "domain_table",
]
