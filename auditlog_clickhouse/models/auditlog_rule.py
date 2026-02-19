import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, TypedDict

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


def _json_sanitize(obj: Any) -> Any:
    """
    Convert values to JSON-serializable structures.

    This is used when writing payloads into a `fields.Json` column:
      - datetime/date -> ISO string
      - recordsets -> list of ids
      - mappings/sequences -> recursively sanitized
      - other unknown types -> string representation
    """
    if obj is None or isinstance(obj, (str | int | float | bool)):
        return obj

    if isinstance(obj, (datetime | date)):
        return obj.isoformat()

    if isinstance(obj, Decimal):
        return float(obj)

    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")

    if isinstance(obj, models.BaseModel):
        return list(obj.ids)

    if isinstance(obj, Mapping):
        return {str(k): _json_sanitize(v) for k, v in obj.items()}

    if isinstance(obj, (list | tuple | set)):
        return [_json_sanitize(v) for v in obj]

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
        cache: dict[tuple[int, tuple[int, ...]], tuple[set[str], bool]] = getattr(
            self.pool, "_auditlog_clickhouse_rule_cache", {}
        )
        if not hasattr(self.pool, "_auditlog_clickhouse_rule_cache"):
            self.pool._auditlog_clickhouse_rule_cache = cache

        rules = self.filtered(lambda r: r.model_id.id == model_id)
        if not rules:
            domain = [("model_id", "=", model_id)]
            if "state" in self._fields:
                domain.append(("state", "=", "subscribed"))
            rules = self.sudo().search(domain)

        key = (model_id, tuple(sorted(rules.ids)))
        if key in cache:
            return cache[key]

        excluded: set[str] = set(FIELDS_BLACKLIST)
        capture_record = False

        if len(rules) > 1:
            _logger.warning(
                "auditlog_clickhouse: multiple rules found for model_id=%s (rules=%s); "
                "using union of excluded fields and any(capture_record).",
                model_id,
                rules.ids,
            )
        for rule in rules:
            excluded |= set(rule.fields_to_exclude_ids.mapped("name"))
            capture_record = capture_record or bool(rule.capture_record)

        cache[key] = (excluded, capture_record)
        return cache[key]

    def _get_audit_model_id(self, res_model: str) -> int:
        """
        Resolve `ir.model` id for a given model name.

        Prefer auditlog's in-memory model cache (filled by auditlog hooks) to avoid
        extra DB lookups. If cache is missing, fall back to `ir.model._get()`.

        Args:
            res_model: Technical model name (e.g. "res.partner").

        Returns:
            The `ir.model` record id for the given model name.
        """
        model_id = getattr(self.pool, "_auditlog_model_cache", {}).get(res_model)
        if model_id:
            return int(model_id)
        return int(self.env["ir.model"].sudo()._get(res_model).id)

    def _dump_payload_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _json_sanitize(payload)

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
        log_type = additional_log_values.get("log_type")

        model_id = self._get_audit_model_id(res_model)
        model_rs = self.env[res_model]
        fields_to_exclude_set, capture_record = self._get_rule_settings(model_id)

        now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        model_rec = self.env["ir.model"].sudo().browse(model_id)

        base_log: dict[str, Any] = {
            "model_id": int(model_id),
            "model_name": model_rec.name,
            "model_model": model_rec.model,
            "user_id": int(uid),
            "method": method,
            "http_request_id": None,
            "http_session_id": None,
            "log_type": log_type,
            "create_date": now_iso,
            "create_uid": int(uid),
        }

        buffer_model = (
            self.env["auditlog.log.buffer"].sudo().with_context(tracking_disable=True)
        )

        buffer_vals_list: list[dict[str, Any]] = []

        # export_data is special (no lines)
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
            buffer_vals_list.append({"payload_json": self._dump_payload_json(payload)})
            buffer_model.create(buffer_vals_list)
            _logger.debug(
                "auditlog_clickhouse: create_logs end export_data (elapsed=%.3fs)",
                time.monotonic() - started,
            )
            return

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

            diff = DictDiffer(
                dict(new_values.get(res_id, EMPTY_DICT)),
                dict(old_values.get(res_id, EMPTY_DICT)),
            )

            if method == "create":
                fields_list = diff.added()
            elif method == "read" or include_lines_on_unlink:
                fields_list = old_values.get(res_id, EMPTY_DICT).keys()
            elif method == "write":
                fields_list = diff.changed()
            else:
                fields_list = ()

            lines: list[_PayloadLine] = []
            if line_builder:
                one_source = method in ("create", "read") or include_lines_on_unlink
                log_ctx = {"res_id": res_id, "model_id": model_id, "log_type": log_type}

                for field_name in fields_list:
                    if field_name in fields_to_exclude_set:
                        continue
                    field = self._get_field(model_id, field_name)
                    if not field:
                        continue

                    if one_source:
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

            if method == "unlink" or lines:
                buffer_vals_list.append(
                    {
                        "payload_json": self._dump_payload_json(
                            {"log": log, "lines": lines}
                        )
                    }
                )

        if buffer_vals_list:
            buffer_model.create(buffer_vals_list)
