# Runbook: Odoo adapter live test (local, disposable)

Use only the disposable local Odoo from the [S2 runbook](s2-llm-evaluation.md) (port 8070,
synthetic data). Never point these steps at a shared or real Odoo.

## One-time setup

1. Install the coupon modules (needs the admin API key, `DISPOSABLE_ODOO_API_KEY`):
   call `ir.module.module.button_immediate_install` with the id of `website_sale_loyalty`
   (it pulls in `loyalty` and `sale_loyalty`). See F-042.
2. Create the read-only role in the Odoo database container (use your own password):

   ```sh
   docker exec -i s2-llm-smoke-odoo-db-1 psql -U odoo -d odoo <<SQL
   CREATE ROLE odoo_ro LOGIN PASSWORD '<password>' NOSUPERUSER NOCREATEDB NOCREATEROLE;
   GRANT CONNECT ON DATABASE odoo TO odoo_ro;
   GRANT USAGE ON SCHEMA public TO odoo_ro;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO odoo_ro;
   SQL
   ```

   Use `ALTER ROLE` instead of `CREATE ROLE` if the role exists.

## Run the live test

```sh
set -a; . ./.env; set +a
export ODOO_URL=http://127.0.0.1:8070 ODOO_DB_NAME=odoo ODOO_INTEGRATION=YES
export ODOO_API_KEY="$DISPOSABLE_ODOO_API_KEY"
# The Postgres container IP on the Docker network (the port is not published)
export ODOO_READONLY_DATABASE_URL=postgresql://odoo_ro:<password>@<db-container-ip>:5432/odoo
uv run pytest -q tests/test_engine_odoo.py tests/test_engine_odoo_live.py
```

The live test creates a synthetic partner, an aged website cart, an activity and a coupon, then
removes the cart, partner and activity and archives the coupon program and card. Without
`ODOO_INTEGRATION=YES` the live tests skip; CI does not run them.
