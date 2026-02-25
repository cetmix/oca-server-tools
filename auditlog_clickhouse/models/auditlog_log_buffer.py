import json
import logging
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as dt_parser

from odoo import api, fields, models
from odoo.tools import SQL

from odoo.addons.queue_job.exception import RetryableJobError

_logger = logging.getLogger(__name__)

JsonMapping = dict[str, Any]
ChRow = tuple[Any, ...]


class AuditlogLogBuffer(models.Model):
    """
    Buffered audit log payloads waiting to be flushed into ClickHouse.

    Each record stores a pre-built payload produced by the auditlog.rule override.
    Export is asynchronous:

      - A cron enqueues a queue_job.
      - The queue_job locks pending buffer rows (FOR UPDATE SKIP LOCKED),
        converts payloads to ClickHouse tuples and inserts them in batches.
      - Successfully flushed buffer rows are removed from PostgreSQL.

    Design notes:
      - This model is an internal queue; no user-facing ACLs should be provided.
      - queue_job provides retries/backoff when ClickHouse is slow/unavailable.
    """

    _name = "auditlog.log.buffer"
    _description = "Auditlog ClickHouse Buffer"
    _order = "create_date asc, id asc"

    STATE_PENDING = "pending"
    STATE_ERROR = "error"

    # Column order MUST match CREATE TABLE schema and inserted tuples.
    _CH_LOG_COLUMNS: tuple[str, ...] = (
        "id",
        "name",
        "model_id",
        "model_name",
        "model_model",
        "res_id",
        "res_ids",
        "user_id",
        "method",
        "http_request_id",
        "http_session_id",
        "log_type",
        "create_date",
        "create_uid",
        "write_date",
        "write_uid",
    )
    _CH_LINE_COLUMNS: tuple[str, ...] = (
        "id",
        "log_id",
        "field_id",
        "field_name",
        "field_description",
        "old_value",
        "new_value",
        "old_value_text",
        "new_value_text",
        "create_date",
        "create_uid",
        "write_date",
        "write_uid",
    )

    _INVALID_PAYLOAD_MESSAGE = (
        "Invalid payload structure (expected object with 'log' and 'lines')."
    )

    @api.model
    def _selection_state(self) -> list[tuple[str, str]]:
        """Centralized selection for `state`."""
        return [
            (self.STATE_PENDING, self.env._("Pending")),
            (self.STATE_ERROR, self.env._("Error")),
        ]

    payload_json = fields.Json(required=True)
    state = fields.Selection(
        selection=lambda self: self._selection_state(),
        default=lambda self: self.STATE_PENDING,
        required=True,
        index=True,
    )
    attempt_count = fields.Integer(default=0, required=True)
    error_message = fields.Text()

    @staticmethod
    def _to_ch_nullable_string(value: Any) -> str | None:
        """
        Convert value into ClickHouse Nullable(String).

        - None/False -> None
        - str -> as is
        - list/dict/tuple -> JSON string (unicode preserved)
        - other -> str(value)
        """
        if value is None or value is False:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (dict | list | tuple)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value)

    @staticmethod
    def _to_ch_datetime_utc(value: Any) -> datetime | None:
        """
        Convert incoming value to tz-aware UTC datetime.

        We normalize to UTC to keep consistent semantics for ClickHouse
        DateTime64(3, 'UTC').
        """
        if not value:
            return None

        if isinstance(value, datetime):
            parsed = value
        else:
            raw = str(value).strip().replace("Z", "+00:00")
            try:
                parsed = dt_parser.parse(raw)
            except (ValueError, TypeError, OverflowError):
                # Fallback: Odoo parser usually returns naive datetime.
                parsed = fields.Datetime.from_string(value)

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _set_error(self, message: str) -> None:
        """
        Mark records as error + increment attempt_count + store error_message.

        We update per-record to ensure attempt_count increments correctly.
        """
        for rec in self:
            rec.write(
                {
                    "state": self.STATE_ERROR,
                    "attempt_count": rec.attempt_count + 1,
                    "error_message": message,
                }
            )

    @api.model
    def _lock_pending_buffers(self, batch_size: int) -> "AuditlogLogBuffer":
        """
        Fetch up to `batch_size` pending buffers and lock them (FOR UPDATE SKIP LOCKED).

        This prevents concurrent workers/jobs from selecting the same rows and
        inserting duplicates into ClickHouse.
        """
        query = SQL(
            """
            SELECT id
            FROM %s
            WHERE state = %s
            ORDER BY id
                FOR UPDATE SKIP LOCKED
                 LIMIT %s
            """,
            SQL.identifier(self._table),
            self.STATE_PENDING,
            batch_size,
        )
        self.env.cr.execute(query)
        ids = [row[0] for row in self.env.cr.fetchall()]
        return self.browse(ids)

    @api.model
    def _cron_flush_to_clickhouse(self, batch_size: int | None = None) -> bool:
        """
        Enqueue a queue_job to flush buffered rows into ClickHouse.

        This cron does not perform ClickHouse INSERTs directly. It only schedules
        a job, so that queue_job can handle retries and high load.

        :param batch_size: optional override; if not provided,
          uses config.queue_batch_size.
        :return: True (cron compatibility).
        """
        config = self.env["auditlog.clickhouse.config"].sudo().get_active_config()
        if not config:
            _logger.debug("auditlog_clickhouse: cron flush skipped (no active config)")
            return True

        effective_batch = int(batch_size or config.queue_batch_size or 0) or 1000

        if not self.sudo().search([("state", "=", self.STATE_PENDING)], limit=1):
            _logger.debug(
                "auditlog_clickhouse: cron flush skipped (no pending buffers)"
            )
            return True

        channel_name = (
            config.queue_channel_id.complete_name
            if config.queue_channel_id
            and getattr(config.queue_channel_id, "complete_name", None)
            else "root"
        )

        _logger.info(
            "auditlog_clickhouse: enqueue flush job "
            "(config=%s channel=%s batch_size=%s)",
            config.id,
            channel_name,
            effective_batch,
        )

        self.sudo().with_delay(
            channel=channel_name,
            description=f"auditlog_clickhouse: flush buffers (config={config.id})",
        )._job_flush_to_clickhouse(config.id, effective_batch)

        return True

    @api.model
    def _get_active_config_for_job(self, config_id: int):
        config = self.env["auditlog.clickhouse.config"].sudo().browse(config_id)
        if not config or not config.exists() or not config.is_active:
            _logger.info(
                "auditlog_clickhouse: job skipped "
                "(config missing or not active) (config_id=%s)",
                config_id,
            )
            return None
        return config

    @classmethod
    def _payload_is_valid(cls, payload: Any) -> bool:
        """Strict-enough validation to avoid endless RetryableJobError loops."""
        if not isinstance(payload, dict):
            return False

        log_data = payload.get("log")
        lines_data = payload.get("lines")

        if not isinstance(log_data, dict) or not isinstance(lines_data, list):
            return False

        # Minimal required log fields (to avoid CH insert failures forever)
        required = (
            "id",
            "model_id",
            "model_model",
            "user_id",
            "method",
            "create_date",
            "create_uid",
        )
        for key in required:
            if not log_data.get(key):
                return False

        # Lines must be a list of dicts (if any line is broken -> whole payload invalid)
        return all(isinstance(line, dict) for line in lines_data)

    def _collect_rows_from_buffers(self, buffers):
        """Return (valid_buffers, invalid_buffers, log_rows, line_rows)."""
        log_rows: list[ChRow] = []
        line_rows: list[ChRow] = []
        invalid_buffers = self.browse()

        for rec in buffers:
            payload = rec.payload_json

            if not self._payload_is_valid(payload):
                invalid_buffers |= rec
                continue

            log_data = payload["log"]
            lines_data = payload["lines"]

            log_rows.append(self._build_ch_log_row(log_data))
            for line_data in lines_data:
                line_rows.append(self._build_ch_line_row(line_data))

        valid_buffers = buffers - invalid_buffers
        return valid_buffers, invalid_buffers, log_rows, line_rows

    def _mark_invalid_buffers(self, invalid_buffers, config) -> None:
        if not invalid_buffers:
            return
        invalid_buffers._set_error(self.env._(self._INVALID_PAYLOAD_MESSAGE))
        _logger.warning(
            "auditlog_clickhouse: invalid payloads=%s (marked error) (config=%s)",
            len(invalid_buffers),
            config.id,
        )

    def _insert_rows_to_clickhouse(
        self, client, config, log_rows, line_rows, valid_buffers
    ):
        try:
            if log_rows:
                client.execute(
                    f"INSERT INTO {config.database}.auditlog_log ("
                    f"{', '.join(self._CH_LOG_COLUMNS)}) VALUES",
                    log_rows,
                )
            if line_rows:
                client.execute(
                    f"INSERT INTO {config.database}.auditlog_log_line ("
                    f"{', '.join(self._CH_LINE_COLUMNS)}) VALUES",
                    line_rows,
                )
        except Exception as exc:
            _logger.exception(
                "auditlog_clickhouse: INSERT failed (will retry) "
                "(config=%s buffers=%s logs=%s lines=%s)",
                config.id,
                len(valid_buffers),
                len(log_rows),
                len(line_rows),
            )
            raise RetryableJobError(
                f"ClickHouse insert failed: {exc}",
                seconds=60,
            ) from exc

    def _delete_flushed_buffers(self, valid_buffers, config) -> None:
        try:
            valid_buffers.unlink()
        except Exception as exc:
            _logger.exception(
                "auditlog_clickhouse: failed to delete flushed buffers "
                "(config=%s buffers=%s)",
                config.id,
                len(valid_buffers),
            )
            valid_buffers._set_error(
                self.env._("Flushed to ClickHouse but failed to delete buffer rows: %s")
                % exc
            )
        else:
            _logger.info(
                "auditlog_clickhouse: job flushed batch "
                "(config=%s flushed_buffers=%s)",
                config.id,
                len(valid_buffers),
            )

    def _enqueue_next_flush_job_if_needed(self, config, batch_size: int) -> None:
        if not self.sudo().search([("state", "=", self.STATE_PENDING)], limit=1):
            return

        channel_name = (
            config.queue_channel_id.complete_name
            if config.queue_channel_id
            and getattr(config.queue_channel_id, "complete_name", None)
            else "root"
        )
        _logger.debug(
            "auditlog_clickhouse: more pending buffers detected, enqueue next job "
            "(config=%s channel=%s batch_size=%s)",
            config.id,
            channel_name,
            batch_size,
        )
        self.sudo().with_delay(
            channel=channel_name,
            description=f"auditlog_clickhouse: flush buffers (config={config.id})",
        )._job_flush_to_clickhouse(config.id, int(batch_size))

    @api.model
    def _job_flush_to_clickhouse(self, config_id: int, batch_size: int) -> None:
        """
        Queue job: flush one batch of pending buffers into ClickHouse.

        - Locks pending buffers (SKIP LOCKED)
        - Validates payload structure
        - Builds CH rows
        - INSERTs into CH (retryable)
        - Deletes flushed buffers
        - Marks invalid payloads as error (non-retryable)
        - Enqueues next job if more pending exist
        """
        config = self._get_active_config_for_job(config_id)
        if not config:
            return

        pending_buffers = self.sudo()._lock_pending_buffers(int(batch_size))
        if not pending_buffers:
            _logger.debug(
                "auditlog_clickhouse: job no-op (no pending buffers) (config=%s)",
                config.id,
            )
            return

        valid_buffers, invalid_buffers, log_rows, line_rows = (
            self._collect_rows_from_buffers(pending_buffers)
        )

        # Nothing valid: just mark invalids and exit successfully.
        if not valid_buffers:
            self._mark_invalid_buffers(invalid_buffers, config)
            return

        client = config._get_client()
        self._insert_rows_to_clickhouse(
            client=client,
            config=config,
            log_rows=log_rows,
            line_rows=line_rows,
            valid_buffers=valid_buffers,
        )

        # Delete flushed buffers; if deletion fails,
        # mark them as error to avoid re-inserts.
        self._delete_flushed_buffers(valid_buffers, config)

        # Mark invalid ones only after successful CH insert
        # (so RetryableJobError doesn't rollback the marking)
        self._mark_invalid_buffers(invalid_buffers, config)

        # Continue draining queue
        self._enqueue_next_flush_job_if_needed(config, int(batch_size))

    @classmethod
    def _build_ch_log_row(cls, log_data: JsonMapping) -> ChRow:
        """Convert payload['log'] dict into CH tuple (order matches _CH_LOG_COLUMNS)."""
        return (
            int(log_data.get("id") or 0),
            cls._to_ch_nullable_string(log_data.get("name")),
            int(log_data.get("model_id") or 0),
            cls._to_ch_nullable_string(log_data.get("model_name")),
            (log_data.get("model_model") or "unknown"),
            int(log_data.get("res_id") or 0)
            if log_data.get("res_id") is not None
            else None,
            cls._to_ch_nullable_string(log_data.get("res_ids")),
            int(log_data.get("user_id") or 0),
            (log_data.get("method") or "unknown"),
            int(log_data.get("http_request_id") or 0)
            if log_data.get("http_request_id") is not None
            else None,
            int(log_data.get("http_session_id") or 0)
            if log_data.get("http_session_id") is not None
            else None,
            cls._to_ch_nullable_string(log_data.get("log_type")),
            cls._to_ch_datetime_utc(log_data.get("create_date")),
            int(log_data.get("create_uid") or 0),
            cls._to_ch_datetime_utc(log_data.get("write_date")),
            int(log_data.get("write_uid") or 0)
            if log_data.get("write_uid") is not None
            else None,
        )

    @classmethod
    def _build_ch_line_row(cls, line_data: JsonMapping) -> ChRow:
        """
        Convert payload['lines'][] dict into CH
        tuple (order matches _CH_LINE_COLUMNS).
        """
        return (
            int(line_data.get("id") or 0),
            int(line_data.get("log_id") or 0),
            int(line_data.get("field_id") or 0),
            cls._to_ch_nullable_string(line_data.get("field_name")),
            cls._to_ch_nullable_string(line_data.get("field_description")),
            cls._to_ch_nullable_string(line_data.get("old_value")),
            cls._to_ch_nullable_string(line_data.get("new_value")),
            cls._to_ch_nullable_string(line_data.get("old_value_text")),
            cls._to_ch_nullable_string(line_data.get("new_value_text")),
            cls._to_ch_datetime_utc(line_data.get("create_date")),
            int(line_data.get("create_uid") or 0),
            cls._to_ch_datetime_utc(line_data.get("write_date")),
            int(line_data.get("write_uid") or 0)
            if line_data.get("write_uid") is not None
            else None,
        )
