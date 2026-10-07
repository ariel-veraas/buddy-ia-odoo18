"""Real Odoo HTTP/transaction checks for a DISPOSABLE seeded test database.

Requires Odoo + demo stub + modules installed. Never run on production. The configured
provider must point to the explicit local stub; this runner refuses remote AI.
Admin credentials are read from environment and never printed. Global rate
settings are restored and temporary users are removed in finally.

Example (PowerShell, after preparing the test DB):
  $env:BUDDY_TEST_ADMIN_LOGIN='admin'; $env:BUDDY_TEST_ADMIN_PASSWORD='...'
  python tests/e2e/backend_regressions.py --url http://127.0.0.1:8069 \
    --db buddy_backend_test --stub http://127.0.0.1:9100 --disposable-test-db

Needs a database seeded by `tools/manage.py demo` logic (demo/seed_demo.py); the primary demo database is not a test database.
"""
import argparse
import http.cookiejar
import json
import os
import uuid
import urllib.parse
import urllib.request
import xmlrpc.client
from concurrent.futures import ThreadPoolExecutor


class Session:
    def __init__(self, url, database, login, password):
        self.url = url.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        result = self.call("/web/session/authenticate", db=database, login=login, password=password)
        assert result.get("uid"), "Login failed"

    def call(self, route, **params):
        request = urllib.request.Request(self.url + route, data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "params": params}).encode(), headers={"Content-Type": "application/json"})
        with self.opener.open(request, timeout=90) as response:
            payload = json.load(response)
        assert "error" not in payload, "Unexpected Odoo RPC error: " + str(payload.get("error", {}).get("message"))
        return payload["result"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--stub", required=True)
    parser.add_argument("--disposable-test-db", action="store_true", required=True)
    args = parser.parse_args()
    assert "test" in args.db.lower(), "Database name must identify a test database"
    login = os.environ["BUDDY_TEST_ADMIN_LOGIN"]
    password = os.environ["BUDDY_TEST_ADMIN_PASSWORD"]
    common = xmlrpc.client.ServerProxy(args.url + "/xmlrpc/2/common")
    admin_uid = common.authenticate(args.db, login, password, {})
    assert admin_uid, "Admin authentication failed"
    rpc = xmlrpc.client.ServerProxy(args.url + "/xmlrpc/2/object")

    def call(model, method, *positional, **kwargs):
        return rpc.execute_kw(args.db, admin_uid, password, model, method, list(positional), kwargs)

    local_hosts = {"127.0.0.1", "localhost", "host.docker.internal", "stub_llm", "stub-llm", "stub"}
    stub = urllib.parse.urlparse(args.stub)
    assert stub.hostname in local_hosts, "A local stub is required"
    get_param = lambda key: call("ir.config_parameter", "get_param", key)
    endpoint = urllib.parse.urlparse(get_param("buddy_ia.api_base") or "")
    assert get_param("buddy_ia.provider") == "custom" and get_param("buddy_ia.api_key"), "No provider configured"
    assert endpoint.hostname in local_hosts and endpoint.port == stub.port, \
        "The configured provider must point to the local test stub"
    with urllib.request.urlopen(args.stub.rstrip("/") + "/_debug/seen", timeout=5) as response:
        assert "requests" in json.load(response), "Expected demo stub debug contract"

    keys = ("buddy_ia.rate_per_minute", "buddy_ia.rate_per_day")
    saved = {key: call("ir.config_parameter", "get_param", key) for key in keys}
    users = []
    try:
        call("ir.config_parameter", "set_param", keys[0], "1")
        call("ir.config_parameter", "set_param", keys[1], "1")
        group_id = call("ir.model.data", "check_object_reference", "base", "group_user")[1]
        temp_login, temp_password = "buddy-regression-" + uuid.uuid4().hex, uuid.uuid4().hex
        user_id = call("res.users", "create", {"name": "Buddy disposable QA", "login": temp_login,
                     "password": temp_password, "groups_id": [(6, 0, [group_id])]})
        users.append(user_id)
        sessions = [Session(args.url, args.db, temp_login, temp_password) for unused in range(8)]
        generation = sessions[0].call("/buddy/config")["session_generation"]
        request_ids = [str(uuid.uuid4()) for unused in sessions]

        def ask(index):
            result = sessions[index].call("/buddy/chat", message="¿Cómo solicito vacaciones?",
                session_generation=generation, request_id=request_ids[index])
            return index, result

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(ask, range(8)))
        successful = [(index, result) for index, result in results if result.get("error") is None]
        assert len(successful) == 1, "Exactly one concurrent request must be accepted at quota=1"
        assert all(result.get("error") in (None, "busy", "rate_limited", "request_pending") for index, result in results), results
        print("OK real concurrent HTTP requests: exactly one accepted at quota=1")
        winner, first = successful[0]
        repeated = sessions[winner].call("/buddy/chat", message="¿Cómo solicito vacaciones?",
            session_generation=generation, request_id=request_ids[winner])
        assert repeated == first, "The same idempotency key must return the committed response"
        print("OK same request_id returns the cached response without spending another quota")
        forgotten = sessions[winner].call("/buddy/forget")
        assert forgotten["ok"] and forgotten["session_generation"] > generation
        stale = sessions[winner].call("/buddy/chat", message="stale request", session_generation=generation,
                                     request_id=str(uuid.uuid4()))
        assert stale.get("error") == "session_changed"
        print("OK old generation cannot reopen memory after deletion")
        blocked = sessions[winner].call("/buddy/chat", message="new request after deletion",
            session_generation=forgotten["session_generation"], request_id=str(uuid.uuid4()))
        assert blocked.get("error") == "rate_limited", blocked
        print("OK forgetting conversations preserves minute/day quotas")
    finally:
        for key, value in saved.items():
            call("ir.config_parameter", "set_param", key, value)
        if users:
            call("res.users", "unlink", users)


if __name__ == "__main__":
    main()
