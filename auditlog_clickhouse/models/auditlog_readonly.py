from odoo import api, models
from odoo.exceptions import UserError


def _is_clickhouse_readonly_mode(env) -> bool:
    """Return True when ClickHouse mode is active and FDW read is enabled."""
    config = env["auditlog.clickhouse.config"].sudo().get_active_config()
    return bool(config and config.fdw_enabled)


def _raise_clickhouse_readonly(env) -> None:
    """Raise a localized UserError for read-only audit log mode."""
    raise UserError(env._("Audit logs are read-only (stored in ClickHouse)."))


class AuditlogLogReadonly(models.Model):
    _inherit = "auditlog.log"

    @api.model_create_multi
    def create(self, vals_list):
        if _is_clickhouse_readonly_mode(self.env):
            _raise_clickhouse_readonly(self.env)
        return super().create(vals_list)

    def write(self, vals):
        if _is_clickhouse_readonly_mode(self.env):
            _raise_clickhouse_readonly(self.env)
        return super().write(vals)

    def unlink(self):
        if _is_clickhouse_readonly_mode(self.env):
            _raise_clickhouse_readonly(self.env)
        return super().unlink()


class AuditlogLogLineReadonly(models.Model):
    _inherit = "auditlog.log.line"

    @api.model_create_multi
    def create(self, vals_list):
        if _is_clickhouse_readonly_mode(self.env):
            _raise_clickhouse_readonly(self.env)
        return super().create(vals_list)

    def write(self, vals):
        if _is_clickhouse_readonly_mode(self.env):
            _raise_clickhouse_readonly(self.env)
        return super().write(vals)

    def unlink(self):
        if _is_clickhouse_readonly_mode(self.env):
            _raise_clickhouse_readonly(self.env)
        return super().unlink()
