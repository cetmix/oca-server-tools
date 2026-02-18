import contextlib
from unittest.mock import patch

from odoo.tests.common import TransactionCase


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


class AuditLogClickhouseCommon(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._patched_models = set()
        cls._created_rules = cls.env["auditlog.rule"]

        # Clean start (important when suites run in one process)
        cls.env["auditlog.clickhouse.config"].sudo().search([]).write(
            {"is_active": False}
        )
        cls.env["auditlog.log.buffer"].sudo().search([]).unlink()

        # For OUR tests we need active config
        cls.base_cfg = cls.create_config(is_active=True)

    @classmethod
    def create_rule(cls, vals):
        """Create an auditlog.rule and track patched models for cleanup."""
        rule = cls.env["auditlog.rule"].with_context(tracking_disable=True).create(vals)
        cls._created_rules |= rule
        cls._patched_models |= set(rule.model_id.mapped("model"))
        return rule

    @classmethod
    def create_config(cls, **vals):
        """Create ClickHouse config. Keep defaults minimal and test-friendly."""
        defaults = {
            "host": "localhost",
            "port": 9000,
            "database": "db",
            "user": "user",
            "password": "pass",
            "is_active": True,
        }
        defaults.update(vals)
        return (
            cls.env["auditlog.clickhouse.config"]
            .with_context(tracking_disable=True)
            .create(defaults)
        )

    @classmethod
    def tearDownClass(cls):
        try:
            # Avoid leaking to other modules (auditlog)
            cls.env["auditlog.clickhouse.config"].sudo().search([]).write(
                {"is_active": False}
            )
            cls.env["auditlog.log.buffer"].sudo().search([]).unlink()

            for rule in getattr(cls, "_created_rules", cls.env["auditlog.rule"]).sudo():
                try:
                    if rule.exists():
                        rule.unsubscribe()
                except KeyError:
                    continue

            for model in getattr(cls, "_patched_models", set()):
                model_rs = cls.env[model]
                for method in ("create", "read", "write", "unlink", "export_data"):
                    func = getattr(model_rs, method, None)
                    if func is None:
                        continue
                    assert not hasattr(
                        func, "origin"
                    ), f"{model} {method} still patched"
        finally:
            super().tearDownClass()

    @contextlib.contextmanager
    def _patched_clickhouse_client(self, *, raise_on_insert: bool = False):
        """
        Patch get_clickhouse_client used inside auditlog.clickhouse.config._get_client()
        so tests don't require clickhouse-driver nor real ClickHouse.
        """
        dummy = DummyClickHouseClient(raise_on_insert=raise_on_insert)
        target = "odoo.addons.auditlog_clickhouse.models.auditlog_clickhouse_config.get_clickhouse_client"  # noqa: E501
        with patch(target, autospec=True, return_value=dummy):
            yield dummy

    def _parse_payloads(self):
        """Return list of decoded payload dicts from buffer (oldest first)."""
        buf = self.env["auditlog.log.buffer"].sudo().search([], order="id asc")
        return [r.payload_json for r in buf]
