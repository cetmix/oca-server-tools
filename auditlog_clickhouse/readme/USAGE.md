Once auditlog_clickhouse is installed and configured:

- Users perform tracked operations (create, write, unlink, read, export) on models with active auditlog.rule subscriptions.
  This behavior is unchanged from the base auditlog module.
- Log data is serialized and stored in the local auditlog.log.buffer table instantly. The standard auditlog tables are not populated.
- Every 5 minutes (default), the Cron job runs, pushes data to ClickHouse, and cleans the local buffer.
- Data is permanently stored in ClickHouse and cannot be modified or deleted via Odoo.

All standard Odoo audit log views work as expected - logs, log lines, and forms with detailed log data display data from ClickHouse.
Search, filtering, and grouping (by user, model, date, session, query) work through FDW with the query being forwarded to ClickHouse.
The “View logs” quick access button in audited model forms works as expected.
Audit logs are read-only. Attempting to modify or delete a log entry from the user interface raises an error.
