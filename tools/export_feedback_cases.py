"""Turn the 👎 votes that came with their question into DRAFT cases for the golden set (backlog item E3).

Who votes and what is kept: see docs/SECURITY.md. A vote never says who gave it, and the text of the question is only
there when the person ticked «Incluir mi pregunta para mejorar a Buddy» (or the administrator keeps every question).

What this tool does
* Reads, through `odoo shell` in the one-shot `odoo-setup` container (like tools/eval_suite.py; nothing else is touched,
  nothing is written to the database), the rows of `buddy.feedback` with value «down» and a question.
* Writes a JSON file with the format of tests/eval/golden.json: one DRAFT case per distinct question, with the question
  as the only message and NO expectations. A draft is not a test: a person reads the question, finds the right answer in
  the documents, writes the expectations (see «Cómo agregar casos» in docs/EVALUACION.md), fixes the category and then
  moves the case into golden.json. `intent` says what the votes told us (reason, documents, how many times, day).

The questions are free text written by employees: look at the file before sharing it or putting it anywhere that is not
this folder, and delete personal data from the texts. Nobody's name is in it, but a question can contain one.

    .venv\\Scripts\\python.exe tools/export_feedback_cases.py
    .venv\\Scripts\\python.exe tools/export_feedback_cases.py --since 30 --output tests/eval/feedback_cases.draft.json
    .venv\\Scripts\\python.exe tools/export_feedback_cases.py --from-file rows.json     # no Docker: rows already exported
"""
import argparse
import base64
import datetime
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "tests/eval/feedback_cases.draft.json"
MARKER = "@@FEEDBACK@@ "
MAX_CASES = 100
REASON_LABELS = {"not_asked": "no era lo que preguntó", "wrong": "dato incorrecto o desactualizado", "incomplete": "incompleta",
                 "other": "otro motivo"}

# Runs inside `odoo shell` (the variable `env` exists there). Read-only. Placeholder: __PAYLOAD__ (base64 of the JSON job).
CONTAINER_SCRIPT = r'''
import base64, json, datetime
JOB = json.loads(base64.b64decode("__PAYLOAD__").decode("utf-8"))
domain = [("value", "=", "down"), ("question", "!=", False)]
if JOB["since_days"]:
    domain.append(("day", ">=", datetime.date.today() - datetime.timedelta(days=JOB["since_days"])))
rows = env["buddy.feedback"].sudo().search(domain, order="id desc", limit=JOB["limit"])
for row in rows:
    print("@@FEEDBACK@@ " + json.dumps({
        "question": row.question or "", "reason": row.reason or "", "day": str(row.day or ""),
        "documents": row.document_ids.mapped("name"), "model": row.model_name or ""}, ensure_ascii=True))
print("@@FEEDBACK@@ " + json.dumps({"done": len(rows)}))
'''


def load_eval_suite():
    spec = importlib.util.spec_from_file_location("eval_suite_for_export", ROOT / "tools/eval_suite.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_question(text):
    """One line, no control characters: how a question goes into a case."""
    return " ".join("".join(ch if ch.isprintable() or ch in "\n\t " else " " for ch in str(text or "")).split())


def build_cases(rows, normalize):
    """Draft cases (golden.json format) from exported rows: one per distinct question, the most voted first.

    `rows`: dicts with question, reason, day, documents and model. `normalize` is eval_suite.normalize (the same
    comparison the harness uses, so the same question written with other capitals or accents counts once).
    """
    groups = {}
    for row in rows:
        question = clean_question(row.get("question"))
        if not question or len(question) > 2000:
            continue
        key = normalize(question)
        group = groups.setdefault(key, {"question": question, "count": 0, "reasons": {}, "documents": {}, "days": []})
        group["count"] += 1
        reason = row.get("reason") or ""
        if reason:
            group["reasons"][reason] = group["reasons"].get(reason, 0) + 1
        for name in row.get("documents") or []:
            group["documents"][name] = group["documents"].get(name, 0) + 1
        if row.get("day"):
            group["days"].append(str(row["day"]))
    ordered = sorted(groups.values(), key=lambda group: (-group["count"], group["question"]))[:MAX_CASES]
    cases = []
    for number, group in enumerate(ordered, 1):
        votes = f"{group['count']} voto{'s' if group['count'] != 1 else ''} 👎"
        reasons = ", ".join(REASON_LABELS.get(code, code) for code, unused in sorted(group["reasons"].items(), key=lambda item: -item[1]))
        documents = ", ".join(sorted(group["documents"]))
        days = f"{min(group['days'])}" + (f" a {max(group['days'])}" if max(group["days"]) != min(group["days"]) else "") if group["days"] else ""
        intent = ("BORRADOR desde feedback: " + votes + (f"; motivo: {reasons}" if reasons else "") +
                  (f"; la respuesta citó: {documents}" if documents else "; sin documentos citados") + (f"; {days}" if days else "") +
                  ". Falta: buscar la respuesta correcta en los documentos, escribir las expectativas y elegir la categoría.")
        cases.append({"id": f"feedback-{number:03d}", "category": "hechos", "intent": intent, "messages": [group["question"]]})
    return cases


def build_document(cases):
    return {"version": 1,
            "description": ("BORRADORES generados por tools/export_feedback_cases.py a partir de votos 👎 con la pregunta incluida. "
                            "No son pruebas: sin expectativas. Revisalos, quitá datos personales y agregá los buenos a golden.json."),
            "generated_at": datetime.date.today().isoformat(), "cases": cases}


def read_rows_from_output(output):
    """The rows printed by the container script (one JSON object after the marker); everything else is noise."""
    rows, done = [], False
    for line in output.splitlines():
        if not line.startswith(MARKER):
            continue
        try:
            data = json.loads(line[len(MARKER):])
        except ValueError:
            continue
        if "done" in data:
            done = True
        elif isinstance(data, dict):
            rows.append(data)
    return rows, done


def fetch_rows(options):
    suite = load_eval_suite()
    job = {"since_days": options.since, "limit": options.limit}
    payload = base64.b64encode(json.dumps(job).encode("ascii")).decode("ascii")
    process = subprocess.run(suite.compose_command(ROOT, options.database), input=CONTAINER_SCRIPT.replace("__PAYLOAD__", payload).encode("utf-8"),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
    rows, done = read_rows_from_output(process.stdout.decode("utf-8", errors="replace"))
    if not done:
        print("El contenedor terminó sin cerrar la lectura (código %s). Últimas líneas:" % process.returncode, file=sys.stderr)
        for line in process.stderr.decode("utf-8", errors="replace").splitlines()[-6:]:
            print("   | " + line[:200], file=sys.stderr)
        return None
    return rows


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Exporta los votos 👎 con pregunta como borradores de casos del conjunto dorado.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="dónde escribir el JSON (tests/eval/feedback_cases.draft.json)")
    parser.add_argument("--since", type=int, default=0, help="solo los últimos N días (0 = todos los que haya)")
    parser.add_argument("--limit", type=int, default=500, help="máximo de votos a leer (500)")
    parser.add_argument("--database", default="buddy_v2", help="base del stack v2 (buddy_v2)")
    parser.add_argument("--from-file", metavar="FILAS.json", help="usa filas ya exportadas (una lista de objetos) en vez de leer la base")
    options = parser.parse_args(argv)
    if options.since < 0 or not 1 <= options.limit <= 10000:
        parser.error("valores fuera de rango")
    if options.database in ("buddy", "postgres"):
        parser.error("la v1 (base «buddy») no se toca: esta herramienta solo lee el stack v2")
    return options


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    options = parse_args(argv)
    suite = load_eval_suite()
    if options.from_file:
        rows = json.loads(Path(options.from_file).read_text(encoding="utf-8-sig"))
        if not isinstance(rows, list):
            print("ERROR: el archivo debe ser una lista de filas", file=sys.stderr)
            return 1
    else:
        rows = fetch_rows(options)
        if rows is None:
            return 2
    cases = build_cases([row for row in rows if isinstance(row, dict)], suite.normalize)
    document = build_document(cases)
    errors, warnings = suite.validate_golden(document) if cases else ([], [])
    if errors:
        for error in errors:
            print("ERROR:", error, file=sys.stderr)
        return 1
    target = Path(options.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(rows)} votos 👎 con pregunta leídos; {len(cases)} borradores escritos en {target}.")
    print("Son borradores sin expectativas: revisalos (y sacá datos personales) antes de pasarlos a tests/eval/golden.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
