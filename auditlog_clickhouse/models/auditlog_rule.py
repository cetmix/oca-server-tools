import logging
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
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


def _json_default(obj: Any) -> str:
    """json.dumps(default=...) helper.

    Keeps the payload JSON-friendly even if auditlog values contain datetime/date.
    """
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    return str(obj)


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

    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")

    if isinstance(obj, models.BaseModel):
        return list(obj.ids)

    if isinstance(obj, Mapping):
        return {str(k): _json_sanitize(v) for k, v in obj.items()}

    if isinstance(obj, (list | tuple | set)):
        return [_json_sanitize(v) for v in obj]

    return _json_default(obj)


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
        model_id = self.pool._auditlog_model_cache.get(res_model)
        if model_id:
            return int(model_id)
        return int(self.env["ir.model"].sudo()._get(res_model).id)

    def _build_base_log(
        self,
        *,
        uid: int,
        method: str,
        model_id: int,
        log_type: Any,
        now_iso: str,
    ) -> _PayloadLog:
        """
        Build base (common) payload for the `log` part of ClickHouse audit entries.

        The returned dict is later merged into per-record log data, and contains
        denormalized model metadata and common fields.

        Args:
            uid: Acting user id.
            method: Audited operation (create, read, write, unlink, export_data).
            model_id: `ir.model` id for the audited model.
            log_type: Auditlog rule log type (e.g. "full" / "fast") or None.
            now_iso: UTC ISO timestamp string used for `create_date`.

        Returns:
            A dict compatible with `_PayloadLog`.
        """
        model_rec = self.env["ir.model"].sudo().browse(model_id)
        return {
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

    def _build_export_payload(
        self,
        *,
        res_model: str,
        res_ids: Sequence[int],
        base_log: _PayloadLog,
    ) -> _Payload:
        """
        Build a payload for the `export_data` audit method.

        Args:
            res_model: Technical model name.
            res_ids: Record ids being exported.
            base_log: Common log payload built by `_build_base_log()`.

        Returns:
            Full payload dict to be JSON-serialized and written into buffer.
        """
        return {
            "log": {
                "id": str(uuid.uuid4()),
                "name": res_model,
                "res_id": None,
                "res_ids": str(list(res_ids)),
                **base_log,
            },
            "lines": [],
        }

    def _select_line_builder(
        self,
        *,
        method: str,
        capture_record: bool,
        old_values: Mapping[int, Mapping[str, Any]],
        new_values: Mapping[int, Mapping[str, Any]],
    ) -> tuple[
        Callable[..., dict[str, Any]] | None,
        tuple[Mapping[int, Mapping[str, Any]], ...],
        bool,
    ]:
        """
        Select auditlog line builder and value sources for the given method.

        Args:
            method: Audited operation ("create", "read", "write", "unlink", ...).
            capture_record: Rule flag that enables capturing record values on unlink
            old_values: Mapping `{res_id: {field: value}}` captured before the operation
            new_values: Mapping `{res_id: {field: value}}` captured after the operation

        Returns:
            A tuple of:
              - line_builder: Callable used to build a single log line values dict,
                or None if lines should not be produced for this method.
              - values_src: Tuple containing old/new mappings passed to `line_builder`.
              - include_lines_on_unlink: Whether unlink should produce lines.
        """
        include_lines_on_unlink = method == "unlink" and capture_record

        if method == "create":
            return (
                self._prepare_log_line_vals_on_create,
                (new_values,),
                include_lines_on_unlink,
            )
        if method == "read":
            return (
                self._prepare_log_line_vals_on_read,
                (old_values,),
                include_lines_on_unlink,
            )
        if method == "write":
            return (
                self._prepare_log_line_vals_on_write,
                (old_values, new_values),
                include_lines_on_unlink,
            )
        if include_lines_on_unlink:
            return (
                self._prepare_log_line_vals_on_read,
                (old_values,),
                include_lines_on_unlink,
            )

        return None, (), include_lines_on_unlink

    def _fields_list_for_record(
        self,
        *,
        method: str,
        include_lines_on_unlink: bool,
        res_id: int,
        old_values: Mapping[int, Mapping[str, Any]],
        new_values: Mapping[int, Mapping[str, Any]],
    ) -> Iterable[str]:
        """
        Determine which field names should be turned into audit lines for a record.

        Args:
            method: Audited operation.
            include_lines_on_unlink: True when unlink lines are enabled by the rule.
            res_id: Record id being processed.
            old_values: Mapping `{res_id: {field: value}}` captured before operation.
            new_values: Mapping `{res_id: {field: value}}` captured after operation.

        Returns:
            Iterable of field technical names to process into payload lines.
        """
        diff = DictDiffer(
            dict(new_values.get(res_id, EMPTY_DICT)),
            dict(old_values.get(res_id, EMPTY_DICT)),
        )

        if method == "create":
            return diff.added()
        if method == "read" or include_lines_on_unlink:
            return old_values.get(res_id, EMPTY_DICT).keys()
        if method == "write":
            return diff.changed()
        return ()

    def _build_lines_for_record(
        self,
        *,
        uid: int,
        now_iso: str,
        model_id: int,
        log_id: str,
        log_ctx: dict[str, Any],
        method: str,
        include_lines_on_unlink: bool,
        line_builder: Callable[..., dict[str, Any]] | None,
        values_src: tuple[Mapping[int, Mapping[str, Any]], ...],
        fields_list: Iterable[str],
        fields_to_exclude_set: set[str],
    ) -> list[_PayloadLine]:
        """
        Build payload line entries for a single audited record.

        Args:
            uid: Acting user id.
            now_iso: UTC ISO timestamp string used for line `create_date`.
            model_id: `ir.model` id for the audited model.
            log_id: UUID of the parent log entry.
            log_ctx: Context dict passed to auditlog helper.
            method: Audited operation.
            include_lines_on_unlink: Whether unlink should be treated as read for lines.
            line_builder: Selected builder callable, or None to return no lines.
            values_src: Tuple of source mappings passed into `line_builder`.
            fields_list: Field names selected by `_fields_list_for_record()`.
            fields_to_exclude_set: Set of field names to ignore.

        Returns:
            List of `_PayloadLine` dicts.
        """
        if not line_builder:
            return []

        one_source = method in ("create", "read") or include_lines_on_unlink
        lines: list[_PayloadLine] = []

        for field_name in fields_list:
            if field_name in fields_to_exclude_set:
                continue

            field = self._get_field(model_id, field_name)
            if not field:
                continue  # Dummy / non-loggable field

            if one_source:
                vals = line_builder(log_ctx, field, values_src[0])
            else:
                vals = line_builder(log_ctx, field, values_src[0], values_src[1])

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

        return lines

    def _dump_payload_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        Prepare payload for storing in the PostgreSQL buffer.

        Buffer field is `fields.Json`, so we store a dict, not a JSON string.
        We sanitize values to ensure the structure is JSON-serializable.
        """
        return _json_sanitize(payload)

    def _buffer_create_or_log(
        self,
        *,
        buffer_model,
        buffer_vals_list: list[dict[str, Any]],
        on_fail_msg: str,
        on_fail_args: tuple[Any, ...],
    ) -> None:
        """
        Create buffer rows and log a consistent exception message on failure.

        Args:
            buffer_model: Recordset of `auditlog.log.buffer` (typically sudo()).
            buffer_vals_list: List of dicts passed to `create()`.
            on_fail_msg: Logger message template used on exception.
            on_fail_args: Arguments for the logger template.

        Raises:
            Any exception raised by `buffer_model.create()` is re-raised.
        """
        try:
            buffer_model.create(buffer_vals_list)
        except Exception:
            _logger.exception(on_fail_msg, *on_fail_args)
            raise

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
        """Write audit logs to ClickHouse buffer instead of PostgreSQL audit tables."""
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

        model_id = self._get_audit_model_id(res_model)
        model_rs = self.env[res_model]
        fields_to_exclude_set, capture_record = self._get_rule_settings(model_id)

        now_iso = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        base_log = self._build_base_log(
            uid=uid,
            method=method,
            model_id=model_id,
            log_type=log_type,
            now_iso=now_iso,
        )

        buffer_model = self.env["auditlog.log.buffer"].sudo()
        buffer_vals_list: list[dict[str, Any]] = []

        if method == "export_data":
            payload = self._build_export_payload(
                res_model=res_model, res_ids=res_ids, base_log=base_log
            )
            buffer_vals_list.append({"payload_json": self._dump_payload_json(payload)})

            self._buffer_create_or_log(
                buffer_model=buffer_model,
                buffer_vals_list=buffer_vals_list,
                on_fail_msg=(
                    "auditlog_clickhouse: buffer create failed "
                    "(export_data) (model=%s uid=%s)"
                ),
                on_fail_args=(res_model, uid),
            )

            _logger.debug(
                "auditlog_clickhouse: create_logs end "
                "(export_data) (buffer_rows=1 elapsed=%.3fs)",
                time.monotonic() - started,
            )
            return

        line_builder, values_src, include_lines_on_unlink = self._select_line_builder(
            method=method,
            capture_record=capture_record,
            old_values=old_values,
            new_values=new_values,
        )

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

            fields_list = self._fields_list_for_record(
                method=method,
                include_lines_on_unlink=include_lines_on_unlink,
                res_id=res_id,
                old_values=old_values,
                new_values=new_values,
            )

            log_ctx = {"res_id": res_id, "model_id": model_id, "log_type": log_type}
            lines = self._build_lines_for_record(
                uid=uid,
                now_iso=now_iso,
                model_id=model_id,
                log_id=log_id,
                log_ctx=log_ctx,
                method=method,
                include_lines_on_unlink=include_lines_on_unlink,
                line_builder=line_builder,
                values_src=values_src,
                fields_list=fields_list,
                fields_to_exclude_set=fields_to_exclude_set,
            )

            if method == "unlink" or lines:
                buffer_vals_list.append(
                    {
                        "payload_json": self._dump_payload_json(
                            {"log": log, "lines": lines}
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

        self._buffer_create_or_log(
            buffer_model=buffer_model,
            buffer_vals_list=buffer_vals_list,
            on_fail_msg=(
                "auditlog_clickhouse: buffer create failed "
                "(model=%s method=%s uid=%s payloads=%s lines=%s)"
            ),
            on_fail_args=(res_model, method, uid, produced_payloads, total_lines),
        )

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
