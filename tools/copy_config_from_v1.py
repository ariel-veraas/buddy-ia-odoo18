"""One-time helper: copy the AI provider settings, the Google service account and the real Drive folder from the
frozen v1 database into the v2 database, so nobody has to type the secrets again.

Reads v1 (read-only SELECTs through `docker exec`), writes v2. Secrets travel through pipes only: they are never
printed, logged or written to a file. Safe to run again (upserts). Needs both stacks running.
Usage (from the v2 folder): python tools/copy_config_from_v1.py
"""
import json
import secrets
import subprocess
import sys

V1_DB_CONTAINER, V1_DB = "odoo-buddy-db-1", "buddy"
V2_DB_CONTAINER, V2_DB = "odoo-buddy-v2-db-1", "buddy_v2"
SKIP = {"buddy_ia.last_test", "buddy_ia.last_test_detail", "buddy_ia.show_advanced"}
# Development overrides for v2 (the real model that was verified, and a roomier per-person daily limit for testing).
OVERRIDES = {"buddy_ia.chat_model": "gemini-2.5-flash", "buddy_ia.rate_per_day": "400"}
SOURCE_NAME = "test"


def psql(container, db, sql, stdin=None):
    cmd = ["docker", "exec", "-i", container, "psql", "-U", "odoo", "-d", db, "-v", "ON_ERROR_STOP=1", "-At"]
    cmd += ["-c", sql] if stdin is None else []
    result = subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        sys.exit("psql failed (%s); details withheld to keep secrets out of the output." % container)
    return result.stdout.strip()


def quote(value):
    tag = "q" + secrets.token_hex(6)
    return "$%s$%s$%s$" % (tag, value, tag)


def main():
    raw = psql(V1_DB_CONTAINER, V1_DB, "select coalesce(json_object_agg(key, value), '{}'::json) "
                                       "from ir_config_parameter where key like 'buddy_ia.%'")
    params = {k: v for k, v in json.loads(raw).items() if k not in SKIP}
    params.update(OVERRIDES)
    if not params.get("buddy_ia.api_key") or not params.get("buddy_ia.drive_key"):
        sys.exit("v1 has no API key or no Drive key to copy.")
    statements = ["begin;"]
    for key, value in sorted(params.items()):
        statements.append(
            "insert into ir_config_parameter (key, value, create_date, write_date) values (%s, %s, now(), now()) "
            "on conflict (key) do update set value = excluded.value, write_date = now();" % (quote(key), quote(value)))
    statements.append("commit;")
    psql(V2_DB_CONTAINER, V2_DB, None, stdin="\n".join(statements))
    folder = psql(V1_DB_CONTAINER, V1_DB, "select json_build_object('url', folder_url, 'name', name) "
                                          "from buddy_source where name = %s limit 1" % quote(SOURCE_NAME))
    print("Copied %d settings (api key and Drive key included, not shown)." % len(params))
    print("SOURCE_JSON=" + folder)


if __name__ == "__main__":
    main()
