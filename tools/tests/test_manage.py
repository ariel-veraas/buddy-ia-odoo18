"""Local installer contracts; no Docker or Odoo operations are executed."""
import configparser
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import types
import unittest
from unittest.mock import patch

import yaml

AUDIT = Path(tempfile.gettempdir()).resolve()
PROJECT = Path(__file__).resolve().parents[2]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MANAGE = load("installer_contract", PROJECT / "tools/manage.py")
BOOTSTRAP = load("bootstrap_contract", PROJECT / "tools/bootstrap.py")


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="installer-", dir=AUDIT)
        self.root = Path(self.temporary.name).resolve()
        assert self.root.is_relative_to(AUDIT.resolve())
    def tearDown(self):
        assert self.root.is_relative_to(AUDIT.resolve())
        self.temporary.cleanup()

    def test_prepare_random_and_repeat_preserves_all_credentials(self):
        first = MANAGE.prepare(self.root)
        original = (self.root / ".env").read_bytes()
        second = MANAGE.prepare(self.root)
        self.assertEqual(first, second)
        self.assertEqual(original, (self.root / ".env").read_bytes())
        self.assertEqual(len({first[k] for k in MANAGE.SECRET_KEYS}), 6)
        self.assertTrue(all(len(first[k]) >= 32 for k in MANAGE.SECRET_KEYS))
        config = configparser.RawConfigParser()
        config.read(self.root / ".config/odoo.conf")
        self.assertEqual(config["options"]["db_password"], first["POSTGRES_PASSWORD"])
        self.assertEqual(config["options"]["dbfilter"], "^buddy_v2$")
        self.assertEqual(config["options"]["addons_path"], "/opt/buddy/addons")
        self.assertEqual(config["options"]["list_db"], "False")

    def test_prepare_cli_does_not_print_credentials(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(MANAGE.main(["--root", str(self.root), "prepare"]), 0)
        values = MANAGE.read_settings(self.root)
        self.assertTrue(all(values[k] not in output.getvalue() for k in MANAGE.SECRET_KEYS))

    def test_short_existing_credential_rejected_without_value_echo(self):
        values = MANAGE.prepare(self.root)
        bad = "short-local-test-value"
        contents = (self.root / ".env").read_text().replace(values["POSTGRES_PASSWORD"], bad)
        (self.root / ".env").write_text(contents)
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(MANAGE.main(["--root", str(self.root), "prepare"]), 1)
        self.assertNotIn(bad, output.getvalue())

    def test_demo_sequence_stops_web_bootstraps_then_exposes(self):
        for file in ("tools/bootstrap.py", "demo/seed_demo.py"):
            target = self.root / file
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / file, target)
        calls = []
        with patch.object(MANAGE, "require_docker"), patch.object(MANAGE, "run",
                side_effect=lambda root, args, label, demo=False, script=None: calls.append((args, script))):
            with contextlib.redirect_stdout(io.StringIO()):
                MANAGE.install(self.root, demo=True)
        self.assertEqual(calls[0][0], ["stop", "odoo"])
        self.assertEqual(calls[-1][0], ["up", "-d", "--no-deps", "odoo"])
        runs = [call for call in calls if call[0][0] == "run"]
        self.assertEqual(len(runs), 3)
        self.assertTrue(all("--no-http" in args for args, _ in runs))
        self.assertTrue(all("--service-ports" not in args for args, _ in runs))
        self.assertIn("bootstrap(env)", runs[1][1])
        self.assertIn("BUDDY_DEMO_ALLOWED=true", runs[2][0])

    def test_validate_uses_only_separate_database_and_never_drops_data(self):
        calls = []
        with patch.object(MANAGE, "require_docker"), patch.object(MANAGE, "run",
                side_effect=lambda root, args, label, demo=False, script=None: calls.append(args)):
            MANAGE.validate(self.root)
        command = calls[-1]
        self.assertEqual(command[command.index("-d") + 1], "buddy_test")
        self.assertIn("--no-http", command)
        self.assertFalse(any("--volumes" in args or "down" in args for args in calls))

    def test_bootstrap_uses_generated_env_password_and_prints_no_secret(self):
        values = MANAGE.prepare(self.root)
        writes, commits = [], []
        class Admin:
            def with_context(self, **kwargs): return self
            def write(self, vals): writes.append(vals)
        params = {}
        class Params:
            def sudo(self): return self
            def set_param(self, key, value): params[key] = value
        class FakeEnv(types.SimpleNamespace):
            def __getitem__(self, model): return Params()
        fake_env = FakeEnv(cr=types.SimpleNamespace(dbname="buddy", commit=lambda: commits.append(True)),
                           ref=lambda name: Admin())
        output = io.StringIO()
        with patch.dict(os.environ, {"BUDDY_BOOTSTRAP_DATABASE":"buddy", "BUDDY_ADMIN_PASSWORD":values["BUDDY_ADMIN_PASSWORD"]}), \
                contextlib.redirect_stdout(output):
            # Odoo 18 Shell.console sets __name__ and injects env before executing stdin.
            scope = {"env":fake_env, "__name__":"__main__"}
            exec((PROJECT / "tools/bootstrap.py").read_text(encoding="utf-8"), scope)
        self.assertEqual(writes[0]["password"], values["BUDDY_ADMIN_PASSWORD"])
        self.assertEqual(commits, [True])
        self.assertEqual(params, {"auth_signup.invitation_scope": "b2b"})
        self.assertNotIn(values["BUDDY_ADMIN_PASSWORD"], output.getvalue())

    def test_bootstrap_refuses_different_database(self):
        fake_env = types.SimpleNamespace(cr=types.SimpleNamespace(dbname="other"))
        with patch.dict(os.environ, {"BUDDY_BOOTSTRAP_DATABASE":"buddy"}), self.assertRaises(ValueError):
            BOOTSTRAP.bootstrap(fake_env)

    def test_compose_yaml_and_build_defenses(self):
        content = yaml.safe_load((PROJECT / "docker-compose.yml").read_text())
        services = content["services"]
        self.assertEqual(services["odoo"]["ports"], ["127.0.0.1:8070:8069"])
        self.assertEqual(services["stub-llm"]["ports"], ["127.0.0.1:9101:9100"])
        self.assertEqual(services["stub-llm"]["profiles"], ["demo"])
        # El servicio permanente no lleva contraseñas de usuarios ni la clave del proveedor demo.
        self.assertFalse([key for key in services["odoo"]["environment"] if key.startswith("BUDDY_")])
        self.assertEqual(services["odoo-setup"]["profiles"], ["setup"])
        self.assertNotIn("ports", services["odoo-setup"])
        self.assertNotIn("ports", services["db"])
        self.assertIn("?Run", services["db"]["environment"]["POSTGRES_PASSWORD"])
        docker = (PROJECT / "docker/Dockerfile").read_text()
        self.assertIn("--ignore-installed", docker)
        self.assertIn("-c /tmp/requirements.lock", docker)
        # One self-contained addon: no vendored odoo-llm modules, patches or pgvector.
        self.assertNotIn("vendor", docker)
        self.assertNotIn("patches", docker)
        self.assertIn("COPY odoo-addons/buddy_ia /opt/buddy/addons/buddy_ia", docker)
        self.assertNotIn("codeload", docker)
        self.assertNotIn("curl ", docker)


if __name__ == "__main__":
    unittest.main(verbosity=2)
