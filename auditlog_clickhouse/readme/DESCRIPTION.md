This module implements buffered asynchronous transfers audit of logs from PostgreSQL to ClickHouse.
Storing audit data in a columnar database that is write-only prevents database bloat, makes audit records effectively
immutable, and allows for scaling to very large volumes of logs without slowing down normal transactions.
Audit logs are written asynchronously to reduce the load on business operations.
Audit logs stored in ClickHouse are displayed in standard Odoo audit log views (logs, log lines,
forms with detailed log information) without any changes to existing view definitions.
