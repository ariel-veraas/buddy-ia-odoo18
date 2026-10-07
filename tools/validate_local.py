"""Local checks. This runner does not certify installation in Odoo/PostgreSQL."""
import ast
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = {"node_modules", "__pycache__", ".git", "legacy", ".claude", ".venv"}


def source_files(suffix):
    return sorted(path for path in ROOT.rglob("*" + suffix) if not EXCLUDE.intersection(path.parts))


def structural():
    python_files, xml_files = source_files(".py"), source_files(".xml")
    for path in python_files:
        ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for path in xml_files:
        ET.parse(path)
    manifests = list((ROOT / "odoo-addons").glob("*/__manifest__.py"))
    modules = {path.parent.name for path in manifests}
    core = {"base", "web", "mail", "base_setup"}
    for path in manifests:
        metadata = ast.literal_eval(path.read_text(encoding="utf-8"))
        assert not set(metadata.get("depends", [])) - modules - core, f"Missing dependency in {path}"
        for relative in metadata.get("data", []):
            assert (path.parent / relative).is_file(), f"Missing data file: {path.parent / relative}"
        if path.parent.name == "buddy_ia":
            for entries in metadata.get("assets", {}).values():
                for relative in entries:
                    assert (path.parent.parent / relative).is_file(), f"Missing asset: {relative}"
    print(f"PASS syntax: {len(python_files)} Python, {len(xml_files)} XML; {len(manifests)} addon manifests/dependencies/assets.", flush=True)


def main():
    structural()
    commands = [
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "odoo-addons/buddy_ia/tests", "-p", "test_drive_client.py", "-v"]),
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "odoo-addons/buddy_ia/tests", "-p", "test_chunker.py", "-v"]),
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "odoo-addons/buddy_ia/tests", "-p", "test_text.py", "-v"]),
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "odoo-addons/buddy_ia/tests", "-p", "test_stream.py", "-v"]),
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "odoo-addons/buddy_ia/tests", "-p", "test_vectors.py", "-v"]),
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "odoo-addons/buddy_ia/tests", "-p", "test_look.py", "-v"]),
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "odoo-addons/buddy_ia/tests", "-p", "test_journey_logic.py", "-v"]),
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "odoo-addons/buddy_ia/tests", "-p", "test_proactive_logic.py", "-v"]),
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "odoo-addons/buddy_ia/tests", "-p", "test_digest_logic.py", "-v"]),
        (ROOT, [sys.executable, "tools/check_backend.py"]),
        (ROOT, [sys.executable, "tools/check_boundary.py"]),
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "tools/tests", "-p", "test_manage.py", "-v"]),
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "tools/tests", "-p", "test_eval_suite.py", "-v"]),
        (ROOT, [sys.executable, "-m", "unittest", "discover", "-s", "tools/tests", "-p", "test_export_feedback_cases.py", "-v"]),
    ]
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node is required for the OWL suite; install Node and run npm ci in frontend-tests.")
    test_files = [str(path.relative_to(ROOT / "frontend-tests")) for path in sorted((ROOT / "frontend-tests/tests").glob("*.test.js"))]
    commands.append((ROOT / "frontend-tests", [node, "--import", "./register.mjs", "--test"] + test_files))
    for directory, command in commands:
        subprocess.run(command, cwd=directory, check=True)
    print("PASS all local suites. Native Odoo/PostgreSQL/Docker and real-provider validation remain separate.")


if __name__ == "__main__":
    main()
