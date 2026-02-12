import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as dt_parser

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

JsonMapping = dict[str, Any]
ChRow = tuple[Any, ...]


class AuditlogLogBuffer(models.Model):
    """
    Buffered audit log payloads waiting to be flushed into ClickHouse.

    Each record stores a pre-serialized JSON payload produced by the auditlog.rule
    override. A periodic cron:
      - reads pending buffer rows
      - converts payload into ClickHouse rows (tuple order matches schema)
      - inserts them in batches
      - deletes successfully flushed buffer rows from PostgreSQL

    Notes:
      - No user-facing ACLs should be provided for this model by design.
      - The cron runs with sudo and is the only expected consumer.
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
    )

    @api.model
    def _selection_state(self) -> list[tuple[str, str]]:
        """Centralized selection for `state`."""
        return [
            (self.STATE_PENDING, self.env._("Pending")),
            (self.STATE_ERROR, self.env._("Error")),
        ]

    payload_json = fields.Text(required=True)
    state = fields.Selection(
        selection=_selection_state,
        default=STATE_PENDING,
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
        if value in (None, False):
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
    def _cron_flush_to_clickhouse(self, batch_size: int = 1000) -> bool:
        """
        Flush pending buffer rows to ClickHouse.

        Steps:
          1) Fetch active ClickHouse configuration.
          2) Read up to `batch_size` pending buffer rows (oldest first).
          3) Deserialize JSON payloads; invalid payloads -> error.
          4) Convert payloads to tuples in CH schema order.
          5) INSERT into ClickHouse in batches.
          6) Delete successfully flushed buffer rows.

        :param batch_size: max number of buffer rows to process per run.
        :return: True for cron compatibility.
        """
        started = time.monotonic()

        config = self.env["auditlog.clickhouse.config"].sudo().get_active_config()
        if not config:
            _logger.warning("auditlog_clickhouse: flush skipped (no active config)")
            return True

        pending_buffers = self.sudo().search(
            [("state", "=", self.STATE_PENDING)],
            order="id asc",
            limit=batch_size,
        )
        if not pending_buffers:
            _logger.debug(
                "auditlog_clickhouse: flush skipped (no pending buffers) (config=%s)",
                config.id,
            )
            return True

        _logger.info(
            "auditlog_clickhouse: flush started (config=%s host=%s:%s db=%s batch_size=%s pending=%s)",
            config.id,
            config.host,
            config.port,
            config.database,
            batch_size,
            len(pending_buffers),
        )

        client = config._get_client()

        log_rows: list[ChRow] = []
        line_rows: list[ChRow] = []
        invalid_buffers = self.browse()

        for buffer_rec in pending_buffers:
            try:
                payload: JsonMapping = json.loads(buffer_rec.payload_json)
            except Exception as exc:
                buffer_rec._set_error(self.env._("Invalid JSON payload: %s") % exc)
                invalid_buffers |= buffer_rec
                continue

            log_data = payload.get("log") or {}
            lines_data = payload.get("lines") or []

            if log_data:
                log_rows.append(self._build_ch_log_row(log_data))

            for line_data in lines_data:
                line_rows.append(self._build_ch_line_row(line_data))

        valid_buffers = pending_buffers - invalid_buffers
        if invalid_buffers:
            _logger.warning(
                "auditlog_clickhouse: invalid JSON payloads=%s (marked error)",
                len(invalid_buffers),
            )

        if not valid_buffers:
            _logger.info(
                "auditlog_clickhouse: flush finished "
                "(nothing valid to insert) (invalid=%s) in %.3fs",
                len(invalid_buffers),
                time.monotonic() - started,
            )
            return True

        # Insert (logs first, then lines) to reduce chance of "orphan lines"
        try:
            # ruff: noqa: E501
            if log_rows:
                client.execute(
                    f"INSERT INTO {config.database}.auditlog_log ({', '.join(self._CH_LOG_COLUMNS)}) VALUES",
                    log_rows,
                )
            if line_rows:
                client.execute(
                    f"INSERT INTO {config.database}.auditlog_log_line ({', '.join(self._CH_LINE_COLUMNS)}) VALUES",
                    line_rows,
                )
        except Exception as exc:
            error_msg = self.env._("ClickHouse insert failed: %s") % exc
            _logger.exception(
                "auditlog_clickhouse: INSERT failed "
                "(config=%s valid_buffers=%s log_rows=%s line_rows=%s)",
                config.id,
                len(valid_buffers),
                len(log_rows),
                len(line_rows),
            )
            valid_buffers._set_error(error_msg)
            return True

        flushed_count = len(valid_buffers)
        valid_buffers.unlink()

        _logger.info(
            "auditlog_clickhouse: flush OK (config=%s flushed_buffers=%s "
            "inserted_logs=%s inserted_lines=%s invalid=%s) in %.3fs",
            config.id,
            flushed_count,
            len(log_rows),
            len(line_rows),
            len(invalid_buffers),
            time.monotonic() - started,
        )
        return True

    @classmethod
    def _build_ch_log_row(cls, log_data: JsonMapping) -> ChRow:
        """Convert payload['log'] dict into CH tuple (order matches _CH_LOG_COLUMNS)."""
        return (
            log_data.get("id"),
            cls._to_ch_nullable_string(log_data.get("name")),
            int(log_data.get("model_id") or 0),
            cls._to_ch_nullable_string(log_data.get("model_name")),
            (log_data.get("model_model") or "unknown"),
            log_data.get("res_id"),
            cls._to_ch_nullable_string(log_data.get("res_ids")),
            int(log_data.get("user_id") or 0),
            (log_data.get("method") or "unknown"),
            log_data.get("http_request_id"),
            log_data.get("http_session_id"),
            cls._to_ch_nullable_string(log_data.get("log_type")),
            cls._to_ch_datetime_utc(log_data.get("create_date")),
            int(log_data.get("create_uid") or 0),
        )

    @classmethod
    def _build_ch_line_row(cls, line_data: JsonMapping) -> ChRow:
        """
        Convert payload['lines'][] dict into CH
        tuple (order matches _CH_LINE_COLUMNS).
        """
        return (
            line_data.get("id"),
            line_data.get("log_id"),
            int(line_data.get("field_id") or 0),
            cls._to_ch_nullable_string(line_data.get("field_name")),
            cls._to_ch_nullable_string(line_data.get("field_description")),
            cls._to_ch_nullable_string(line_data.get("old_value")),
            cls._to_ch_nullable_string(line_data.get("new_value")),
            cls._to_ch_nullable_string(line_data.get("old_value_text")),
            cls._to_ch_nullable_string(line_data.get("new_value_text")),
            cls._to_ch_datetime_utc(line_data.get("create_date")),
            int(line_data.get("create_uid") or 0),
        )
