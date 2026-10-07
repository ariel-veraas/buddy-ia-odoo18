"""Standalone tests for the pure rules of the weekly summary (no Odoo, no database): grouping of questions, masking, windows.

Run from the repo root (tools/validate_local.py does it):
    .venv\\Scripts\\python.exe -m unittest discover -s odoo-addons/buddy_ia/tests -p test_digest_logic.py
"""
import importlib.util
import json
import sys
import types
import unittest
from datetime import date
from pathlib import Path

sys.dont_write_bytecode = True
MODELS = Path(__file__).resolve().parents[1] / "models"

_package = types.ModuleType("buddy_digest_under_test")
_package.__path__ = [str(MODELS)]
sys.modules[_package.__name__] = _package


def load(name):
    spec = importlib.util.spec_from_file_location(_package.__name__ + "." + name, MODELS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


for _name in ("buddy_text", "buddy_journey_logic"):
    load(_name)
logic = load("buddy_digest_logic")

QUESTIONS = [
    "¿Cómo restauro un backup?",
    "¿Dónde se guardan los backups?",
    "Necesito restaurar el backup de ayer",
    "¿Cómo pido vacaciones?",
    "¿Cuántos días de vacaciones me tocan?",
    "¿Cómo se carga una factura de proveedor?",
]


class Keys(unittest.TestCase):
    def test_the_words_of_the_topic_are_kept_and_the_filler_is_dropped(self):
        words = [shown for key, shown in logic.question_keys("¿Cómo restauro un backup de la base?")]
        self.assertEqual(words, ["restauro", "backup", "base"])

    def test_singular_and_plural_are_the_same_key_and_accents_do_not_matter(self):
        self.assertEqual(logic.question_keys("backups")[0][0], logic.question_keys("Backup")[0][0])
        self.assertEqual(logic.question_keys("Política")[0][0], logic.question_keys("politica")[0][0])
        self.assertEqual(logic.question_keys("Política")[0][1], "política", "the accents of what was written are kept to show it")

    def test_addresses_and_links_are_not_a_topic(self):
        words = [shown for key, shown in logic.question_keys("Soy ana@empresa.com, mirá https://x.com/a y www.algo.com/ruta: ¿cómo restauro el backup?")]
        self.assertEqual(words, ["mirá", "restauro", "backup"])

    def test_numbers_short_words_and_non_text_are_dropped(self):
        self.assertEqual(logic.question_keys("12345 de la y 7"), [])
        self.assertEqual(logic.question_keys(None), [])
        self.assertEqual(logic.question_keys(5), [])

    def test_the_number_of_words_is_bounded(self):
        self.assertEqual(len(logic.question_keys(" ".join("tema" + chr(97 + i) * 3 for i in range(26)))), 14)


class Clusters(unittest.TestCase):
    def test_questions_that_share_a_topic_word_make_a_group_biggest_first(self):
        groups, ungrouped = logic.cluster_questions(QUESTIONS)
        self.assertEqual([(group["terms"][0], group["count"]) for group in groups], [("backup", 3), ("vacaciones", 2)])
        self.assertEqual(groups[0]["members"], [0, 1, 2])
        self.assertEqual(ungrouped, 1, "the question about the invoice shares nothing with the rest")

    def test_a_group_is_named_with_the_words_most_of_it_uses(self):
        groups, _ = logic.cluster_questions(["¿Cómo puedo restaurar un backup?", "Restaurar el backup falla", "No logro restaurar el backup"])
        self.assertEqual(groups[0]["terms"], ["restaurar", "backup"], "both are in all three: the longest names the group, the other follows")
        self.assertLessEqual(len(groups[0]["terms"]), 3)

    def test_between_words_in_as_many_questions_the_longest_names_the_group(self):
        groups, _ = logic.cluster_questions(["¿Cuántos días de vacaciones me corresponden por año?", "Los días de vacaciones de este año", "¿Vacaciones: cuántos días por año?"])
        self.assertEqual(groups[0]["terms"][0], "vacaciones")
        self.assertIn("año", groups[0]["terms"])

    def test_the_same_questions_always_give_the_same_groups(self):
        first = logic.cluster_questions(QUESTIONS)
        self.assertEqual(first, logic.cluster_questions(list(QUESTIONS)))

    def test_a_word_that_is_in_most_questions_does_not_start_a_group(self):
        questions = ["¿Cómo hago %s en Odoo?" % topic for topic in ("facturas", "facturas de compra", "remitos", "remitos de venta", "viáticos", "viáticos del mes",
                                                                    "cotizaciones", "cotizaciones nuevas")]
        groups, _ = logic.cluster_questions(questions)
        self.assertTrue(groups)
        self.assertNotIn("odoo", [group["terms"][0] for group in groups])
        self.assertEqual(sorted(group["terms"][0] for group in groups)[:2], ["cotizaciones", "facturas"])

    def test_nothing_to_group_gives_no_groups(self):
        self.assertEqual(logic.cluster_questions([]), ([], 0))
        self.assertEqual(logic.cluster_questions(["", None, "   ", "de la"]), ([], 0))
        self.assertEqual(logic.cluster_questions(["uno solo"]), ([], 1))

    def test_empty_questions_are_not_counted_as_ungrouped(self):
        groups, ungrouped = logic.cluster_questions(["backup uno", "backup dos", "", None, "otra cosa distinta"])
        self.assertEqual((len(groups), ungrouped), (1, 1))

    def test_the_number_of_groups_is_bounded(self):
        questions = []
        for index in range(30):
            word = "tema" + chr(97 + index % 26) * 2
            questions += ["¿Qué hay de %s?" % word, "Necesito saber de %s" % word]
        groups, _ = logic.cluster_questions(questions)
        self.assertEqual(len(groups), logic.MAX_GROUPS)

    def test_a_big_input_is_handled(self):
        questions = ["pregunta numero%s sobre tema%s" % (index, index % 40) for index in range(1000)]
        groups, ungrouped = logic.cluster_questions(questions)
        self.assertEqual(len(groups), logic.MAX_GROUPS)
        self.assertGreater(groups[0]["count"], 20)


class Masking(unittest.TestCase):
    def test_mails_links_and_long_numbers_are_hidden(self):
        text = logic.mask_personal("Soy ana@empresa.com, mi DNI 30.123.456 y mi cel 11 5555-1234; mirá https://x.com/a?b=1 o www.algo.com/ruta")
        self.assertNotIn("ana@empresa.com", text)
        self.assertNotIn("30.123.456", text)
        self.assertNotIn("5555", text)
        self.assertNotIn("https", text)
        self.assertNotIn("www.", text)
        for marker in ("[correo]", "[número]", "[enlace]"):
            self.assertIn(marker, text)

    def test_short_numbers_and_ordinary_words_stay(self):
        self.assertEqual(logic.mask_personal("¿Qué dice el PR 38 sobre 3 pasos?"), "¿Qué dice el PR 38 sobre 3 pasos?")

    def test_it_is_one_plain_line_and_bounded(self):
        text = logic.mask_personal("uno\ndos\u0000 " + "x" * 500)
        self.assertNotIn("\n", text)
        self.assertNotIn("\x00", text)
        self.assertLessEqual(len(text), logic.EXAMPLE_CHARS)
        self.assertEqual(logic.mask_personal(None), "")

    def test_examples_are_masked_different_and_few(self):
        questions = ["Mi mail es a@b.com y no restauro el backup", "no restauro el backup", "No RESTAURO el backup", "restauro backup larguísimo " * 4]
        found = logic.example_questions(questions, [0, 1, 2, 3])
        self.assertEqual(len(found), logic.EXAMPLES_PER_GROUP)
        self.assertEqual(len(set(found)), len(found))
        self.assertTrue(all("@" not in item for item in found))
        self.assertEqual(logic.example_questions(questions, [0], limit=5), ["Mi mail es [correo] y no restauro el backup"])


class Windows(unittest.TestCase):
    def test_the_summary_asked_for_now_counts_the_last_seven_days_including_today(self):
        self.assertEqual(logic.manual_window(date(2026, 10, 6)), (date(2026, 9, 30), date(2026, 10, 7)))

    def test_the_week_ends_the_day_before_the_summary(self):
        self.assertEqual(logic.week_window(date(2026, 10, 6)), (date(2026, 9, 29), date(2026, 10, 6)))

    def test_the_unused_window_is_never_longer_than_what_the_history_and_the_retention_allow(self):
        self.assertEqual(logic.unused_window(400, 90), 90)
        self.assertEqual(logic.unused_window(60, 90), 60)
        self.assertEqual(logic.unused_window(400, 45), 45)
        self.assertEqual(logic.unused_window(29, 90), 0, "too young to say a document is unused")
        self.assertEqual(logic.unused_window(400, 29), 0, "statistics kept for less than a month cannot say it either")
        self.assertEqual(logic.unused_window(30, 30), 30)
        self.assertEqual(logic.unused_window(None, None), 0)
        self.assertEqual(logic.unused_window(100, 3650), 90)


class StoredData(unittest.TestCase):
    def test_the_data_survives_a_round_trip(self):
        data = {"window": {"from": "2026-09-29", "to": "2026-10-06"}, "unanswered": {"total": 3, "groups": [{"terms": ["backup"], "count": 2}]}}
        loaded = logic.load_data(logic.dump_data(data))
        self.assertEqual(loaded["unanswered"], data["unanswered"])
        self.assertEqual(loaded["version"], logic.DATA_VERSION)
        self.assertEqual(logic.window_dates(loaded), (date(2026, 9, 29), date(2026, 10, 6)))

    def test_whatever_is_stored_that_is_not_the_expected_shape_is_empty(self):
        for raw in (None, "", "no es json", "[]", "5", '{"version": 99}', '{"sin": "version"}', "{" * 100000, "x" * 300000, 7, b"{}"):
            self.assertEqual(logic.load_data(raw), {}, repr(raw)[:30])
        self.assertEqual(logic.window_dates({}), (None, None))
        self.assertEqual(logic.window_dates({"window": {"from": "2026-13-01", "to": "x"}}), (None, None))

    def test_a_summary_that_would_be_too_big_is_refused_not_cut(self):
        with self.assertRaises(ValueError):
            logic.dump_data({"unanswered": {"groups": ["x" * 1000] * 300}})

    def test_untrusted_ids_and_counts_are_checked(self):
        self.assertEqual(logic.int_list([1, True, "2", 3.0, -4, 0, 2 ** 40, 5], 10), [1, 5])
        self.assertEqual(logic.int_list("1,2", 10), [])
        self.assertEqual(logic.int_list(list(range(1, 100)), 3), [1, 2, 3])
        self.assertEqual([logic.count(value) for value in (3, True, -1, "4", None, 10 ** 12)], [3, 0, 0, 0, 0, 0])

    def test_the_summary_is_valid_json(self):
        self.assertEqual(json.loads(logic.dump_data({"a": "ñ"}))["a"], "ñ")


class Limits(unittest.TestCase):
    def test_the_documented_limits(self):
        self.assertEqual((logic.MIN_PEOPLE, logic.STEP_MIN_LATE, logic.WINDOW_DAYS, logic.UNUSED_DAYS, logic.MIN_UNUSED_DAYS), (5, 2, 7, 90, 30))
        self.assertEqual(logic.ADMIN_SECTIONS, ("folders", "onboarding"))
        self.assertTrue(set(logic.ADMIN_SECTIONS) <= set(logic.SECTIONS))
        self.assertEqual(sorted(logic.RENDER_ORDER), sorted(logic.SECTIONS))
        self.assertEqual(logic.RENDER_ORDER[-1], "unused")


if __name__ == "__main__":
    unittest.main()
