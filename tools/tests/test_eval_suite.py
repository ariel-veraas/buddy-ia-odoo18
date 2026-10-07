"""Pure tests of the evaluation harness (tools/eval_suite.py): normalization, golden-set validation, grading,
summaries, comparison and the batching logic. No network, no Docker, no Odoo."""
import ast
import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import re
import types
import unittest

PROJECT = Path(__file__).resolve().parents[2]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EV = load("eval_suite_contract", PROJECT / "tools/eval_suite.py")
GOLDEN = json.loads((PROJECT / "tests/eval/golden.json").read_text(encoding="utf-8-sig"))
# The full set (built on a company's real documents) is private and never published: its tests run only where it exists.
FULL_PATH = PROJECT / "tests/eval/golden.private.json"
FULL = json.loads(FULL_PATH.read_text(encoding="utf-8-sig")) if FULL_PATH.is_file() else None
needs_full = unittest.skipUnless(FULL, "needs the private full golden set (tests/eval/golden.private.json)")
SYNTHETIC = json.loads((PROJECT / "tests/eval/synthetic_docs.json").read_text(encoding="utf-8-sig"))


def turn(answer="", ring="ok", sources=(), error=None, latency=2.0, **extra):
    data = {"message": "x", "attempts": 1, "error": error, "answer": answer, "ring": ring, "sources": list(sources),
            "latency_s": latency, "secret_leak": False}
    data.update(extra)
    return data


class NormalizeTests(unittest.TestCase):
    def test_accents_case_markdown_and_spaces(self):
        self.assertEqual(EV.normalize("  **Contraseña**   de  AÑO Nuevo  "), "contrasena de ano nuevo")
        self.assertEqual(EV.normalize("`Odoo.SH` _y_ más"), "odoo.sh y mas")

    def test_idempotent_and_safe_with_none(self):
        text = "¿Cuántos días? «Sí»"
        self.assertEqual(EV.normalize(EV.normalize(text)), EV.normalize(text))
        self.assertEqual(EV.normalize(None), "")

    def test_curly_quotes_and_dashes(self):
        self.assertEqual(EV.normalize("it’s “ok” – yes"), "it's \"ok\" - yes")


class GoldenSetTests(unittest.TestCase):
    def test_shipped_golden_is_valid_and_normalized(self):
        for data in (GOLDEN, FULL):
            if data:
                errors, warnings = EV.validate_golden(data)
                self.assertEqual(errors, [])
                self.assertEqual(warnings, [])

    def test_the_public_golden_set_has_only_synthetic_cases(self):
        self.assertTrue(GOLDEN["cases"])
        self.assertTrue(all(case.get("synthetic") for case in GOLDEN["cases"]))

    @needs_full
    def test_covers_every_category_and_the_v1_questions(self):
        cases = FULL["cases"]
        self.assertEqual({case["category"] for case in cases}, set(EV.CATEGORIES))
        v1 = {case["v1_id"] for case in cases if case.get("v1_id")}
        expected = {f"{letter}{number}" for letter, top in (("a", 10), ("b", 6), ("c", 4), ("d", 4), ("e", 6), ("f", 4), ("h", 3))
                    for number in range(1, top + 1)} | {"g1", "g2", "g3", "g4"}
        self.assertEqual(v1, expected)  # g5/g6 (permissions across folders) are covered by the native tests
        by_category = {name: [c for c in cases if c["category"] == name] for name in EV.CATEGORIES}
        self.assertEqual(len(by_category["parafrasis"]), 12)
        self.assertEqual(len(by_category["seguimiento"]), 8)
        self.assertTrue(all(2 <= len(c["messages"]) <= 3 for c in by_category["seguimiento"]))
        self.assertGreaterEqual(len(by_category["tipeos"]), 5)

    @needs_full
    def test_full_run_fits_under_the_default_query_cap(self):
        self.assertLessEqual(EV.count_queries(FULL["cases"]), 150 * 0.7)  # leaves room for retries

    def test_every_case_has_a_check_that_can_fail(self):
        for case in GOLDEN["cases"]:
            checks = [key for key in ("must_contain_any", "must_contain_all", "must_not_contain", "expected_sources",
                                      "expect_needs_human", "expect_no_sources") if case.get(key) not in (None, [])]
            self.assertTrue(checks, case["id"])

    @needs_full
    def test_documents_named_in_expected_sources_exist_in_the_real_folder(self):
        known = {"pr 11", "pr 14", "pr 27", "pr 28", "pr 29", "pr 38", "pr 46"}
        for case in FULL["cases"]:
            if case.get("synthetic"):
                continue  # their documents are the synthetic ones (see SyntheticDocumentsTests)
            for entry in case.get("expected_sources") or []:
                for option in entry.split("|"):
                    self.assertIn(option, known, case["id"])

    def test_validation_catches_mistakes(self):
        def bad(mutate):
            data = copy.deepcopy(GOLDEN)
            mutate(data["cases"])
            return EV.validate_golden(data)[0]

        self.assertTrue(bad(lambda cases: cases[1].update(id=cases[0]["id"])))
        self.assertTrue(bad(lambda cases: cases[0].update(category="inventada")))
        self.assertTrue(bad(lambda cases: cases[0].update(messages=[])))
        self.assertTrue(bad(lambda cases: cases[0].update(messages=["x" * 2001])))
        self.assertTrue(bad(lambda cases: cases[0].update(must_contain_any="texto")))
        self.assertTrue(bad(lambda cases: cases[0].update(expect_needs_human="si")))
        self.assertTrue(bad(lambda cases: cases[0].update(max_latency_s=0)))
        self.assertTrue(bad(lambda cases: cases[0].update(colorin="x")))
        self.assertTrue(bad(lambda cases: cases[0].update(turn_checks={"0": {}})))  # single turn: nothing before the last
        multi = next((i for i, c in enumerate(GOLDEN["cases"]) if len(c["messages"]) > 1), None)
        if multi is not None:
            self.assertTrue(bad(lambda cases: cases[multi].update(turn_checks={"5": {"must_contain_any": ["a"]}})))
        self.assertIn("El archivo debe ser", EV.validate_golden({"cases": []})[0][0])
        self.assertTrue(EV.validate_golden([])[0])

    def test_unnormalized_expectations_are_only_a_warning(self):
        data = copy.deepcopy(GOLDEN)
        data["cases"][0]["must_contain_any"] = ["Odoo.SH"]
        errors, warnings = EV.validate_golden(data)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    @needs_full
    def test_select_cases(self):
        cases = FULL["cases"]
        self.assertEqual({c["category"] for c in EV.select_cases(cases, only=["charla"])}, {"charla"})
        self.assertEqual([c["id"] for c in EV.select_cases(cases, ids=["hechos-01", "tipeos-01"])], ["hechos-01", "tipeos-01"])
        self.assertEqual(EV.select_cases(cases, only=["charla"], ids=["hechos-01"]), [])
        self.assertEqual(len(EV.select_cases(cases)), len(cases))


class GradeTurnTests(unittest.TestCase):
    def grade(self, expect, **kwargs):
        return EV.grade_turn(expect, turn(**kwargs))

    def test_pass_with_normalized_comparison(self):
        result = self.grade({"must_contain_any": ["odoo.sh"], "expected_sources": ["pr 11"], "expect_needs_human": False},
                            answer="Se hacen en **Odoo.SH**.", sources=["PR 11 Resguardo de Información V6.docx"])
        self.assertEqual(result, {"status": "PASA", "reasons": []})

    def test_missing_required_word_fails(self):
        result = self.grade({"must_contain_any": ["100 mbps", "cien"]}, answer="Son 50 Mbps.")
        self.assertEqual(result["status"], "FALLA")
        self.assertIn("no contiene ninguno", result["reasons"][0])

    def test_forbidden_text_is_a_failure(self):
        result = self.grade({"must_not_contain": ["secreto"]}, answer="El SECRETO es 42")
        self.assertEqual(result["status"], "FALLA")
        self.assertIn("prohibido", result["reasons"][0])

    def test_credential_markers_fail_every_case(self):
        self.assertEqual(self.grade({}, answer="usá AIzaSyABC123")["status"], "FALLA")
        self.assertEqual(self.grade({}, answer="hola", secret_leak=True)["status"], "FALLA")

    def test_needs_human_verdict_both_ways(self):
        self.assertEqual(self.grade({"expect_needs_human": True}, answer="Es 5", ring="ok")["status"], "FALLA")
        self.assertEqual(self.grade({"expect_needs_human": True}, answer="No sé", ring="doubt")["status"], "PASA")
        self.assertEqual(self.grade({"expect_needs_human": False}, answer="No sé", ring="doubt")["status"], "FALLA")
        self.assertEqual(self.grade({"expect_needs_human": None}, answer="x", ring="doubt")["status"], "PASA")

    def test_sources_are_soft(self):
        result = self.grade({"expected_sources": ["pr 29", "pr 11"]}, answer="x", sources=["PR 29 Gestión de la capacidad"])
        self.assertEqual(result["status"], "PARCIAL")
        self.assertIn("pr 11", result["reasons"][0])
        # «a|b» accepts either document
        self.assertEqual(self.grade({"expected_sources": ["pr 38|pr 14"]}, answer="x", sources=["PR 14 Mantenimiento"])["status"], "PASA")
        self.assertEqual(self.grade({"expected_sources": ["pr 38"]}, answer="x", sources=[])["status"], "PARCIAL")

    def test_no_sources_expected(self):
        self.assertEqual(self.grade({"expect_no_sources": True}, answer="x", sources=["PR 11"])["status"], "PARCIAL")
        self.assertEqual(self.grade({"expect_no_sources": True}, answer="x")["status"], "PASA")

    def test_required_words_all_some_none(self):
        expect = {"must_contain_all": ["publica", "uso interno", "confidencial"]}
        self.assertEqual(self.grade(expect, answer="Pública, uso interno y confidencial")["status"], "PASA")
        partial = self.grade(expect, answer="Pública y confidencial")
        self.assertEqual(partial["status"], "PARCIAL")
        self.assertIn("uso interno", partial["reasons"][0])
        self.assertEqual(self.grade(expect, answer="nada")["status"], "FALLA")

    def test_latency_is_soft_and_none_disables_it(self):
        self.assertEqual(self.grade({"max_latency_s": 5}, answer="x", latency=7.5)["status"], "PARCIAL")
        self.assertEqual(self.grade({"max_latency_s": 5}, answer="x", latency=5.0)["status"], "PASA")
        self.assertEqual(self.grade({"max_latency_s": None}, answer="x", latency=99)["status"], "PASA")

    def test_errors(self):
        self.assertEqual(EV.grade_turn({}, turn(error="provider_error")), {"status": "FALLA", "reasons": ["error inesperado «provider_error»"]})
        self.assertEqual(EV.grade_turn({"expect_error": "daily_limit"}, turn(error="daily_limit"))["status"], "PASA")
        self.assertEqual(EV.grade_turn({"expect_error": "daily_limit"}, turn(answer="x"))["status"], "FALLA")
        self.assertEqual(EV.grade_turn({}, None)["status"], "FALLA")

    def test_hard_failure_beats_soft_one(self):
        result = self.grade({"must_contain_any": ["xyz"], "expected_sources": ["pr 1"]}, answer="nada")
        self.assertEqual(result["status"], "FALLA")
        self.assertEqual(len(result["reasons"]), 2)


class GradeCaseTests(unittest.TestCase):
    CASE = {"id": "s", "category": "seguimiento", "messages": ["uno", "dos", "tres"], "must_contain_any": ["datadog"],
            "turn_checks": {"1": {"must_contain_any": ["100 mbps"]}}}

    def test_all_turns_ok(self):
        turns = [turn("hola"), turn("son 100 Mbps"), turn("Datadog")]
        result = EV.grade_case(self.CASE, turns)
        self.assertEqual(result, {"status": "PASA", "reasons": [], "turns": ["PASA", "PASA", "PASA"]})

    def test_earlier_turn_failure_counts_and_is_labelled(self):
        result = EV.grade_case(self.CASE, [turn("hola"), turn("no sé"), turn("Datadog")])
        self.assertEqual(result["status"], "FALLA")
        self.assertTrue(result["reasons"][0].startswith("turno 2:"))

    def test_unchecked_earlier_turn_only_fails_on_error(self):
        self.assertEqual(EV.grade_case(self.CASE, [turn("cualquier cosa", ring="doubt"), turn("100 mbps"), turn("datadog")])["status"], "PASA")
        self.assertEqual(EV.grade_case(self.CASE, [turn(error="provider_error"), turn("100 mbps"), turn("datadog")])["status"], "FALLA")

    def test_missing_turns_fail(self):
        result = EV.grade_case(self.CASE, [turn("hola")])
        self.assertEqual(result["status"], "FALLA")
        self.assertIn("no se ejecutó", result["reasons"][0])

    def test_default_latency_limit_applies_to_the_last_turn(self):
        case = {"id": "x", "category": "hechos", "messages": ["a"], "must_contain_any": ["a"]}
        self.assertEqual(EV.grade_case(case, [turn("a", latency=EV.DEFAULT_MAX_LATENCY_S + 1)])["status"], "PARCIAL")
        case["max_latency_s"] = None
        self.assertEqual(EV.grade_case(case, [turn("a", latency=500)])["status"], "PASA")

    def test_single_turn_reasons_have_no_prefix(self):
        case = {"id": "x", "category": "hechos", "messages": ["a"], "must_contain_any": ["zzz"], "max_latency_s": 5}
        self.assertFalse(EV.grade_case(case, [turn("a")])["reasons"][0].startswith("turno"))


class SummaryAndCompareTests(unittest.TestCase):
    RESULTS = [
        {"id": "a", "category": "hechos", "status": "PASA", "reasons": [], "turns": [turn("x", tokens_in=100, tokens_out=10, latency=1.0)]},
        {"id": "b", "category": "hechos", "status": "FALLA", "reasons": ["x"], "turns": [turn("x", latency=3.0)]},
        {"id": "c", "category": "charla", "status": "PARCIAL", "reasons": ["y"], "turns": [turn(error="invalid_response", latency=9)]},
        {"id": "d", "category": "charla", "status": "FALLA", "reasons": ["z"], "turns": [turn("x", secret_leak=True)]},
    ]

    def test_summary_counts_and_numbers(self):
        summary = EV.summarize(self.RESULTS)
        self.assertEqual(summary["categories"]["hechos"], {"PASA": 1, "PARCIAL": 0, "FALLA": 1, "total": 2})
        self.assertEqual(summary["total"], {"PASA": 1, "PARCIAL": 1, "FALLA": 2, "total": 4})
        self.assertEqual(summary["errors"], {"invalid_response": 1})
        self.assertEqual(summary["secret_leaks"], 1)
        self.assertEqual(summary["tokens"], {"in": 100, "out": 10, "answers_reporting": 1})
        self.assertEqual(summary["latency_s"]["max"], 3.0)  # the answer with an error does not count as latency
        self.assertEqual(EV.summarize([])["latency_s"]["mean"], None)

    def test_percentile(self):
        self.assertIsNone(EV.percentile([], 0.5))
        self.assertEqual(EV.percentile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertEqual(EV.percentile([1, 2, 3, 4, 5], 1.0), 5)

    def test_compare_finds_improvements_and_regressions(self):
        before = {"cases": [{"id": "a", "category": "x", "status": "FALLA"}, {"id": "b", "category": "x", "status": "PASA"},
                            {"id": "c", "category": "x", "status": "PARCIAL"}, {"id": "gone", "category": "x", "status": "PASA"}]}
        now = {"cases": [{"id": "a", "category": "x", "status": "PASA", "reasons": []},
                         {"id": "b", "category": "x", "status": "PARCIAL", "reasons": ["falta algo"]},
                         {"id": "c", "category": "x", "status": "PARCIAL", "reasons": []},
                         {"id": "new", "category": "x", "status": "PASA", "reasons": []}]}
        diff = EV.compare(before, now)
        self.assertEqual([e["id"] for e in diff["improved"]], ["a"])
        self.assertEqual([(e["id"], e["from"], e["to"]) for e in diff["regressed"]], [("b", "PASA", "PARCIAL")])
        self.assertEqual(diff["unchanged"], 1)
        self.assertEqual((diff["only_before"], diff["only_now"]), (["gone"], ["new"]))
        out = io.StringIO()
        EV.print_comparison(diff, out)
        self.assertIn("1 mejoran, 1 empeoran", out.getvalue())
        self.assertIn("falta algo", out.getvalue())

    def test_report_text_does_not_crash_and_lists_problems(self):
        out = io.StringIO()
        EV.print_summary(EV.summarize(self.RESULTS), out)
        EV.print_problems(self.RESULTS, out)
        text = out.getvalue()
        self.assertIn("Hechos concretos", text)
        self.assertIn("invalid_response: 1", text)
        self.assertIn("[FALLA] b", text)


class SyntheticDocumentsTests(unittest.TestCase):
    """The contradiction and weak-evidence cases (E3) and the synthetic documents they run on."""

    def synthetic_cases(self):
        return [case for case in GOLDEN["cases"] if case.get("synthetic")]

    def test_the_synthetic_file_is_valid_and_never_looks_like_a_real_user(self):
        self.assertEqual(EV.validate_synthetic(SYNTHETIC), [])
        self.assertTrue(SYNTHETIC["user_login"].startswith("buddy_eval_"))
        self.assertEqual(SYNTHETIC["source"], "eval-sintetica")

    def test_the_new_categories_are_covered_by_synthetic_cases_with_checkable_expectations(self):
        by_category = {name: [c for c in self.synthetic_cases() if c["category"] == name] for name in ("contradicciones", "confianza")}
        self.assertGreaterEqual(len(by_category["contradicciones"]), 3)
        self.assertGreaterEqual(len(by_category["confianza"]), 3)
        for case in by_category["contradicciones"]:
            if case["expect_conflict"]:
                self.assertEqual(len(case["expected_sources"]), 2, case["id"] + ": both documents must be cited")
                self.assertTrue(case["must_contain_any"] and case["must_contain_all"], case["id"])
        self.assertTrue(any(case["expect_conflict"] is False for case in by_category["contradicciones"]), "a control for false alarms")
        self.assertTrue(any(case.get("expect_confidence_in") == ["high", "medium"] for case in by_category["confianza"]), "a control of clear evidence")

    def test_every_expected_source_of_a_synthetic_case_names_a_synthetic_document(self):
        titles = [EV.normalize(document["name"]) for document in SYNTHETIC["documents"]]
        for case in self.synthetic_cases():
            for entry in case.get("expected_sources") or []:
                self.assertTrue(any(EV.normalize(entry) in title for title in titles), (case["id"], entry))

    def test_the_conflicting_numbers_really_differ_in_the_documents(self):
        texts = {document["key"]: document["text"] for document in SYNTHETIC["documents"]}
        self.assertIn("15 dias habiles", texts["gastos-2023"])
        self.assertIn("30 dias corridos", texts["gastos-guia-2024"])
        self.assertIn("cada 3 anios", texts["notebooks-politica"])
        self.assertIn("cada 4 anios", texts["notebooks-faq"])

    def test_only_synthetic_cases_ask_for_synthetic_documents_and_no_real_case_does(self):
        for case in (FULL or GOLDEN)["cases"]:
            if case["category"] in ("contradicciones", "confianza"):
                self.assertTrue(case.get("synthetic"), case["id"])
            else:
                self.assertFalse(case.get("synthetic"), case["id"])

    def test_validation_of_the_new_keys(self):
        def bad(**changes):
            data = copy.deepcopy(GOLDEN)
            data["cases"][0].update(changes)
            return EV.validate_golden(data)[0]

        self.assertEqual(bad(expect_conflict=True, expect_contact=False, expect_confidence_in=["low"], synthetic=False), [])
        self.assertTrue(bad(expect_conflict="si"))
        self.assertTrue(bad(expect_contact=1))
        self.assertTrue(bad(expect_confidence_in="low"))
        self.assertTrue(bad(expect_confidence_in=[]))
        self.assertTrue(bad(expect_confidence_in=["alta"]))
        self.assertTrue(bad(synthetic="si"))
        self.assertEqual(EV.validate_synthetic({}), ["falta «source»", "falta «group»", "falta «user_login»", "documents debe ser una lista no vacía"])
        broken = copy.deepcopy(SYNTHETIC)
        broken["user_login"] = "admin"
        broken["documents"].append(dict(broken["documents"][0]))
        self.assertEqual(len(EV.validate_synthetic(broken)), 2)

    def grade(self, expect, **kwargs):
        return EV.grade_turn(expect, turn(**kwargs))

    def test_a_marked_contradiction_passes_and_a_missed_one_fails(self):
        expect = {"expect_conflict": True, "must_contain_any": ["informacion distinta"], "must_contain_all": ["15", "30"],
                  "expected_sources": ["politica de gastos", "guia rapida"], "expect_needs_human": False, "expect_contact": True}
        good = dict(answer="Encontré información distinta: 15 días y 30 días.", ring="doubt", ticket=False, contact=True, conflict=True,
                    confidence="medium", sources=["Política de Gastos 2023", "Guía Rápida de Administración"])
        self.assertEqual(self.grade(expect, **good)["status"], "PASA", "the doubt ring of a contradiction is not «needs a person»")
        self.assertEqual(self.grade(expect, **dict(good, conflict=False))["status"], "FALLA")
        self.assertEqual(self.grade(expect, **dict(good, answer="Son 15 días."))["status"], "FALLA")
        self.assertEqual(self.grade(expect, **dict(good, sources=["Política de Gastos 2023"]))["status"], "PARCIAL")
        self.assertEqual(self.grade(expect, **dict(good, contact=False))["status"], "PARCIAL")

    def test_a_false_alarm_is_a_failure(self):
        expect = {"expect_conflict": False, "expect_needs_human": False, "must_contain_any": ["2 dias"]}
        self.assertEqual(self.grade(expect, answer="Son 2 días.", ticket=False, conflict=False)["status"], "PASA")
        self.assertEqual(self.grade(expect, answer="Son 2 días.", ticket=False, conflict=True)["status"], "FALLA")

    def test_confidence_levels_are_soft_and_none_means_not_said(self):
        expect = {"expect_confidence_in": ["low", "none"]}
        self.assertEqual(self.grade(expect, answer="x", confidence="low")["status"], "PASA")
        self.assertEqual(self.grade(expect, answer="x", confidence=None)["status"], "PASA")
        self.assertEqual(self.grade(expect, answer="x")["status"], "PASA")
        weak = self.grade(expect, answer="x", confidence="high")
        self.assertEqual(weak["status"], "PARCIAL")
        self.assertIn("high", weak["reasons"][0])

    def test_the_verdict_of_needs_a_person_uses_the_ticket_when_the_turn_has_one(self):
        # An answer with the doubt ring and no ticket was given (weak evidence): it is NOT «needs a person».
        self.assertEqual(self.grade({"expect_needs_human": False}, answer="x", ring="doubt", ticket=False)["status"], "PASA")
        self.assertEqual(self.grade({"expect_needs_human": True}, answer="x", ring="doubt", ticket=True)["status"], "PASA")
        self.assertEqual(self.grade({"expect_needs_human": True}, answer="x", ring="doubt", ticket=False)["status"], "FALLA")
        # old reports (no ticket key) keep the old reading
        self.assertEqual(self.grade({"expect_needs_human": True}, answer="x", ring="doubt")["status"], "PASA")

    def test_the_summary_counts_the_confidence_and_the_contradictions(self):
        item = lambda **t: {"id": "x", "category": "confianza", "status": "PASA", "turns": [turn("a", **t)]}  # noqa: E731
        summary = EV.summarize([item(confidence="low"), item(confidence="low", conflict=True), item(confidence="high"), item()])
        self.assertEqual(summary["confidence"], {"levels": {"low": 2, "high": 1, "none": 1}, "conflicts": 1})
        out = io.StringIO()
        EV.print_summary(summary, out)
        self.assertIn("contradicción marcada: 1", out.getvalue())

    def test_the_container_script_seeds_only_when_needed_and_always_cleans_up(self):
        script = EV.build_script({"cases": [], "synthetic": {"source": "s", "group": "g", "user_login": "buddy_eval_syn", "documents": []}})
        ast.parse(script)
        source = EV.CONTAINER_SCRIPT
        self.assertIn("syn_seed() if SYN and any(", source, "seeded only for synthetic cases")
        self.assertIn("finally:", source)
        self.assertIn("syn_cleanup()", source.split("finally:")[-1], "the cleanup runs in the finally block")
        self.assertIn("eval-syn-", source, "the documents are marked")
        self.assertIn("group_ids", source)
        self.assertIn('"auto_sync": False', source, "the cron never synchronizes the synthetic folder")
        ast.parse(EV.build_script({"synthetic": {"source": "s", "group": "g"}}, EV.CLEANUP_SCRIPT))

    def test_the_cleanup_only_deletes_what_is_unmistakably_synthetic(self):
        for script in (EV.CONTAINER_SCRIPT, EV.CLEANUP_SCRIPT):
            cleanup = script.split("def syn_cleanup():")[1].split("return removed")[0]
            self.assertIn('"evalsintetica" in (source.folder_url or "")', cleanup, "the folder must carry the marker address")
            self.assertIn('startswith("eval-syn-")', cleanup, "and every document the marker")
            self.assertIn("synthetic_refused", cleanup, "a real folder with the same name is refused, never deleted")
            self.assertIn("login == SYN[\"user_login\"]", cleanup, "the group only goes when nobody else is in it")
            self.assertIn('write({"active": False})', cleanup, "the evaluation user is deactivated")
            self.assertEqual(cleanup.count(".unlink()"), 2, "exactly: the marked source and the empty group")
        self.assertIn('"folder_url": "https://drive.google.com/drive/folders/evalsintetica', EV.CONTAINER_SCRIPT, "what the seed creates carries the marker")
        self.assertIn('"drive_id": "eval-syn-%d"', EV.CONTAINER_SCRIPT)

    def test_the_batch_job_carries_the_synthetic_documents_only_for_synthetic_cases(self):
        captured = {}

        def fake_run(command, input=None, **kwargs):
            captured["script"] = input.decode("utf-8")
            return types.SimpleNamespace(stdout=b"", stderr=b"", returncode=0)

        options = types.SimpleNamespace(pause=0, retries=0, backoff=0, users=1, budget_left=5, database="buddy_v2", synthetic=SYNTHETIC)
        original = EV.subprocess.run
        EV.subprocess.run = fake_run
        try:
            EV.run_batch([{"id": "hechos-01", "messages": ["hola"]}], options)
            plain = json.loads(base64.b64decode(re.search(r'b64decode\("([A-Za-z0-9+/=]+)"\)', captured["script"]).group(1)))
            EV.run_batch([{"id": "contradicciones-01", "messages": ["hola"], "synthetic": True}], options)
            marked = json.loads(base64.b64decode(re.search(r'b64decode\("([A-Za-z0-9+/=]+)"\)', captured["script"]).group(1)))
        finally:
            EV.subprocess.run = original
        self.assertIsNone(plain["synthetic"])
        self.assertEqual(plain["cases"][0]["synthetic"], False)
        self.assertEqual(marked["synthetic"]["source"], "eval-sintetica")
        self.assertEqual(marked["cases"][0]["synthetic"], True)

    def test_cleanup_without_synthetic_documents_does_nothing(self):
        self.assertIsNone(EV.cleanup_synthetic(types.SimpleNamespace(synthetic=None, database="buddy_v2")))


class ContainerScriptTests(unittest.TestCase):
    JOB = {"cases": [{"id": "a", "messages": ["¿qué día es? ñ \"comillas\" '''"]}], "pause": 1, "retries": 1, "backoff": 1,
           "users": 2, "budget": 5, "retryable": list(EV.RETRYABLE_ERRORS)}

    def test_script_is_valid_python_and_the_payload_round_trips(self):
        script = EV.build_script(self.JOB)
        ast.parse(script)
        encoded = re.search(r'b64decode\("([A-Za-z0-9+/=]+)"\)', script).group(1)
        self.assertEqual(json.loads(base64.b64decode(encoded).decode("utf-8")), self.JOB)
        self.assertNotIn("__PAYLOAD__", script)

    def test_script_never_touches_secrets_or_the_v1(self):
        script = EV.CONTAINER_SCRIPT
        for forbidden in ("environ", "getenv", "print(api_key", "print(params", "buddy-odoo-", "'buddy'", '"buddy"'):
            self.assertNotIn(forbidden, script)
        # the key is only ever compared (a boolean leaves the container), never emitted
        self.assertIn("api_key in blob", script)

    def test_compose_command_targets_only_the_v2_stack(self):
        command = EV.compose_command(Path("/proj"), "buddy_v2")
        self.assertEqual(command[command.index("-d") + 1], "buddy_v2")
        self.assertIn("odoo-setup", command)
        self.assertNotIn("buddy", command)  # the v1 database

    def test_parse_events_ignores_noise(self):
        output = "odoo log line\n@@EVAL@@ {\"kind\": \"meta\", \"model\": \"m\"}\nBuddy: WARNING x\n@@EVAL@@ {rota\n@@EVAL@@ {\"kind\": \"done\", \"attempts\": 3}\n"
        self.assertEqual(EV.parse_events(output), [{"kind": "meta", "model": "m"}, {"kind": "done", "attempts": 3}])


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        import contextlib
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))  # run_suite narrates every batch
        self.addCleanup(stack.close)

    def options(self, **kw):
        base = dict(pause=0, batch_size=3, max_queries=10, budget_left=10, retries=1, backoff=0, users=2, database="buddy_v2",
                    label="", only=None, ids=None, cases_all=[])
        base.update(kw)
        return types.SimpleNamespace(**base)

    CASES = [{"id": f"c{i}", "category": "hechos", "messages": ["q"] * n, "must_contain_any": ["ok"]}
             for i, n in enumerate([1, 2, 2, 1, 3], 1)]

    def test_batches_never_split_a_conversation(self):
        groups = [[c["id"] for c in g] for g in EV.batches(self.CASES, 3)]
        self.assertEqual(groups, [["c1", "c2"], ["c3", "c4"], ["c5"]])
        self.assertEqual([[c["id"] for c in g] for g in EV.batches(self.CASES[:1], 1)], [["c1"]])

    def fake_runner(self, answer="ok", error=None):
        def runner(group, options):
            events = [{"kind": "meta", "provider": "gemini", "model": "m", "top_k": "6", "documents": 7, "chunks": 79}]
            used = 0
            for case in group:
                if used + len(case["messages"]) > options.budget_left:
                    break  # like the real script: it stops when the budget is spent
                turns = [turn(answer, error=error) for _ in case["messages"]]
                events.append({"kind": "case", "id": case["id"], "turns": turns, "eval_user": 1})
                used += len(case["messages"])
            events.append({"kind": "done", "attempts": used})
            return events
        return runner

    def test_run_suite_grades_and_counts_queries(self):
        results, meta, attempts, aborted = EV.run_suite(self.CASES, self.options(), runner=self.fake_runner(), sleep=lambda s: None)
        self.assertEqual([r["status"] for r in results], ["PASA"] * 5)
        self.assertEqual((attempts, aborted, meta["model"]), (9, None, "m"))

    def test_run_suite_stops_at_the_query_cap(self):
        results, _, attempts, aborted = EV.run_suite(self.CASES, self.options(max_queries=3), runner=self.fake_runner(), sleep=lambda s: None)
        self.assertEqual(aborted, "se agotó el tope de consultas")
        self.assertEqual(attempts, 3)
        self.assertEqual([r["id"] for r in results], ["c1", "c2"])  # the first batch ran; nothing after the cap

    def test_run_suite_marks_unexecuted_cases_and_stops_when_a_batch_dies(self):
        def dying(group, options):
            return [{"kind": "case", "id": group[0]["id"], "turns": [turn("ok")], "eval_user": 1}]
        results, _, attempts, aborted = EV.run_suite(self.CASES, self.options(), runner=dying, sleep=lambda s: None)
        self.assertIn("sin avisar", aborted)
        by_id = {r["id"]: r for r in results}
        self.assertEqual(by_id["c1"]["status"], "PASA")
        self.assertEqual(by_id["c2"]["status"], "FALLA")
        self.assertIn("no se ejecutó", by_id["c2"]["reasons"][0])

    def test_provider_errors_are_failures_not_crashes(self):
        results, _, _, _ = EV.run_suite(self.CASES[:1], self.options(), runner=self.fake_runner(error="provider_error"), sleep=lambda s: None)
        self.assertEqual(results[0]["status"], "FALLA")

    def test_build_report_is_json_and_has_no_secrets_fields(self):
        options = self.options(cases_all=self.CASES, only=None)
        results, meta, attempts, aborted = EV.run_suite(self.CASES, options, runner=self.fake_runner(), sleep=lambda s: None)
        report = EV.build_report(results, meta, attempts, aborted, options, PROJECT / "tests/eval/golden.json", "t0", "t1")
        text = json.dumps(report)
        self.assertEqual(report["queries_used"], 9)
        self.assertEqual(report["summary"]["total"]["total"], 5)
        self.assertEqual([c["id"] for c in report["cases"]], ["c1", "c2", "c3", "c4", "c5"])
        for forbidden in ("api_key", "password", "BEGIN PRIVATE"):
            self.assertNotIn(forbidden, text)


class CommandLineTests(unittest.TestCase):
    def test_defaults(self):
        options = EV.parse_args([])
        self.assertEqual((options.max_queries, options.pause, options.retries, options.database), (150, 7.0, 2, "buddy_v2"))

    def test_filters_are_split(self):
        options = EV.parse_args(["--only", "charla, hechos", "--ids", "a,b"])
        self.assertEqual((options.only, options.ids), (["charla", "hechos"], ["a", "b"]))

    def test_the_v1_database_is_refused(self):
        with self.assertRaises(SystemExit):
            EV.parse_args(["--database", "buddy"])

    def test_dry_run_validates_and_never_runs_the_container(self):
        calls = []
        original = EV.run_batch
        EV.run_batch = lambda *args, **kw: calls.append(args)
        out = io.StringIO()
        try:
            import contextlib
            with contextlib.redirect_stdout(out):
                code = EV.main(["--dry-run", "--only", "contradicciones"])
        finally:
            EV.run_batch = original
        self.assertEqual((code, calls), (0, []))
        self.assertIn("4 casos", out.getvalue())

    def test_unknown_ids_and_over_cap_are_refused(self):
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(EV.main(["--dry-run", "--ids", "no-existe"]), 1)
            self.assertEqual(EV.main(["--max-queries", "3"]), 1)
        self.assertIn("no existen", err.getvalue())
        self.assertIn("superan el tope", err.getvalue())


if __name__ == "__main__":
    unittest.main()
