import json
import logging
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import (
    Any,
    TypedDict,
)

from odoo import models

from odoo.addons.auditlog.models.rule import EMPTY_DICT, FIELDS_BLACKLIST, DictDiffer

_logger = logging.getLogger(__name__)


class _PayloadLog(TypedDict, total=False):
    id: str
    name: str | None
    model_id: int
    model_name: str | None
    model_model: str
    res_id: int | None
    res_ids: str | None
    user_id: int
    method: str
    http_request_id: int | None
    http_session_id: int | None
    log_type: str | None
    create_date: str
    create_uid: int


class _PayloadLine(TypedDict, total=False):
    id: str
    log_id: str
    field_id: int
    field_name: str | None
    field_description: str | None
    old_value: Any | None
    new_value: Any | None
    old_value_text: Any | None
    new_value_text: Any | None
    create_date: str
    create_uid: int


class _Payload(TypedDict):
    log: _PayloadLog
    lines: list[_PayloadLine]


def _json_default(obj: Any) -> str:
    """json.dumps(default=...) helper.

    Keeps the payload JSON-friendly even if auditlog values contain datetime/date.
    """
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    return str(obj)


class AuditlogRule(models.Model):
    _inherit = "auditlog.rule"

    def _get_rule_settings(self, model_id: int) -> tuple[set[str], bool]:
        """Return (fields_to_exclude_set, capture_record) for the given model_id.

        We cache the result on the registry pool to avoid
        a DB hit for every audited call.
        Cache is naturally reset on registry reload
        (auditlog invalidates registry on rule changes).
        """
        cache: dict[int, tuple[set[str], bool]] = getattr(
            self.pool, "_auditlog_clickhouse_rule_cache", {}
        )
        if not hasattr(self.pool, "_auditlog_clickhouse_rule_cache"):
            self.pool._auditlog_clickhouse_rule_cache = cache

        if model_id in cache:
            return cache[model_id]

        rule = self.sudo().search([("model_id", "=", model_id)], limit=1)
        excluded = set(
            (rule.fields_to_exclude_ids.mapped("name") if rule else [])
            + FIELDS_BLACKLIST
        )
        capture_record = bool(rule and rule.capture_record)

        cache[model_id] = (excluded, capture_record)

        _logger.debug(
            "auditlog_clickhouse: cached rule settings "
            "for model_id=%s (excluded=%s capture_record=%s)",
            model_id,
            len(excluded),
            capture_record,
        )
        return cache[model_id]

    # flake8: noqa: C901
    def create_logs(
        self,
        uid: int,
        res_model: str,
        res_ids: Sequence[int],
        method: str,
        old_values: Mapping[int, Mapping[str, Any]] | None = None,
        new_values: Mapping[int, Mapping[str, Any]] | None = None,
        additional_log_values: Mapping[str, Any] | None = None,
    ) -> None:
        """Write audit logs to ClickHouse buffer instead of PostgreSQL audit tables.

        This overrides `auditlog.rule.create_logs()`:
        - No rows are created in `auditlog.log` / `auditlog.log.line` (PostgreSQL).
        - A single JSON payload is stored in `auditlog.log.buffer` per logged entry.
        - The cron will later flush these payloads into ClickHouse.

        Logging:
          - DEBUG: timings + counts of generated buffer rows/lines.
          - WARNING/EXCEPTION: failures creating buffer payload rows.
        """
        config = self.env["auditlog.clickhouse.config"].sudo().get_active_config()
        if not config:
            return super().create_logs(
                uid,
                res_model,
                res_ids,
                method,
                old_values=old_values,
                new_values=new_values,
                additional_log_values=additional_log_values,
            )
        started = time.monotonic()

        old_values = old_values or EMPTY_DICT
        new_values = new_values or EMPTY_DICT
        additional_log_values = dict(additional_log_values or {})
        log_type = additional_log_values.get("log_type")  # 'full' / 'fast'

        if _logger.isEnabledFor(logging.DEBUG):
            _logger.debug(
                "auditlog_clickhouse: create_logs start "
                "(uid=%s model=%s method=%s res_ids=%s log_type=%s)",
                uid,
                res_model,
                method,
                len(res_ids),
                log_type,
            )

        # Prefer auditlog's model cache (filled by their _register_hook),
        # fallback to ir.model lookup.
        model_id = self.pool._auditlog_model_cache.get(res_model)
        if not model_id:
            model_id = self.env["ir.model"].sudo()._get(res_model).id

        model_rec = self.env["ir.model"].sudo().browse(model_id)
        model_rs = self.env[res_model]

        fields_to_exclude_set, capture_record = self._get_rule_settings(model_id)

        # Single timestamp for the whole batch: consistent ordering for log + lines.
        now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        base_log: _PayloadLog = {
            "model_id": int(model_id),
            "model_name": model_rec.name,
            "model_model": model_rec.model,
            "user_id": int(uid),
            "method": method,
            # Intentionally not creating auditlog
            # HTTP PG tables in the write-only module.
            "http_request_id": None,
            "http_session_id": None,
            "log_type": log_type,
            "create_date": now_iso,
            "create_uid": int(uid),
        }

        buffer_model = self.env["auditlog.log.buffer"].sudo()
        buffer_vals_list: list[dict[str, Any]] = []

        # Fast-path: export_data produces one entry, no lines.
        if method == "export_data":
            payload: _Payload = {
                "log": {
                    "id": str(uuid.uuid4()),
                    "name": res_model,
                    "res_id": None,
                    "res_ids": str(list(res_ids)),
                    **base_log,
                },
                "lines": [],
            }
            buffer_vals_list.append(
                {
                    "payload_json": json.dumps(
                        payload, ensure_ascii=False, default=_json_default
                    )
                }
            )

            try:
                buffer_model.create(buffer_vals_list)
            except Exception:
                _logger.exception(
                    "auditlog_clickhouse: buffer create failed "
                    "(export_data) (model=%s uid=%s)",
                    res_model,
                    uid,
                )
                raise

            _logger.debug(
                "auditlog_clickhouse: create_logs end "
                "(export_data) (buffer_rows=1 elapsed=%.3fs)",
                time.monotonic() - started,
            )
            return

        # Select correct line builder + field source for each method.
        # We reuse auditlog's own _prepare_* helpers to keep semantics aligned.
        line_builder: Callable[..., dict[str, Any]] | None
        values_src: tuple[Mapping[int, Mapping[str, Any]], ...]
        include_lines_on_unlink = method == "unlink" and capture_record

        if method == "create":
            line_builder = self._prepare_log_line_vals_on_create
            values_src = (new_values,)
        elif method == "read":
            line_builder = self._prepare_log_line_vals_on_read
            values_src = (old_values,)
        elif method == "write":
            line_builder = self._prepare_log_line_vals_on_write
            values_src = (old_values, new_values)
        elif include_lines_on_unlink:
            line_builder = self._prepare_log_line_vals_on_read
            values_src = (old_values,)
        else:
            line_builder = None
            values_src = ()

        total_lines = 0
        produced_payloads = 0

        for res_id in res_ids:
            log_id = str(uuid.uuid4())
            record = model_rs.browse(res_id)

            log: _PayloadLog = {
                "id": log_id,
                "name": record.display_name,
                "res_id": int(res_id),
                "res_ids": None,
                **base_log,
            }

            # Determine which fields should produce lines for this record.
            diff = DictDiffer(
                dict(new_values.get(res_id, EMPTY_DICT)),
                dict(old_values.get(res_id, EMPTY_DICT)),
            )

            if method == "create":
                fields_list: Iterable[str] = diff.added()
            elif method == "read":
                fields_list = old_values.get(res_id, EMPTY_DICT).keys()
            elif method == "write":
                fields_list = diff.changed()
            elif include_lines_on_unlink:
                fields_list = old_values.get(res_id, EMPTY_DICT).keys()
            else:
                fields_list = ()

            log_ctx = {"res_id": res_id, "model_id": model_id, "log_type": log_type}
            lines: list[_PayloadLine] = []

            if line_builder:
                for field_name in fields_list:
                    if field_name in fields_to_exclude_set:
                        continue

                    field = self._get_field(model_id, field_name)
                    if not field:
                        # Dummy / non-loggable field (no ir.model.fields row)
                        continue

                    # Reuse auditlog helper to keep the same old/new/text semantics.
                    if method in ("create", "read") or include_lines_on_unlink:
                        vals = line_builder(log_ctx, field, values_src[0])
                    else:
                        vals = line_builder(
                            log_ctx, field, values_src[0], values_src[1]
                        )

                    lines.append(
                        {
                            "id": str(uuid.uuid4()),
                            "log_id": log_id,
                            "field_id": int(field["id"]),
                            "field_name": field.get("name"),
                            "field_description": field.get("field_description"),
                            "old_value": vals.get("old_value"),
                            "new_value": vals.get("new_value"),
                            "old_value_text": vals.get("old_value_text"),
                            "new_value_text": vals.get("new_value_text"),
                            "create_date": now_iso,
                            "create_uid": int(uid),
                        }
                    )

            # Match original semantics: unlink is always logged;
            # others only if there are lines.
            if method == "unlink" or lines:
                payload = {"log": log, "lines": lines}
                buffer_vals_list.append(
                    {
                        "payload_json": json.dumps(
                            payload, ensure_ascii=False, default=_json_default
                        )
                    }
                )
                produced_payloads += 1
                total_lines += len(lines)

        if not buffer_vals_list:
            # This can legitimately happen when method != unlink and there are no
            # changed fields after exclusions; still useful to know during debugging.
            _logger.debug(
                "auditlog_clickhouse: no payloads produced "
                "(model=%s method=%s res_ids=%s excluded=%s capture_record=%s)",
                res_model,
                method,
                len(res_ids),
                len(fields_to_exclude_set),
                capture_record,
            )
            return

        try:
            # Batch insert into PostgreSQL buffer to minimize ORM overhead.
            buffer_model.create(buffer_vals_list)
        except Exception:
            _logger.exception(
                "auditlog_clickhouse: buffer create failed "
                "(model=%s method=%s uid=%s payloads=%s lines=%s)",
                res_model,
                method,
                uid,
                produced_payloads,
                total_lines,
            )
            raise

        _logger.debug(
            "auditlog_clickhouse: create_logs end (model=%s method=%s "
            "payloads=%s lines=%s res_ids=%s elapsed=%.3fs)",
            res_model,
            method,
            produced_payloads,
            total_lines,
            len(res_ids),
            time.monotonic() - started,
        )
