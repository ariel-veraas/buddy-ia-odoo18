"""Idempotent local demo seed, executed through tools/manage.py demo and Odoo shell.

Uses generated BUDDY_ANA_PASSWORD/BUDDY_LEO_PASSWORD, never shared defaults.
The demo has no Google account: it loads the sample documents straight into three folders that never sync.
"""
import os
from pathlib import Path
import re

from odoo.addons.buddy_ia.models.buddy_chunker import split_text

if os.environ.get("BUDDY_DEMO_ALLOWED") != "true":
    raise ValueError("Demo seeding requires the explicit tools/manage.py demo action.")
if env.cr.dbname != os.environ.get("BUDDY_BOOTSTRAP_DATABASE"):
    raise ValueError("Demo database differs from the explicitly selected installation.")

DOCS = next((Path(p) for p in (os.environ.get("BUDDY_DOCS", ""), "/mnt/demo/docs", "demo/docs")
             if p and Path(p).is_dir()), None)
if DOCS is None:
    raise ValueError("Demo document directory is missing.")
API_BASE = os.environ.get("BUDDY_API_BASE", "http://stub-llm:9100/v1")
API_KEY = os.environ.get("BUDDY_API_KEY", "")
if not API_KEY:
    raise ValueError("A local provider key is required.")
CHAT = os.environ.get("BUDDY_CHAT_MODEL", "stub-chat")


def get_or_create(model, domain, vals, update=True):
    record = env[model].search(domain, limit=1)
    if record:
        if update:
            record.write(vals)
        return record
    identity = {item[0]: item[2] for item in domain if item[1] == "="}
    return env[model].create({**identity, **vals})


g_user = env.ref("base.group_user")
g_sales = get_or_create("res.groups", [("name", "=", "Buddy · Ventas")], {})
g_it = get_or_create("res.groups", [("name", "=", "Buddy · IT")], {})
# Spanish (Argentina) is the widget's source language; users created in English would get the en.po translation.
lang = env["res.lang"].with_context(active_test=False).search([("code", "=", "es_AR")], limit=1)
if not lang.active:
    env["base.language.install"].create({"lang_ids": [(6, 0, lang.ids)], "overwrite": False}).lang_install()
env.ref("base.user_admin").write({"groups_id": [(4, g_sales.id), (4, g_it.id)], "lang": "es_AR"})
for login, name, groups, key in (
        ("ana", "Ana (ventas)", [g_user, g_sales], "BUDDY_ANA_PASSWORD"),
        ("leo", "Leo (sin permisos extra)", [g_user], "BUDDY_LEO_PASSWORD")):
    password = os.environ.get(key, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", password):
        raise ValueError("A generated demo user password is required; no default is accepted.")
    users = env["res.users"].with_context(no_reset_password=True)
    record = users.search([("login", "=", login)], limit=1)
    values = {"name": name, "login": login, "password": password, "active": True, "lang": "es_AR",
              "groups_id": [(6, 0, [g.id for g in groups])]}
    if record:
        record.write(values)
    else:
        users.create(values)

# The simulated provider speaks the OpenAI-compatible protocol, so it is configured as "Otro".
parameters = env["ir.config_parameter"].sudo()
parameters.set_param("buddy_ia.provider", "custom")
parameters.set_param("buddy_ia.api_base", API_BASE)
parameters.set_param("buddy_ia.api_key", API_KEY)
parameters.set_param("buddy_ia.chat_model", CHAT)
parameters.set_param("buddy_ia.company", "Empresa Demo")
# The demo tickets have a person in charge (the administrator): they show their chatter and the to-do it creates.
parameters.set_param("buddy_ia.ticket_user_id", str(env.ref("base.user_admin").id))

levels = {"all": ("Conocimiento general", g_user), "ventas": ("Conocimiento de Ventas", g_sales),
          "it": ("Conocimiento de IT", g_it)}
sources = {key: get_or_create("buddy.source", [("name", "=", name)], {
    "folder_url": "https://drive.google.com/drive/folders/DemoSinDrive" + key.capitalize(),
    "group_ids": [(6, 0, [group.id])], "auto_sync": False, "state": "ok", "last_summary": "Carpeta de demostración (no se sincroniza)"})
    for key, (name, group) in levels.items()}

for path in sorted(DOCS.glob("*.md")):
    raw = path.read_text(encoding="utf-8")
    frontmatter = re.match(r"\A---\s*\n(.*?)\n---\s*\n", raw, re.S)
    head = frontmatter.group(1) if frontmatter else ""
    title_match = re.search(r"^title:\s*(.+)$", head, re.M)
    acl_match = re.search(r"^acl:\s*\[(.+?)\]", head, re.M)
    updated_match = re.search(r"^updated:\s*(\S+)", head, re.M)
    title = title_match.group(1).strip() if title_match else path.stem
    level = acl_match.group(1).split(",")[0].strip() if acl_match else "all"
    if level not in sources:
        raise ValueError("Unknown access level in a demo document.")
    body = raw[frontmatter.end():] if frontmatter else raw
    pieces = split_text(body)
    if not pieces:
        raise ValueError("A demo document has no text.")
    document = get_or_create("buddy.document", [("source_id", "=", sources[level].id), ("drive_id", "=", "demo-" + path.stem)], {
        "name": title, "modified_time": (updated_match.group(1) if updated_match else "2026-01-01") + "T00:00:00Z",
        "mime_type": "text/markdown", "state": "ready"})
    document.chunk_ids.unlink()
    env["buddy.chunk"].create([{"document_id": document.id, "sequence": index, "content": piece}
                               for index, piece in enumerate(pieces)])

env.cr.commit()
print("Demo seed completed: users ana/leo and three access levels. Credentials remain in the local .env.")
