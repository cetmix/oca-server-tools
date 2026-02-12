import json

from odoo.tests import tagged
from odoo.tools import mute_logger

from .common import AuditLogClickhouseCommon


@tagged("-at_install", "post_install")
class TestAuditlogClickhouseBuffer(AuditLogClickhouseCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.groups_model_id = cls.env.ref("base.model_res_groups").id
        cls.partner_model_id = cls.env.ref("base.model_res_partner").id

        # Rule for groups: full logging
        cls.groups_rule = cls.create_rule(
            {
                "name": "testrule groups clickhouse",
                "model_id": cls.groups_model_id,
                "log_read": True,
                "log_create": True,
                "log_write": True,
                "log_unlink": True,
                "log_export_data": True,
                "log_type": "full",
                "capture_record": False,
            }
        )

    def setUp(self):
        super().setUp()
        # Ensure rule is subscribed per test.
        self.groups_rule.subscribe()

    def test_01_create_writes_to_buffer_not_auditlog_tables(self):
        buf = self.env["auditlog.log.buffer"].sudo()
        log_model = self.env["auditlog.log"]

        start_buf = buf.search_count([])
        start_logs = log_model.search_count([("model_id", "=", self.groups_model_id)])

        group = (
            self.env["res.groups"]
            .with_context(tracking_disable=True)
            .create({"name": "ch_test_group_1"})
        )

        self.assertEqual(
            log_model.search_count([("model_id", "=", self.groups_model_id)])
            - start_logs,
            0,
            "auditlog.log must NOT be written by auditlog_clickhouse",
        )
        self.assertEqual(buf.search_count([]) - start_buf, 1)

        payload = json.loads(buf.search([], order="id desc", limit=1).payload_json)
        self.assertEqual(payload["log"]["method"], "create")
        self.assertEqual(payload["log"]["model_id"], self.groups_model_id)
        self.assertEqual(payload["log"]["res_id"], group.id)

    def test_02_write_creates_lines(self):
        buf = self.env["auditlog.log.buffer"].sudo()
        start_buf = buf.search_count([])

        group = self.env["res.groups"].create({"name": "CH Group"})
        group.write({"name": "CH Group v2"})

        self.assertGreater(buf.search_count([]), start_buf)

        payload = json.loads(buf.search([], order="id desc", limit=1).payload_json)
        self.assertEqual(payload["log"]["method"], "write")
        self.assertEqual(payload["log"]["model_model"], "res.groups")

        field_names = {line.get("field_name") for line in payload["lines"]}
        self.assertIn("name", field_names)

    def test_03_export_data_creates_single_payload_no_lines(self):
        buf = self.env["auditlog.log.buffer"].sudo()
        start_buf = buf.search_count([])

        self.env["res.groups"].search([]).export_data(["name"])

        self.assertEqual(buf.search_count([]) - start_buf, 1)
        payload = json.loads(buf.search([], order="id desc", limit=1).payload_json)
        self.assertEqual(payload["log"]["method"], "export_data")
        self.assertEqual(payload["lines"], [])

    def test_04_unlink_is_always_logged_even_without_capture_record(self):
        buf = self.env["auditlog.log.buffer"].sudo()
        start_buf = buf.search_count([])

        g = (
            self.env["res.groups"]
            .with_context(tracking_disable=True)
            .create({"name": "ch_test_group_unlink"})
        )
        g.unlink()

        self.assertGreater(buf.search_count([]), start_buf)
        payload = json.loads(buf.search([], order="id desc", limit=1).payload_json)
        self.assertEqual(payload["log"]["method"], "unlink")
        # capture_record=False => lines may be empty, but payload must exist
        self.assertIsInstance(payload["lines"], list)


@tagged("-at_install", "post_install")
class TestAuditlogClickhouseCron(AuditLogClickhouseCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner_model_id = cls.env.ref("base.model_res_partner").id
        cls.rule = cls.create_rule(
            {
                "name": "testrule partner clickhouse cron",
                "model_id": cls.partner_model_id,
                "log_create": True,
                "log_write": True,
                "log_unlink": True,
                "log_type": "full",
            }
        )
        cls.config = cls.create_config(is_active=True)

    def setUp(self):
        super().setUp()
        self.rule.subscribe()

    def test_01_cron_flush_success_deletes_buffers_and_calls_insert(self):
        buf = self.env["auditlog.log.buffer"].sudo()

        partner = (
            self.env["res.partner"]
            .with_context(tracking_disable=True)
            .create({"name": "Cron Test"})
        )
        partner.with_context(tracking_disable=True).write({"name": "Cron Test v2"})

        self.assertGreater(buf.search_count([]), 0)

        with self._patched_clickhouse_client() as dummy:
            buf._cron_flush_to_clickhouse(batch_size=1000)

        self.assertEqual(
            buf.search_count([]), 0, "Buffers must be removed after successful flush"
        )

        # Assert we did at least one INSERT call.
        insert_calls = [
            q for (q, params) in dummy.calls if "INSERT INTO" in (q or "").upper()
        ]
        self.assertTrue(insert_calls, "Cron must insert into ClickHouse")

    def test_02_cron_invalid_json_marks_error_and_keeps_row(self):
        buf = self.env["auditlog.log.buffer"].sudo()
        rec = buf.create(
            {
                "payload_json": "NOT A JSON",
                "state": buf.STATE_PENDING,
            }
        )

        with self._patched_clickhouse_client() as dummy:
            with mute_logger(
                "odoo.addons.auditlog_clickhouse.models.auditlog_log_buffer"
            ):
                res = buf._cron_flush_to_clickhouse(batch_size=1000)

        self.assertTrue(res)

        rec.invalidate_recordset()
        self.assertEqual(rec.state, buf.STATE_ERROR)
        self.assertTrue(rec.error_message)
        self.assertGreaterEqual(rec.attempt_count, 1)

        insert_calls = [
            q for (q, _params) in dummy.calls if "INSERT INTO" in (q or "").upper()
        ]
        self.assertFalse(insert_calls)

    @mute_logger("odoo.addons.auditlog_clickhouse.models.auditlog_log_buffer")
    def test_03_cron_insert_failure_marks_pending_as_error(self):
        buf = self.env["auditlog.log.buffer"].sudo()

        partner = (
            self.env["res.partner"]
            .with_context(tracking_disable=True)
            .create({"name": "Fail Test"})
        )
        partner.with_context(tracking_disable=True).write({"name": "Fail Test v2"})

        pending = buf.search([("state", "=", "pending")])
        self.assertTrue(pending, "Expected pending buffer rows to be created")

        with self._patched_clickhouse_client(raise_on_insert=True):
            res = buf._cron_flush_to_clickhouse(batch_size=1000)

        self.assertTrue(res)

        # Re-read from DB
        errored = buf.search([("id", "in", pending.ids), ("state", "=", "error")])
        self.assertEqual(
            len(errored),
            len(pending),
            "All pending buffer rows must be marked as error on insert failure",
        )

        # Ensure they were not deleted
        remaining = buf.search([("id", "in", pending.ids)])
        self.assertEqual(len(remaining), len(pending))
