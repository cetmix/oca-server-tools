from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import AuditLogClickhouseCommon


@tagged("-at_install", "post_install")
class TestAuditlogClickhouseConfig(AuditLogClickhouseCommon):
    def test_01_single_active_on_create(self):
        cfg1 = self.create_config(is_active=True, host="h1")
        cfg2 = self.create_config(is_active=True, host="h2")

        cfg1.invalidate_recordset()
        cfg2.invalidate_recordset()

        active = self.env["auditlog.clickhouse.config"].search(
            [("is_active", "=", True)]
        )
        self.assertEqual(len(active), 1)
        self.assertTrue(cfg2.is_active)
        self.assertFalse(cfg1.is_active)

    def test_02_single_active_on_write(self):
        cfg1 = self.create_config(is_active=False, host="h1")
        cfg2 = self.create_config(is_active=True, host="h2")

        cfg1.write({"is_active": True})
        cfg1.invalidate_recordset()
        cfg2.invalidate_recordset()

        active = self.env["auditlog.clickhouse.config"].search(
            [("is_active", "=", True)]
        )
        self.assertEqual(len(active), 1)
        self.assertTrue(cfg1.is_active)
        self.assertFalse(cfg2.is_active)

    def test_03_test_connection_uses_client(self):
        cfg = self.create_config(is_active=True)

        # Without patch, get_clickhouse_client may
        # raise if clickhouse-driver isn't installed
        with self._patched_clickhouse_client() as dummy:
            action = cfg.action_test_connection()

        self.assertTrue(action)
        self.assertTrue(any("SELECT 1" in (q or "") for (q, params) in dummy.calls))

    def test_04_cron_requires_active_config(self):
        self.env["auditlog.clickhouse.config"].search([]).write({"is_active": False})

        with mute_logger("odoo.addons.auditlog_clickhouse.models.auditlog_log_buffer"):
            with self.assertRaises(UserError):
                self.env["auditlog.log.buffer"].sudo()._cron_flush_to_clickhouse(
                    batch_size=10
                )
