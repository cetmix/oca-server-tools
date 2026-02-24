from collections.abc import Mapping
from typing import Any

from odoo import _
from odoo.exceptions import UserError

try:
    from clickhouse_driver import Client as ClickHouseClient
except ImportError:
    ClickHouseClient = None


def _require_driver() -> None:
    """Ensure clickhouse-driver is importable in the current Odoo environment."""
    if ClickHouseClient is None:
        raise UserError(
            _(
                "Python package 'clickhouse-driver' is not available. "
                "Install it in the Odoo environment to use ClickHouse storage."
            )
        )


def get_clickhouse_client(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str | None = None,
    settings: Mapping[str, Any] | None = None,
) -> "ClickHouseClient":
    """Create and return a ClickHouse client (clickhouse-driver).

    Args:
        host: ClickHouse host or IP.
        port: ClickHouse TCP port (native protocol).
        database: Default database to use for queries.
        user: ClickHouse username.
        password: ClickHouse password (optional).
        settings: Optional clickhouse-driver settings dict.

    Returns:
        clickhouse_driver.Client instance configured for native TCP protocol.

    Raises:
        UserError: If the clickhouse-driver package is not installed.
    """
    _require_driver()
    # `settings` is passed as-is to clickhouse-driver, keep it optional and immutable.
    return ClickHouseClient(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password or "",
        settings=dict(settings or {}),
    )
