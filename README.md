# Buddy IA for Odoo 18

> Leer en español: [README.es.md](README.es.md) (the full documentation in `docs/` is in Spanish).

**Buddy is a floating assistant for Odoo 18 Community. Employees ask it questions and it answers with the documents in the company's Google Drive folders, citing the source of every fact and showing each person only what they are allowed to see.**

It is for teams that want "how do I do X?" to answer itself, without digging through folders or interrupting a colleague. A person who administers Odoo installs and maintains it; no programming or DBA work is needed.

> **Honest status (18.0.5.5.0):** the full flow is covered by more than 1,500 automated tests (native Odoo tests, pure Python and widget tests) and the v2 was also exercised against **real Google Drive and Gemini** on a test copy. **Not tested yet:** real Claude and OpenAI providers (the code supports them), a real screen reader or touch phone, and a large document corpus. See [docs/VALIDATION.md](docs/VALIDATION.md). Try it in a test environment first.

![Buddy answering with a source](docs/screenshots/03-respuesta-con-fuentes.png)

## What it does

- **Answers with sources.** Reads Google Drive folders (read-only, through a service account), splits the documents into fragments in Odoo's own database and searches them with Postgres full-text search, plus an optional **search by meaning** (embeddings; off by default because it sends document text to the AI provider).
- **Respects permissions.** Each Odoo group sees only its folders. The search runs with the user's own permissions (no `sudo`).
- **Remembers the conversation** (follow-ups like "and who handles it?"), **flags contradictions** between documents and says so when the evidence is weak, instead of inventing.
- **Onboarding.** HR builds journeys (day 1, week 1, month 1); each person sees "Today's tasks" in the chat; HR sees progress without reading any conversation.
- **Gentle proactivity.** Per-person suggestions, one discreet nudge a day, a weekly "what is missing in the docs" digest. No extra AI calls, nothing is spammed, everything can be switched off.
- **Anonymous feedback** (👍/👎) to learn what to document next.
- **Bring your own AI.** Claude, OpenAI, Gemini or any OpenAI-compatible server.
- **A character with personality.** Two built-in buddies (Kitsune and Mochi), accessories, colour, name and idle gestures. **A company can add its own mascot as a separate module** without touching the core: see [docs/CUSTOMIZE.md](docs/CUSTOMIZE.md) ("Un buddy propio de tu empresa") and the template in [`examples/buddy_ia_mascot_example`](examples/buddy_ia_mascot_example).

```text
 Google Drive           Odoo 18 (your server)                        AI provider
 ┌──────────┐  reads ┌─────────────────────────────┐               (Claude / OpenAI /
 │ Folders  │ ─────► │ Sync (cron)                 │                Gemini / other)
 │ (read    │        │   └► fragments in Postgres  │                      ▲
 │  only)   │        │                             │  question +          │
 └──────────┘        │ Employee ─► question ─►     │  allowed fragments   │
                     │   search (their groups) ────┼──────────────────────┘
                     │                        ◄────┼── answer + sources
                     └─────────────────────────────┘
```

Whole documents are **never** sent to the provider: only the relevant fragments of each question.

## Requirements

- A Google account with Drive (to create the service account and hold the folders): [docs/GOOGLE-DRIVE.md](docs/GOOGLE-DRIVE.md).
- An API key from an AI provider (pay-per-use, billed by the provider).
- An **Odoo 18 Community** server where you can install an addon. Odoo Online does not work (no custom addons); Odoo.sh and Enterprise were not tested.
- Nothing else: no pgvector, no Postgres extensions, no Docker in production.

## Try it in 3 steps (Docker demo)

The demo runs on your computer with fictional documents and a **simulated AI**: no cost, no keys, no real Odoo touched.

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and Python 3.12.
2. In a terminal, in this folder:

   ```text
   python tools/manage.py demo
   ```

3. Open <http://127.0.0.1:8070>. Users: `admin`, `ana` (sees General and Sales) and `leo` (General only). Passwords are generated into the private `.env` file in this folder.

Ask "how do I create a quote?" as `ana` and as `leo` and compare. Because the AI is simulated, answers are the raw document text; with a real AI they are written in natural language. Stop everything with `python tools/manage.py down` (your data is kept).

## Install in your Odoo

1. Copy `odoo-addons/buddy_ia` into a folder on your Odoo `addons_path`.
2. `pip install -r odoo-addons/requirements.lock` (pypdf, PyJWT, packaging).
3. Restart Odoo, update the apps list, install **Buddy IA** (or `odoo -d YOUR_DB -i buddy_ia --stop-after-init`).
4. Follow the [Google Drive guide](docs/GOOGLE-DRIVE.md) and configure everything under **Settings > Buddy IA**, then press **Test connection**.

Full steps, upgrading and uninstalling: [docs/INSTALL.md](docs/INSTALL.md).

## Documentation (Spanish)

| Document | What for |
|---|---|
| [INSTALL](docs/INSTALL.md) · [GOOGLE-DRIVE](docs/GOOGLE-DRIVE.md) | Install, upgrade, connect Drive |
| [ADMIN-GUIDE](docs/ADMIN-GUIDE.md) | Organising folders, quality, costs |
| [CUSTOMIZE](docs/CUSTOMIZE.md) | Settings, extending the engine, adding your own mascot |
| [SECURITY](docs/SECURITY.md) · [ARCHITECTURE](docs/ARCHITECTURE.md) | Who sees what, how it is built |
| [EVALUACION](docs/EVALUACION.md) | The evaluation harness for measuring answer quality with a real AI |
| [VALIDATION](docs/VALIDATION.md) · [CHANGELOG](docs/CHANGELOG.md) · [RISKS](docs/RISKS.md) · [ROADMAP](docs/ROADMAP.md) | What was tested, what changed, known limits |

## Repository layout

| Folder | Content |
|---|---|
| `odoo-addons/buddy_ia` | The module (the only thing installed in production) |
| `examples/buddy_ia_mascot_example` | Template module: add your own character without forking |
| `tools` | Demo installer (`manage.py`), evaluation runner, checks |
| `demo` | Fictional documents and a simulated AI server (demo only) |
| `frontend-tests` | Widget tests (`make test`) |
| `docs` | Documentation |

License: LGPL-3 (see [NOTICE.md](NOTICE.md), which also covers dependency licences).
