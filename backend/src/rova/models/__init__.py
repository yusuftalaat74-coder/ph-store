"""Importing this package registers all 66 Table objects on rova.models.meta.metadata."""
from rova.models import (  # noqa: F401
    accounts,
    billing,
    catalogue,
    fees,
    fulfilment,
    governance,
    infra,
    integration,
    ordering,
    support,
)
from rova.models.meta import metadata  # noqa: F401
