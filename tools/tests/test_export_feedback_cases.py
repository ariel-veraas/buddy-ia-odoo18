"""Pure tests of tools/export_feedback_cases.py: draft cases from 👎 votes, in the format of the golden set. No Docker, no Odoo."""
import ast
import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[2]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EX = load("export_feedback_cases_contract", PROJECT / "tools/export_feedback_cases.py")
EV = load("eval_suite_for_export_test", PROJECT / "tools/eval_suite.py")

ROWS = [
    {"question": "¿Cuánto tarda el reintegro?", "reason": "wrong", "day": "2026-10-01", "documents": ["Política de Gastos"], "model": "m"},
    {"question": "¿cuanto tarda el reintegro?", "reason": "incomplete", "day": "2026-10-03", "documents": ["Política de Gastos", "Guía"], "model": "m"},
    {"question": "¿Dónde está la sala?", "reason": "", "day": "2026-10-02", "documents": [], "model": "m"},
    {"question": "   ", "reason": "wrong", "day": "2026-10-02", "documents": []},
    {"question": "x" * 2001, "reason": "", "day": "2026-10-02", "documents": []},
    {"question": "Línea\u0000 con\ncontrol", "reason": "other", "day": "2026-10-04", "documents": []},
]


class BuildCasesTests(unittest.TestCase):
    def setUp(self):
        self.cases = EX.build_cases(ROWS, EV.normalize)

    def test_one_draft_per_distinct_question_the_most_voted_first(self):
        self.assertEqual([case["messages"][0] for case in self.cases],
                         ["¿Cuánto tarda el reintegro?", "Línea con control", "¿Dónde está la sala?"])
        self.assertEqual([case["id"] for case in self.cases], ["feedback-001", "feedback-002", "feedback-003"])

    def test_drafts_have_no_expectations_so_they_cannot_pass_as_tests(self):
        for case in self.cases:
            self.assertEqual(set(case), {"id", "category", "intent", "messages"})
            self.assertEqual(len(case["messages"]), 1)

    def test_the_intent_says_what_the_votes_told(self):
        intent = self.cases[0]["intent"]
        self.assertTrue(intent.startswith("BORRADOR"))
        for expected in ("2 votos", "dato incorrecto o desactualizado", "incompleta", "Guía", "Política de Gastos", "2026-10-01 a 2026-10-03"):
            self.assertIn(expected, intent)
        self.assertIn("1 voto 👎", self.cases[2]["intent"])
        self.assertIn("sin documentos citados", self.cases[2]["intent"])

    def test_blank_or_huge_questions_and_control_characters_are_dropped_or_cleaned(self):
        texts = [case["messages"][0] for case in self.cases]
        self.assertNotIn("x" * 2001, texts)
        self.assertFalse(any("\u0000" in text or "\n" in text for text in texts))

    def test_the_document_is_a_valid_golden_set_and_says_it_is_a_draft(self):
        document = EX.build_document(self.cases)
        errors, warnings = EV.validate_golden(document)
        self.assertEqual((errors, warnings), ([], []))
        self.assertIn("BORRADORES", document["description"])

    def test_nothing_that_identifies_a_person_can_come_through(self):
        # The rows never have such fields; and even if a caller adds them, the cases only ever use the known ones.
        rows = [dict(ROWS[0], user="Ana Pérez", login="ana", partner_id=7, email="ana@example.com")]
        text = json.dumps(EX.build_cases(rows, EV.normalize), ensure_ascii=False)
        for forbidden in ("Ana Pérez", "ana@example.com", "partner", "login"):
            self.assertNotIn(forbidden, text)

    def test_capped(self):
        rows = [{"question": f"pregunta distinta número {n}", "reason": "", "day": "", "documents": []} for n in range(EX.MAX_CASES + 30)]
        self.assertEqual(len(EX.build_cases(rows, EV.normalize)), EX.MAX_CASES)

    def test_no_rows_no_cases(self):
        self.assertEqual(EX.build_cases([], EV.normalize), [])


class ContainerScriptTests(unittest.TestCase):
    def test_script_is_valid_python_read_only_and_the_payload_round_trips(self):
        job = {"since_days": 30, "limit": 500}
        payload = base64.b64encode(json.dumps(job).encode("ascii")).decode("ascii")
        script = EX.CONTAINER_SCRIPT.replace("__PAYLOAD__", payload)
        ast.parse(script)
        self.assertEqual(json.loads(base64.b64decode(re.search(r'b64decode\("([A-Za-z0-9+/=]+)"\)', script).group(1))), job)
        for forbidden in (".write(", ".create(", ".unlink(", "commit", "execute(", "environ", "getenv", "api_key", "drive_key", "login", "user_id"):
            self.assertNotIn(forbidden, script)
        self.assertIn('("value", "=", "down")', script)
        self.assertIn('("question", "!=", False)', script)

    def test_rows_are_read_from_the_marker_lines_and_noise_is_ignored(self):
        output = ("odoo log\n@@FEEDBACK@@ {\"question\": \"a\", \"reason\": \"wrong\"}\nWARNING x\n@@FEEDBACK@@ {rota\n"
                  "@@FEEDBACK@@ {\"question\": \"b\"}\n@@FEEDBACK@@ {\"done\": 2}\n")
        rows, done = EX.read_rows_from_output(output)
        self.assertEqual((rows, done), ([{"question": "a", "reason": "wrong"}, {"question": "b"}], True))
        self.assertEqual(EX.read_rows_from_output("sin nada"), ([], False))


class CommandLineTests(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = EX.main(list(argv))
            except SystemExit as exit_:
                code = exit_.code
        return code, out.getvalue(), err.getvalue()

    def test_the_v1_database_is_refused(self):
        code, out, err = self.run_main("--database", "buddy")
        self.assertEqual(code, 2)
        self.assertIn("v1", err)

    def test_writes_the_draft_file_from_rows_without_docker(self):
        with tempfile.TemporaryDirectory() as folder:
            rows_file, target = Path(folder, "rows.json"), Path(folder, "out", "drafts.json")
            rows_file.write_text(json.dumps(ROWS), encoding="utf-8")
            code, out, err = self.run_main("--from-file", str(rows_file), "--output", str(target))
            self.assertEqual((code, err), (0, ""))
            self.assertIn("3 borradores", out)
            written = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(len(written["cases"]), 3)
            self.assertEqual(EV.validate_golden(written)[0], [])

    def test_a_file_that_is_not_a_list_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder, "rows.json")
            bad.write_text("{}", encoding="utf-8")
            self.assertEqual(self.run_main("--from-file", str(bad), "--output", str(Path(folder, "x.json")))[0], 1)

    def test_out_of_range_values_are_refused(self):
        self.assertEqual(self.run_main("--limit", "0")[0], 2)
        self.assertEqual(self.run_main("--since", "-1")[0], 2)

    def test_the_default_output_is_a_draft_file_next_to_the_golden_set(self):
        self.assertEqual(EX.DEFAULT_OUTPUT, PROJECT / "tests/eval/feedback_cases.draft.json")


if __name__ == "__main__":
    unittest.main()
