{
    "name": "Audit Log ClickHouse store and read",
    "version": "18.0.1.0.0",
    "summary": "Asynchronous audit log storage in ClickHouse",
    "category": "Tools",
    "license": "AGPL-3",
    "author": "Odoo Community Association (OCA), Cetmix",
    "website": "https://github.com/OCA/server-tools",
    "depends": [
        "auditlog",
    ],
    "external_dependencies": {
        "python": ["clickhouse_driver"],
    },
    "data": [
        "security/ir.model.access.csv",
        "views/auditlog_clickhouse_config_views.xml",
        "data/ir_cron.xml",
    ],
    "installable": True,
}
