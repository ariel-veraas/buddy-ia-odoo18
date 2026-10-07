"""Manage the isolated Buddy installation on Windows, macOS and Linux."""
import argparse
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SECRET_KEYS = ("POSTGRES_PASSWORD", "ODOO_MASTER_PASSWORD", "BUDDY_ADMIN_PASSWORD",
               "BUDDY_ANA_PASSWORD", "BUDDY_LEO_PASSWORD", "BUDDY_STUB_API_KEY")
DATABASE = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


def private_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def read_settings(root):
    values = {}
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key in values:
            raise ValueError("Invalid or repeated setting in .env; credentials were not printed.")
        values[key] = value
    if any(not TOKEN.fullmatch(values.get(key, "")) for key in SECRET_KEYS):
        raise ValueError(".env requires generated passwords of at least 32 characters. Keep an existing installation's .env intact.")
    name = values.get("BUDDY_DB", "buddy_v2")
    if not DATABASE.fullmatch(name) or name in ("postgres", "template0", "template1", "buddy_test"):
        raise ValueError("BUDDY_DB must name the primary Buddy database, distinct from buddy_test.")
    values["BUDDY_DB"] = name
    return values


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    env_path = root / ".env"
    if not env_path.exists():
        values = {key: secrets.token_urlsafe(36) for key in SECRET_KEYS}
        values["BUDDY_DB"] = "buddy_v2"
        private_write(env_path, "# Generated locally. Keep private and out of archives.\n" +
                      "\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    values = read_settings(root)
    config = "\n".join([
        "[options]", "addons_path = /opt/buddy/addons", "data_dir = /var/lib/odoo",
        "db_host = db", "db_port = 5432", "db_user = odoo",
        "db_password = " + values["POSTGRES_PASSWORD"],
        "admin_passwd = " + values["ODOO_MASTER_PASSWORD"],
        "db_name = " + values["BUDDY_DB"], "dbfilter = ^" + values["BUDDY_DB"] + "$",
        "list_db = False", "proxy_mode = False", "workers = 0",
        "limit_time_cpu = 120", "limit_time_real = 180", "log_level = warn", "",
    ])
    private_write(root / ".config" / "odoo.conf", config)
    if os.name != "nt":
        # Parent directory is private on the host; container UID reads the file bind mount.
        (root / ".config").chmod(0o700)
        (root / ".config" / "odoo.conf").chmod(0o644)
    return values


def compose(root, demo=False):
    command = ["docker", "compose", "--env-file", str(root / ".env"), "-f", str(root / "docker-compose.yml"),
               "--profile", "setup"]
    if demo:
        command += ["--profile", "demo"]
    return command


def run(root, args, label, demo=False, script=None):
    print(label, flush=True)
    subprocess.run(compose(root, demo) + args, cwd=root, input=script,
                   encoding="utf-8", check=True)


def require_docker(root):
    if not (root / "docker-compose.yml").is_file():
        raise ValueError("Run this command from the extracted Buddy project.")
    if not shutil.which("docker"):
        raise ValueError("Docker Desktop or Docker Engine with Compose is required for this command.")
    # Never display `compose config`: it expands private environment values.
    subprocess.run(["docker", "compose", "version"], cwd=root, check=True, stdout=subprocess.DEVNULL)


def install(root, demo=False):
    settings = prepare(root)
    require_docker(root)
    run(root, ["stop", "odoo"], "Stopping the web service before initialization.")
    run(root, ["build", "odoo"] + (["stub-llm"] if demo else []), "Building the local addon images.", demo)
    run(root, ["up", "-d", "--wait", "db"] + (["stub-llm"] if demo else []), "Starting the private database and required provider.", demo)
    if not demo:
        run(root, ["stop", "stub-llm"], "Stopping the demo provider for the normal installation.", demo=True)
    database = settings["BUDDY_DB"]
    base = ["odoo", "--config=/etc/odoo/odoo.conf", "--no-http", "-d", database]
    run(root, ["run", "--rm", "-T", "--no-deps", "odoo"] + base +
        ["-i", "buddy_ia", "--without-demo=all", "--stop-after-init"], "Installing Buddy while HTTP is disabled.")
    shell = ["odoo", "shell", "--config=/etc/odoo/odoo.conf", "--no-http", "-d", database]
    run(root, ["run", "--rm", "-T", "--no-deps", "odoo-setup"] + shell,
        "Setting the private administrator password.", script=(root / "tools/bootstrap.py").read_text(encoding="utf-8"))
    if demo:
        run(root, ["run", "--rm", "-T", "--no-deps", "-e", "BUDDY_DEMO_ALLOWED=true",
                   "-e", "BUDDY_API_BASE=http://stub-llm:9100/v1", "odoo-setup"] + shell,
            "Loading or updating demo data without printing credentials.", demo=True,
            script=(root / "demo/seed_demo.py").read_text(encoding="utf-8"))
    run(root, ["up", "-d", "--no-deps", "odoo"], "Starting Buddy on http://127.0.0.1:8070.")
    print("Login: admin. Read BUDDY_ADMIN_PASSWORD in your local .env; it is not printed here.")


def validate(root):
    prepare(root)
    require_docker(root)
    run(root, ["build", "odoo"], "Building the test image.")
    run(root, ["up", "-d", "--wait", "db"], "Starting the database for the isolated test base.")
    run(root, ["run", "--rm", "-T", "--no-deps", "odoo", "odoo", "--config=/etc/odoo/odoo.conf",
               "--no-http", "-d", "buddy_test", "--db-filter=^buddy_test$",
               "-i", "buddy_ia", "-u", "buddy_ia", "--without-demo=all",
               "--test-enable", "--test-tags=/buddy_ia", "--stop-after-init"],
        "Running native Odoo tests only in buddy_test; the primary database is untouched.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Project directory; prepare can target a temporary directory.")
    actions = parser.add_subparsers(dest="action", required=True)
    for name, help_text in (("prepare", "Generate local credentials/config; no Docker needed."),
                            ("demo", "Initialize Buddy and demo data before exposing HTTP."),
                            ("up", "Initialize Buddy without the simulated provider or demo data."),
                            ("validate", "Run native Odoo tests in buddy_test, preserving the primary base."),
                            ("down", "Stop containers while retaining persistent volumes.")):
        actions.add_parser(name, help=help_text)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.action == "prepare":
            prepare(root)
            print("Private .env and .config/odoo.conf prepared. Existing passwords were preserved.")
        elif args.action in ("up", "demo"):
            install(root, demo=args.action == "demo")
        elif args.action == "validate":
            validate(root)
        else:
            read_settings(root)
            require_docker(root)
            run(root, ["down"], "Stopping containers; persistent volumes are retained.", demo=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, subprocess.CalledProcessError):
            print("The installation step failed; the web service only starts after bootstrap succeeds.", file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
