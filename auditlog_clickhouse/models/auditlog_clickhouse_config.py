import logging
from typing import Any, Optional

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import SQL

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

    FDW_SERVER = "auditlog_clickhouse_srv"
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
        return Channel.search([("complete_name", "=", "root")], limit=1)

    queue_channel_id = fields.Many2one(
        comodel_name="queue.job.channel",
        string="Channel",
        required=True,
        default=_default_queue_channel,
        ondelete="restrict",
        help="queue_job channel used for export jobs.",
    )

    fdw_enabled = fields.Boolean(
        string="FDW enabled",
        readonly=True,
        help="Technical flag set after configuring pg_clickhouse FDW objects.",
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
                id Int64,
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
                create_uid Int32,
                write_date Nullable(DateTime64(3, 'UTC')),
                write_uid Nullable(Int32)
            )
            ENGINE = MergeTree
            ORDER BY (create_date, id)
            """,
            f"""
            CREATE TABLE IF NOT EXISTS {db_name}.auditlog_log_line
            (
                id Int64,
                log_id Int64,
                field_id Int32,
                field_name Nullable(String),
                field_description Nullable(String),
                old_value Nullable(String),
                new_value Nullable(String),
                old_value_text Nullable(String),
                new_value_text Nullable(String),
                create_date DateTime64(3, 'UTC'),
                create_uid Int32,
                write_date Nullable(DateTime64(3, 'UTC')),
                write_uid Nullable(Int32)
            )
            ENGINE = MergeTree
            ORDER BY (create_date, id)
            """,
        ]

    def _fdw_server_exists(self) -> bool:
        self.env.cr.execute(
            "SELECT 1 FROM pg_foreign_server WHERE srvname = %s",
            (self.FDW_SERVER,),
        )
        return bool(self.env.cr.fetchone())

    def _fdw_user_mapping_exists(self) -> bool:
        # pg_user_mappings: srvname, usename (view)
        self.env.cr.execute(
            "SELECT 1 FROM pg_user_mappings "
            "WHERE srvname = %s AND usename = current_user",
            (self.FDW_SERVER,),
        )
        return bool(self.env.cr.fetchone())

    def action_setup_fdw_read(self):
        """UI button: configure pg_clickhouse FDW server + user mapping."""
        self.ensure_one()

        try:
            self.env.cr.execute("CREATE EXTENSION IF NOT EXISTS pg_clickhouse")
        except Exception as exc:
            raise UserError(
                self.env._("pg_clickhouse extension is not available: %s") % exc
            ) from exc

        driver = "binary"
        host = (self.host or "").strip()
        if not host:
            raise UserError(self.env._("Host is required."))
        port = int(self.port or 0) or self.DEFAULT_PORT
        port_opt = str(port)
        dbname = (self.database or "").strip() or self.DEFAULT_DB

        try:
            if self._fdw_server_exists():
                self.env.cr.execute(
                    SQL(
                        """
                        ALTER SERVER %s OPTIONS (
                            SET driver %s,
                            SET host %s,
                            SET port %s,
                            SET dbname %s
                        )
                        """,
                        SQL.identifier(self.FDW_SERVER),
                        driver,
                        host,
                        port_opt,
                        dbname,
                    )
                )
            else:
                self.env.cr.execute(
                    SQL(
                        """
                        CREATE SERVER %s
                        FOREIGN DATA WRAPPER clickhouse_fdw
                        OPTIONS (
                            driver %s,
                            host %s,
                            port %s,
                            dbname %s
                        )
                        """,
                        SQL.identifier(self.FDW_SERVER),
                        driver,
                        host,
                        port_opt,
                        dbname,
                    )
                )
        except Exception as exc:
            raise UserError(
                self.env._("Failed to create/alter FDW server: %s") % exc
            ) from exc

        ch_user = (self.user or "default").strip() or "default"
        ch_pass = self.password or ""

        try:
            if self._fdw_user_mapping_exists():
                self.env.cr.execute(
                    SQL(
                        """
                        ALTER USER MAPPING FOR CURRENT_USER
                        SERVER %s
                        OPTIONS (
                            SET user %s,
                            SET password %s
                        )
                        """,
                        SQL.identifier(self.FDW_SERVER),
                        ch_user,
                        ch_pass,
                    )
                )
            else:
                self.env.cr.execute(
                    SQL(
                        """
                        CREATE USER MAPPING FOR CURRENT_USER
                        SERVER %s
                        OPTIONS (
                            user %s,
                            password %s
                        )
                        """,
                        SQL.identifier(self.FDW_SERVER),
                        ch_user,
                        ch_pass,
                    )
                )
        except Exception as exc:
            raise UserError(
                self.env._("Failed to create/alter user mapping: %s") % exc
            ) from exc

        self._swap_auditlog_tables_to_fdw()

        self.write({"fdw_enabled": True})
        return self._notify(
            title=self.env._("Success"),
            message=self.env._("FDW server and user mapping were configured."),
            notif_type="success",
        )

    def _relation_kind(self, schema: str, name: str) -> str | None:
        """Return pg_class.relkind for schema.name, or None if missing."""
        self.env.cr.execute("SELECT to_regclass(%s)", (f"{schema}.{name}",))
        reg = self.env.cr.fetchone()[0]
        if not reg:
            return None
        self.env.cr.execute(
            """
            SELECT c.relkind
            FROM pg_class c
                     JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s
              AND c.relname = %s
            """,
            (schema, name),
        )
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _drop_foreign_table_if_exists(self, schema: str, name: str):
        kind = self._relation_kind(schema, name)
        if kind == "f":
            self.env.cr.execute(
                SQL(
                    "DROP FOREIGN TABLE %s.%s",
                    SQL.identifier(schema),
                    SQL.identifier(name),
                )
            )

    def _rename_table_if_exists(self, schema: str, name: str, new_name: str):
        kind = self._relation_kind(schema, name)
        if kind == "r":  # ordinary table
            self.env.cr.execute(
                SQL(
                    "ALTER TABLE %s.%s RENAME TO %s",
                    SQL.identifier(schema),
                    SQL.identifier(name),
                    SQL.identifier(new_name),
                )
            )

    def _ensure_sequences(self):
        # needed for integer ids if PG tables are swapped away
        self.env.cr.execute("CREATE SEQUENCE IF NOT EXISTS auditlog_log_id_seq")
        self.env.cr.execute("CREATE SEQUENCE IF NOT EXISTS auditlog_log_line_id_seq")

    def _create_foreign_tables(self, schema: str):
        # pg_clickhouse foreign table options: table_name,
        # (optional) database :contentReference[oaicite:1]{index=1}
        db_opt = (self.database or "").strip()

        # auditlog_log
        self.env.cr.execute(
            SQL(
                """
                CREATE FOREIGN TABLE %s.%s (
                    id bigint,
                    create_date timestamp,
                    create_uid integer,
                    write_date timestamp,
                    write_uid integer,
                    name text,
                    model_id integer,
                    model_name text,
                    model_model text,
                    res_id bigint,
                    res_ids text,
                    user_id integer,
                    method text,
                    http_session_id integer,
                    http_request_id integer,
                    log_type text
                )
                SERVER %s
                OPTIONS (table_name %s, database %s)
                """,
                SQL.identifier(schema),
                SQL.identifier("auditlog_log"),
                SQL.identifier(self.FDW_SERVER),
                "auditlog_log",
                db_opt,
            )
        )

        # auditlog_log_line
        self.env.cr.execute(
            SQL(
                """
                CREATE FOREIGN TABLE %s.%s (
                    id bigint,
                    create_date timestamp,
                    create_uid integer,
                    write_date timestamp,
                    write_uid integer,
                    field_id integer,
                    log_id bigint,
                    old_value text,
                    new_value text,
                    old_value_text text,
                    new_value_text text,
                    field_name text,
                    field_description text
                )
                SERVER %s
                OPTIONS (table_name %s, database %s)
                """,
                SQL.identifier(schema),
                SQL.identifier("auditlog_log_line"),
                SQL.identifier(self.FDW_SERVER),
                "auditlog_log_line",
                db_opt,
            )
        )

    def _recreate_auditlog_log_line_view(self, schema: str):
        # Odoo model auditlog.log.line.view expects this view name.
        # Drop first to avoid old OID dependencies when swapping tables.
        self.env.cr.execute(
            SQL(
                "DROP VIEW IF EXISTS %s.%s",
                SQL.identifier(schema),
                SQL.identifier("auditlog_log_line_view"),
            )
        )
        self.env.cr.execute(
            SQL(
                """
                CREATE VIEW %s.%s AS
                SELECT alogl.id,
                       alogl.create_date,
                       alogl.create_uid,
                       alogl.write_uid,
                       alogl.write_date,
                       alogl.field_id,
                       alogl.log_id,
                       alogl.old_value,
                       alogl.new_value,
                       alogl.old_value_text,
                       alogl.new_value_text,
                       alogl.field_name,
                       alogl.field_description,
                       alog.name,
                       alog.model_id,
                       alog.model_name,
                       alog.model_model,
                       alog.res_id,
                       alog.user_id,
                       alog.method,
                       alog.http_session_id,
                       alog.http_request_id,
                       alog.log_type
                FROM %s.%s alogl
                         JOIN %s.%s alog ON alog.id = alogl.log_id
                """,
                SQL.identifier(schema),
                SQL.identifier("auditlog_log_line_view"),
                SQL.identifier(schema),
                SQL.identifier("auditlog_log_line"),
                SQL.identifier(schema),
                SQL.identifier("auditlog_log"),
            )
        )

    def _swap_auditlog_tables_to_fdw(self):
        """Make auditlog read from ClickHouse through pg_clickhouse foreign tables."""
        self.ensure_one()
        schema = "public"

        # 1) Drop SQL view first (it binds to old table OIDs)
        self.env.cr.execute(
            SQL(
                "DROP VIEW IF EXISTS %s.%s",
                SQL.identifier(schema),
                SQL.identifier("auditlog_log_line_view"),
            )
        )

        # 2) If foreign tables already exist, drop them (safe; data is in ClickHouse)
        self._drop_foreign_table_if_exists(schema, "auditlog_log_line")
        self._drop_foreign_table_if_exists(schema, "auditlog_log")

        # 3) If ordinary tables exist, rename to backup (keep local history)
        self._rename_table_if_exists(
            schema, "auditlog_log_line", "auditlog_log_line_pg_backup"
        )
        self._rename_table_if_exists(schema, "auditlog_log", "auditlog_log_pg_backup")

        # 4) Ensure sequences (needed by our ClickHouse write path)
        self._ensure_sequences()

        # 5) Create foreign tables
        self._create_foreign_tables(schema)

        # 6) Recreate view that auditlog uses for details
        self._recreate_auditlog_log_line_view(schema)

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
