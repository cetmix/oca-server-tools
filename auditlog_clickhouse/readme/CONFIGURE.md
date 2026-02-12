- Make sure `clickhouse-driver` is available in your system.
- Install the module.
- Configure the connection parameters in Odoo:
  - **Settings > Technical > Auditlog > Clickhouse configuration**
  - Fill in the following parameters:

| Field |
|:-----|
| Hostname or IP |
| TCP port |
| ClickHouse database name |
| ClickHouse user |
| ClickHouse Password |

- Click **Test connection**.
- Optionally, click **Create Auditlog Tables** to create the tables and User in the target database.
