import contextlib
from unittest.mock import patch

from odoo.addons.auditlog.tests.common import AuditLogRuleCommon


class DummyClickHouseClient:
    """Tiny fake clickhouse client collecting execute() calls."""

    def __init__(self, *, raise_on_insert: bool = False):
        self.raise_on_insert = raise_on_insert
        self.calls = []  # list[(query, params)]

    def execute(self, query, params=None):
        self.calls.append((query, params))
        q = (query or "").strip().upper()
        if q.startswith("SELECT"):
            return [(1,)]
        if self.raise_on_insert and "INSERT INTO" in q:
            raise Exception("Simulated ClickHouse insert error")
        return []


class AuditLogClickhouseCommon(AuditLogRuleCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._cleanup_clickhouse_test_data()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._cleanup_clickhouse_test_data()
        finally:
            super().tearDownClass()

    @classmethod
    def _cleanup_clickhouse_test_data(cls):
        """Ensure clean state for configs and buffer across suites."""
        cls.env["auditlog.clickhouse.config"].sudo().search([]).write(
            {"is_active": False}
        )
        cls.env["auditlog.log.buffer"].sudo().search([]).unlink()

    @classmethod
    def create_config(cls, **vals):
        """Create ClickHouse config with minimal defaults for tests."""
        defaults = {
            "host": "localhost",
            "port": 9000,
            "database": "db",
            "user": "user",
            "password": "pass",
            "is_active": False,
        }
        defaults.update(vals)
        return (
            cls.env["auditlog.clickhouse.config"]
            .with_context(tracking_disable=True)
            .create(defaults)
        )

    @contextlib.contextmanager
    def _patched_clickhouse_client(self, *, raise_on_insert: bool = False):
        """Patch ClickHouse client getter so tests don't require real ClickHouse."""
        dummy = DummyClickHouseClient(raise_on_insert=raise_on_insert)
        target = (
            "odoo.addons.auditlog_clickhouse.models."
            "auditlog_clickhouse_config.get_clickhouse_client"
        )
        with patch(target, autospec=True, return_value=dummy):
            yield dummy

    def _parse_payloads(self):
        """Return list of decoded payload dicts from buffer (oldest first)."""
        buf = self.env["auditlog.log.buffer"].sudo().search([], order="id asc")
        return [rec.payload_json for rec in buf]
