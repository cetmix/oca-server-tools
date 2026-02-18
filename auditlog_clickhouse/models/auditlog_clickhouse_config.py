import logging
from typing import Any, Optional

from odoo import api, fields, models
from odoo.exceptions import UserError

from .clickhouse_client import get_clickhouse_client

_logger = logging.getLogger(__name__)


class AuditlogClickhouseConfig(models.Model):
    """
    ClickHouse connection configuration for auditlog_clickhouse.

    Business rules:
      - Only one configuration can be active at a time.
      - UI provides tools to test the connection and (optionally) create tables.

    Notes:
      - As soon as a configuration becomes active, audit log entries will be stored
        in the configured ClickHouse database from that moment.
    """

    _name = "auditlog.clickhouse.config"
    _description = "Auditlog ClickHouse Configuration"
    _rec_name = "display_name"

    DEFAULT_PORT = 9000
    DEFAULT_DB = "odoo_audit"
    DEFAULT_USER = "odoo_audit_writer"
    DEFAULT_QUEUE_BATCH_SIZE = 1000

    is_active = fields.Boolean(
        help=(
            "If checked audit logs will be buffered locally and exported to ClickHouse."
            " Only one configuration can be active at a time."
        ),
    )
    host = fields.Char(
        string="Hostname or IP",
        required=True,
        help=(
            "ClickHouse server hostname or IP address. "
            "Must be reachable from the Odoo server."
        ),
    )
    port = fields.Integer(
        string="TCP Port",
        required=True,
        default=DEFAULT_PORT,
        help=(
            "ClickHouse native TCP port used by clickhouse-driver " "(default is 9000)."
        ),
    )
    database = fields.Char(
        string="Database name",
        required=True,
        default=DEFAULT_DB,
        help=(
            "Target ClickHouse database where auditlog tables exist "
            "(or will be created by the setup button)."
        ),
    )
    user = fields.Char(
        required=True,
        default=DEFAULT_USER,
        help=(
            "ClickHouse user name used for INSERT operations into auditlog tables. "
            "Recommended: a dedicated user with INSERT-only privileges."
        ),
    )
    password = fields.Char(
        help="Password for the ClickHouse user.",
    )

    queue_batch_size = fields.Integer(
        string="Batch size",
        default=DEFAULT_QUEUE_BATCH_SIZE,
        required=True,
        help="Maximum number of buffer rows processed per queue job run.",
    )

    def _default_queue_channel(self):
        Channel = self.env["queue.job.channel"].sudo()
        channel = Channel.search([("complete_name", "=", "root")], limit=1)
        return channel

    queue_channel_id = fields.Many2one(
        comodel_name="queue.job.channel",
        string="Channel",
        required=True,
        default=_default_queue_channel,
        ondelete="restrict",
        help="queue_job channel used for export jobs.",
    )

    @api.depends("host", "port", "database", "user", "is_active")
    def _compute_display_name(self):
        for rec in self:
            base = (
                f"{rec.host or ''}:{rec.port or ''}/"
                f"{rec.database or ''} ({rec.user or ''})"
            )
            rec.display_name = f"{base} [active]" if rec.is_active else base

    @api.model
    def get_active_config(self) -> Optional["AuditlogClickhouseConfig"]:
        """Return the currently active configuration (if any)."""
        config = self.search([("is_active", "=", True)], limit=1)
        _logger.debug(
            "auditlog_clickhouse: get_active_config -> %s",
            config.id if config else None,
        )
        return config

    def _deactivate_other_configs(self) -> None:
        """
        Silently deactivate all other active configurations.

        Called after the current record(s) become active to keep the
        "single active" rule without DB constraint errors.
        """
        other_configs = self.search(
            [("is_active", "=", True), ("id", "not in", self.ids)]
        )
        if other_configs:
            _logger.info(
                "auditlog_clickhouse: deactivating other configs %s (activated=%s)",
                other_configs.ids,
                self.ids,
            )
            other_configs.write({"is_active": False})

    @api.onchange("is_active")
    def _onchange_is_active(self):
        """
        Show disclaimer immediately when user enables the checkbox.

        If another active configuration exists, also warn that it will be
        deactivated after saving.
        """
        for rec in self:
            if not rec.is_active or (rec._origin and rec._origin.is_active):
                continue

            disclaimer = rec.env._(
                "As soon as this connection to ClickHouse is activated, all log entries"
                " from that moment will be stored in the configured ClickHouse"
                " database.\n\n Only one connection can be active at a time."
            )

            domain = [("is_active", "=", True)]
            if rec.id:
                domain.append(("id", "!=", rec.id))

            other = rec.env["auditlog.clickhouse.config"].sudo().search(domain, limit=1)
            if other:
                message = rec.env._(
                    "%s\n\nIf you save this configuration as active, "
                    "the currently active one will be deactivated:\n- %s"
                ) % (disclaimer, other.display_name)
                return {
                    "warning": {
                        "title": rec.env._("ClickHouse activation"),
                        "message": message,
                    }
                }

            return {
                "warning": {
                    "title": rec.env._("ClickHouse activation"),
                    "message": disclaimer,
                }
            }

    @api.model_create_multi
    def create(self, vals_list: list[dict[str, Any]]):
        """
        Enforce single active config on creation.

        If any newly created record is active, deactivate all other active
        configs after the create succeeds.
        """
        records = super().create(vals_list)
        active_records = records.filtered("is_active")
        if active_records:
            _logger.info(
                "auditlog_clickhouse: created active config(s) %s",
                active_records.ids,
            )
            active_records._deactivate_other_configs()
        else:
            _logger.debug("auditlog_clickhouse: created config(s) %s", records.ids)
        return records

    def write(self, vals: dict[str, Any]) -> bool:
        """
        Enforce single active config on update.

        If this write enables the current record(s), deactivate all other active
        configs after the write succeeds.
        """
        turning_on = vals.get("is_active") is True
        result = super().write(vals)

        if turning_on:
            activated = self.filtered("is_active")
            _logger.info(
                "auditlog_clickhouse: activated config(s) %s (via write)",
                activated.ids,
            )
            activated._deactivate_other_configs()
        else:
            _logger.debug(
                "auditlog_clickhouse: updated config(s) %s (vals=%s)",
                self.ids,
                sorted(vals.keys()),
            )

        return result

    def action_test_connection(self) -> dict[str, Any]:
        """UI button: verify ClickHouse connectivity with a trivial query."""
        self.ensure_one()
        _logger.info(
            "auditlog_clickhouse: testing connection "
            "(config=%s host=%s port=%s db=%s user=%s)",
            self.id,
            self.host,
            self.port,
            self.database,
            self.user,
        )

        client = self._get_client()
        try:
            client.execute("SELECT 1")
        except Exception as exc:
            _logger.exception(
                "auditlog_clickhouse: connection test FAILED "
                "(config=%s host=%s port=%s db=%s user=%s)",
                self.id,
                self.host,
                self.port,
                self.database,
                self.user,
            )
            raise UserError(
                self.env._("ClickHouse connection failed: %s") % exc
            ) from exc

        _logger.info(
            "auditlog_clickhouse: connection test OK "
            "(config=%s host=%s port=%s db=%s user=%s)",
            self.id,
            self.host,
            self.port,
            self.database,
            self.user,
        )

        return self._notify(
            title=self.env._("Success"),
            message=self.env._("Connection to ClickHouse is OK."),
            notif_type="success",
        )

    def action_create_auditlog_tables(self) -> dict[str, Any]:
        """
        UI button: create ClickHouse tables if they do not exist.

        Important:
          - This is optional. In production you may point to an existing DB.
          - Database must already exist.
          - We intentionally do not create users/grants in this project.
        """
        self.ensure_one()
        _logger.info(
            "auditlog_clickhouse: creating tables (config=%s db=%s host=%s:%s)",
            self.id,
            self.database,
            self.host,
            self.port,
        )

        client = self._get_client()
        try:
            for statement in self._get_clickhouse_ddl():
                preview = " ".join(statement.strip().splitlines())[:120]
                _logger.debug(
                    "auditlog_clickhouse: executing DDL (config=%s): %s...",
                    self.id,
                    preview,
                )
                client.execute(statement)
        except Exception as exc:
            _logger.exception(
                "auditlog_clickhouse: create tables FAILED "
                "(config=%s db=%s host=%s:%s)",
                self.id,
                self.database,
                self.host,
                self.port,
            )
            raise UserError(
                self.env._("Failed to create ClickHouse tables: %s") % exc
            ) from exc

        _logger.info(
            "auditlog_clickhouse: create tables OK (config=%s db=%s)",
            self.id,
            self.database,
        )

        return self._notify(
            title=self.env._("Success"),
            message=self.env._("Auditlog tables were created (if they did not exist)."),
            notif_type="success",
        )

    def _get_client(self):
        """Build a clickhouse-driver client from the current record values."""
        self.ensure_one()
        _logger.debug(
            "auditlog_clickhouse: building client "
            "(config=%s host=%s port=%s db=%s user=%s)",
            self.id,
            self.host,
            self.port,
            self.database,
            self.user,
        )
        return get_clickhouse_client(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self.password,
        )

    def _get_clickhouse_ddl(self) -> list[str]:
        """
        Return ClickHouse DDL statements for required objects.

        Schema is based on the reference provided in the task. Engines/ORDER BY
        are chosen as safe defaults for append-only workloads.
        """
        self.ensure_one()
        db_name = self.database

        return [
            f"""
            CREATE TABLE IF NOT EXISTS {db_name}.auditlog_log
            (
                id String,
                name Nullable(String),
                model_id Int32,
                model_name Nullable(String),
                model_model String,
                res_id Nullable(Int64),
                res_ids Nullable(String),
                user_id Int32,
                method String,
                http_request_id Nullable(Int64),
                http_session_id Nullable(Int64),
                log_type Nullable(String),
                create_date DateTime64(3, 'UTC'),
                create_uid Int32
            )
            ENGINE = MergeTree
            ORDER BY (create_date, id)
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {db_name}.auditlog_log_line
            (
                id String,
                log_id String,
                field_id Int32,
                field_name Nullable(String),
                field_description Nullable(String),
                old_value Nullable(String),
                new_value Nullable(String),
                old_value_text Nullable(String),
                new_value_text Nullable(String),
                create_date DateTime64(3, 'UTC'),
                create_uid Int32
            )
            ENGINE = MergeTree
            ORDER BY (create_date, id)
            """,
        ]

    @staticmethod
    def _notify(
        *, title: str, message: str, notif_type: str = "info"
    ) -> dict[str, Any]:
        """Return standard Odoo UI notification action."""
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": notif_type,
                "sticky": False,
            },
        }
