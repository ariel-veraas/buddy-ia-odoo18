"""The weekly summary «Qué falta documentar» (E4b): what it counts, what it never says, who can open it and what they see,
the notice (an Odoo activity, never an e-mail), the retention and the switches of the administrator."""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..models import buddy_digest
from ..models import buddy_digest_logic as logic
from .common import new_document, new_group, new_source, new_user
from .test_insight import row
from .test_journey import JourneyCase, assign, make_journey, today_of

MANAGER = "base.group_user,buddy_ia.group_buddy_manager"
UNANSWERED = [
    "¿Cómo restauro un backup?", "¿Dónde se guardan los backups?", "Necesito restaurar el backup de ayer",
    "¿Cómo pido vacaciones?", "¿Cuántos días de vacaciones me tocan?", "¿Cómo se carga una factura de proveedor?",
]


class DigestCase(JourneyCase):
    def setUp(self):
        super().setUp()
        self.Digest = self.env["buddy.digest"]
        self.Insight = self.env["buddy.insight"]
        self.Feedback = self.env["buddy.feedback"]
        for key in ("insight_days", "insight_questions", "feedback_min_audience", "digest_enabled", "digest_notify", "ticket_user_id"):
            self.P.search([("key", "=", "buddy_ia." + key)]).unlink()
        self.Digest.sudo().search([]).unlink()
        self.Insight.sudo().search([]).unlink()
        self.Feedback.sudo().search([]).unlink()
        self.group = new_group(self.env, "Lectores")
        self.readers = [new_user(self.env, "lector%d" % index, extra_groups=(self.group,)) for index in range(5)]
        self.source = new_source(self.env, "Carpeta abierta", [self.group])
        self.doc = new_document(self.env, self.source, "PR 38 Backups.docx", "texto")
        self.doc2 = new_document(self.env, self.source, "Guía de ingreso.pdf", "texto")
        self.manager = new_test_user(self.env, login="buddy_d_gestor", groups=MANAGER)
        self.manager.groups_id = [(4, self.group.id)]  # this manager reads the folder of the tests (not the secret ones)
        # The summary is made the day AFTER the rows of these tests were created (they are made «today» by the database).
        self.run_day = fields.Date.today() + timedelta(days=1)

    def generate(self, **kwargs):
        return self.Digest._generate(self.run_day, **kwargs)[0]

    def data_of(self, digest):
        return json.loads(digest.sudo().data)

    def ask(self, questions, **over):
        for question in questions:
            self.Insight._record(row(question=question, needs_human=True, **over))

    def vote(self, value, documents, day=None):
        """A vote given `day` (yesterday by default: the votes of today are never counted in a summary)."""
        feedback = self.Feedback._store(None, {"value": value, "document_ids": documents.ids, "model": "m"})
        self.env.cr.execute("UPDATE buddy_feedback SET day = %s WHERE id = %s", (day or fields.Date.today() - timedelta(days=1), feedback.id))
        return feedback

    def report(self, user, digest=None):
        return (digest or self.generate()).with_user(user).report


# ---------------------------------------------------------------------------------------------------------- the weekly run
@tagged("post_install", "-at_install")
class TestDigestGeneration(DigestCase):
    def test_one_weekly_summary_per_day_and_the_button_makes_its_own(self):
        first, created = self.Digest._generate(self.run_day)
        self.assertTrue(created)
        self.assertEqual((first.date_from, first.date_to, first.date_last, first.manual), (self.run_day - timedelta(days=7), self.run_day, self.run_day - timedelta(days=1), False))
        again, created = self.Digest._generate(self.run_day)
        self.assertEqual((again, created), (first, False), "the cron running twice does not duplicate")
        self.ask(["¿Cómo restauro un backup?"])
        asked, created = self.Digest._generate(self.run_day, manual=True)
        self.assertTrue(created and asked.manual and asked != first, "asking for one never takes the place of the weekly one")
        self.assertEqual((asked.date_from, asked.date_to), (self.run_day - timedelta(days=6), self.run_day + timedelta(days=1)), "it counts until today, included")
        self.assertEqual(asked.unanswered_count, 1)
        remade, created = self.Digest._generate(self.run_day, manual=True)
        self.assertEqual((remade, created), (asked, False), "asking again remakes the same one")
        self.assertEqual(self.Digest.sudo().search_count([]), 2)
        self.assertEqual(first.unanswered_count, 0, "the weekly one was not touched")

    def test_a_second_row_for_the_same_day_is_refused_by_the_database(self):
        digest = self.generate()
        with self.assertRaises(Exception), mute_logger("odoo.sql_db"), self.env.cr.savepoint():
            self.Digest.create({"date_from": digest.date_from, "date_to": digest.date_to, "manual": False})
        self.Digest.create({"date_from": digest.date_from, "date_to": digest.date_to, "manual": True})  # the one asked for is another row

    def test_the_unanswered_questions_are_grouped_by_the_words_they_share(self):
        self.ask(UNANSWERED)
        digest = self.generate()
        section = self.data_of(digest)["unanswered"]
        self.assertEqual((section["total"], section["read"], section["ungrouped"]), (6, 6, 1))
        self.assertEqual([(group["terms"][0], group["count"]) for group in section["groups"]], [("backup", 3), ("vacaciones", 2)])
        self.assertEqual(digest.unanswered_count, 6)
        self.assertLessEqual(len(section["groups"][0]["examples"]), logic.EXAMPLES_PER_GROUP)

    def test_answered_questions_and_the_ones_outside_the_week_are_not_counted(self):
        self.ask(["¿Cómo restauro un backup?"])
        self.Insight._record(row(question="respondida", needs_human=False))
        self.Insight._record(row(question="con error", needs_human=True, error="provider_error"))
        old = self.Insight._record(row(question="vieja", needs_human=True))
        self.env.cr.execute("UPDATE buddy_insight SET create_date = now() - interval '10 days' WHERE id = %s", (old.id,))
        self.assertEqual(self.data_of(self.generate())["unanswered"]["total"], 1)

    def test_questions_with_no_stored_text_are_only_counted(self):
        self.Insight._record(row(question="", needs_human=True))
        self.Insight._record(row(question="   ", needs_human=True))
        section = self.data_of(self.generate())["unanswered"]
        self.assertEqual((section["total"], section["without_text"], section["groups"]), (2, 2, []))

    def test_only_the_first_thousand_questions_are_read_but_all_are_counted(self):
        self.Insight.create([{"question": "¿Cómo hago el tema%d?" % (index % 50), "needs_human": True} for index in range(1100)])
        section = self.data_of(self.generate())["unanswered"]
        self.assertEqual((section["total"], section["read"]), (1100, logic.QUESTIONS_READ))
        self.assertLessEqual(len(section["groups"]), logic.MAX_GROUPS)

    def test_the_downvotes_are_counted_per_document_and_the_week_is_respected(self):
        for _index in range(3):
            self.vote("down", self.doc)
        self.vote("down", self.doc2)
        self.vote("up", self.doc2)
        self.vote("up", self.doc2)
        self.vote("up", self.doc)
        self.vote("down", self.doc, day=fields.Date.today())  # today is not over: never counted
        self.vote("down", self.doc, day=self.run_day)  # after the week
        self.vote("down", self.doc, day=self.run_day - timedelta(days=8))  # before the week
        section = self.data_of(self.generate())["downvotes"]
        self.assertEqual((section["down"], section["up"]), (4, 3))
        self.assertEqual(section["items"], [{"doc": self.doc.id, "down": 3, "up": 1}, {"doc": self.doc2.id, "down": 1, "up": 2}])

    def test_votes_without_documents_count_in_the_total_but_never_name_a_document(self):
        small = new_group(self.env, "Dos")
        new_user(self.env, "d1", extra_groups=(small,))
        hidden = new_document(self.env, new_source(self.env, "Chica", [small]), "Poco leído", "texto")
        self.vote("down", hidden)  # fewer than five people can read it: the vote is kept without the document
        section = self.data_of(self.generate())["downvotes"]
        self.assertEqual((section["down"], section["items"]), (1, []))

    def test_the_doubtful_answers_name_the_documents_that_were_cited(self):
        self.Insight._record(row(document_ids=[self.doc.id, self.doc2.id], conflict=True, confidence="low"))
        self.Insight._record(row(document_ids=[self.doc.id], confidence="low"))
        self.Insight._record(row(document_ids=[self.doc2.id], confidence="high"))
        digest = self.generate()
        section = self.data_of(digest)["doubtful"]
        self.assertEqual((section["total"], section["conflicts"], digest.doubtful_count), (2, 1, 2))
        self.assertEqual(section["items"], [{"doc": self.doc.id, "count": 2, "conflicts": 1}, {"doc": self.doc2.id, "count": 1, "conflicts": 1}])

    def test_unused_documents_are_only_judged_with_enough_history(self):
        self.Insight._record(row(document_ids=[self.doc.id]))
        section = self.data_of(self.generate())["unused"]
        self.assertEqual((section["days"], section["total"], section["doc_ids"]), (0, 0, []), "the statistics are a minute old")

    def test_unused_documents_are_the_old_ones_nothing_cited_in_the_window(self):
        stale = new_document(self.env, self.source, "Política vieja", "texto")
        fresh = new_document(self.env, self.source, "Recién subido", "texto")
        self.Insight._record(row(document_ids=[self.doc.id]))
        oldest = self.Insight._record(row(document_ids=[]))
        self.env.cr.execute("UPDATE buddy_insight SET create_date = now() - interval '100 days' WHERE id = %s", (oldest.id,))
        self.env.cr.execute("UPDATE buddy_document SET create_date = now() - interval '120 days' WHERE id IN %s",
                            ((stale.id, self.doc.id, self.doc2.id),))
        digest = self.generate()
        section = self.data_of(digest)["unused"]
        self.assertEqual(section["days"], 90)
        self.assertIn(stale.id, section["doc_ids"])
        self.assertIn(self.doc2.id, section["doc_ids"])
        self.assertNotIn(self.doc.id, section["doc_ids"], "cited this week")
        self.assertNotIn(fresh.id, section["doc_ids"], "younger than the window: new, not unused")
        self.assertGreaterEqual(section["total"], 2)
        self.assertEqual(section["total"], digest.unused_count)

    def test_the_window_of_unused_documents_is_never_longer_than_what_the_statistics_keep(self):
        self.P.set_param("buddy_ia.insight_days", "40")
        oldest = self.Insight._record(row(document_ids=[]))
        self.env.cr.execute("UPDATE buddy_insight SET create_date = now() - interval '100 days' WHERE id = %s", (oldest.id,))
        self.assertEqual(self.data_of(self.generate())["unused"]["days"], 40)
        self.P.set_param("buddy_ia.insight_days", "20")
        self.assertEqual(self.data_of(self.generate(manual=True))["unused"]["days"], 0)

    def test_folders_with_errors_or_files_that_could_not_be_read_fully_are_listed(self):
        self.source.sudo().write({"state": "error"})
        new_document(self.env, self.source, "Roto", "t", state="error", error="No se pudo leer")
        new_document(self.env, self.source, "Escaneado", "t", state="empty")
        new_document(self.env, self.source, "Larguísimo", "t", error="Se leyó solo hasta la página 500")
        healthy = new_source(self.env, "Sana", [self.group])
        new_document(self.env, healthy, "Bien", "texto")
        digest = self.generate()
        section = self.data_of(digest)["folders"]
        ours = [item for item in section["items"] if item["source"] in (self.source.id, healthy.id)]
        self.assertEqual(ours, [{"source": self.source.id, "sync_error": True, "error_docs": 1, "empty_docs": 1, "truncated_docs": 1}])
        self.assertGreaterEqual(digest.problem_count, 1)

    def test_onboarding_gives_figures_only_for_journeys_that_enough_people_follow(self):
        big = make_journey(self.env, steps=[(0, "Alta de accesos"), (1, "Charla con RR.HH."), (2, "Cierre")])
        small = make_journey(self.env, steps=[(0, "Algo")])
        for user in self.readers:
            assign(self.env, user, big, start=today_of(self.env, user) - timedelta(days=5))
        for user in (self.ana, self.beto, self.rosa, self.raul):
            assign(self.env, user, small, start=today_of(self.env, user) - timedelta(days=5))
        for user in self.readers[:2]:
            self.env["buddy.journey.progress"].search([("user_id", "=", user.id), ("step_id.name", "=", "Alta de accesos")]).with_user(user).write({"done": True})
        digest = self.generate()
        section = self.data_of(digest)["onboarding"]
        self.assertGreaterEqual(section["small"], 1)
        (item,) = [item for item in section["items"] if item["journey"] in (big.id, small.id)]
        self.assertEqual((item["journey"], item["people"], item["late_people"], item["avg_pct"]), (big.id, 5, 5, 13), "the one of four people has no figures")
        names = {step.id: step.name for step in big.step_ids}
        self.assertEqual({names[line["step"]]: line["late"] for line in item["steps"]}, {"Alta de accesos": 3, "Charla con RR.HH.": 5, "Cierre": 5})
        self.assertGreaterEqual(digest.late_count, 5)
        dump = digest.sudo().data
        for user in self.readers + [self.ana]:
            self.assertNotIn(user.login, dump)
            self.assertNotIn(user.name, dump, "figures, never who")

    def test_a_step_late_for_a_single_person_is_not_named(self):
        journey = make_journey(self.env, steps=[(0, "Hecho por casi todos"), (3, "Ya pasó")])
        for index, user in enumerate(self.readers):
            assignment = assign(self.env, user, journey, start=today_of(self.env, user) - timedelta(days=2))
            if index:
                assignment.progress_ids.filtered(lambda line: line.step_id.name == "Hecho por casi todos").with_user(user).write({"done": True})
        (item,) = [item for item in self.data_of(self.generate())["onboarding"]["items"] if item["journey"] == journey.id]
        self.assertEqual(item["late_people"], 1)
        self.assertEqual(item["steps"], [], "one person late with a step: a number about somebody")

    def test_a_section_that_fails_is_left_out_and_the_rest_is_kept(self):
        self.ask(UNANSWERED)
        with patch.object(type(self.Digest), "_section_doubtful", side_effect=RuntimeError("boom")), mute_logger("odoo.addons.buddy_ia.models.buddy_digest"):
            digest = self.generate()
        data = self.data_of(digest)
        self.assertEqual(data["partial"], ["doubtful"])
        self.assertNotIn("doubtful", data)
        self.assertEqual(data["unanswered"]["total"], 6)
        self.assertTrue(digest.partial)

    def test_a_query_that_breaks_the_transaction_does_not_cost_the_other_sections(self):
        def broken(this, date_from, date_to):
            this.env.cr.execute("SELECT 1 FROM table_that_does_not_exist")

        self.ask(UNANSWERED)
        with patch.object(type(self.Digest), "_section_downvotes", broken), mute_logger("odoo.sql_db", "odoo.addons.buddy_ia.models.buddy_digest"):
            data = self.data_of(self.generate())
        self.assertEqual(data["partial"], ["downvotes"])
        self.assertEqual(data["unanswered"]["total"], 6, "the sections after the broken one still ran")
        self.assertIn("unused", data)

    def test_when_the_budget_runs_out_what_is_left_is_marked_partial(self):
        clock = iter([0.0] + [1000.0] * 20)
        with patch.object(buddy_digest, "time", type("Clock", (), {"monotonic": staticmethod(lambda: next(clock))})):
            digest = self.generate()
        self.assertEqual(self.data_of(digest)["partial"], list(logic.SECTIONS))
        self.assertTrue(digest.partial)
        self.assertIn("Algunas partes no se pudieron calcular", digest.with_user(self.admin).report)

    def test_the_time_limit_of_the_queries_is_put_back_afterwards(self):
        self.env.cr.execute("SHOW statement_timeout")
        before = self.env.cr.fetchone()[0]
        self.generate()
        self.env.cr.execute("SHOW statement_timeout")
        self.assertEqual(self.env.cr.fetchone()[0], before)

    def test_an_empty_week_is_a_valid_summary(self):
        digest = self.generate()
        self.assertEqual((digest.unanswered_count, digest.downvote_count, digest.doubtful_count, digest.unused_count), (0, 0, 0, 0))
        report = digest.with_user(self.admin).report
        self.assertIn("Esta semana no hay nada que documentar ni corregir.", report)
        self.assertIn("Sin novedades en: preguntas sin respuesta, votos 👎, respuestas dudosas, carpetas.", report)
        self.assertNotIn("Qué hacer:", report, "nothing to do, no advice")


# --------------------------------------------------------------------------------------------------- what the person sees
@tagged("post_install", "-at_install")
class TestDigestReport(DigestCase):
    def test_every_section_has_a_plain_recommendation(self):
        self.ask(UNANSWERED)
        for _index in range(2):
            self.vote("down", self.doc)
        self.Insight._record(row(document_ids=[self.doc.id, self.doc2.id], conflict=True))
        stale = new_document(self.env, self.source, "Política vieja", "texto")
        oldest = self.Insight._record(row(document_ids=[]))
        self.env.cr.execute("UPDATE buddy_insight SET create_date = now() - interval '100 days' WHERE id = %s", (oldest.id,))
        self.env.cr.execute("UPDATE buddy_document SET create_date = now() - interval '120 days' WHERE id = %s", (stale.id,))
        self.source.sudo().write({"state": "error"})
        report = self.report(self.admin)
        for fragment in ("Preguntas sin respuesta", "Documentá cómo se hace «backup", "Documentos con más votos 👎", "Revisá «PR 38 Backups».",
                         "Respuestas dudosas o contradictorias", "Unificá lo que dicen los documentos", "Documentos sin uso", "Fijate si siguen vigentes",
                         "Carpetas con problemas", "Abrí Buddy → Carpetas de Drive"):
            self.assertIn(fragment, report)
        self.assertEqual(report.count("Qué hacer:"), 5)
        self.assertIn("6 preguntas sin respuesta · 2 votos 👎 · 1 respuesta dudosa · 1 documento sin uso · 1 carpeta con problemas", report)
        self.assertIn("Esta semana:", report)
        self.assertIn("Empezá por «Preguntas sin respuesta».", report)
        order = [report.index(title) for title in ("Preguntas sin respuesta</h2>", "Documentos con más votos 👎</h2>", "Respuestas dudosas o contradictorias</h2>",
                                                   "Carpetas con problemas</h2>", "Documentos sin uso</h2>")]
        self.assertEqual(order, sorted(order), "what asks for work comes first, the list of unused documents last")

    def test_the_singular_is_written_as_a_singular(self):
        self.ask(["¿Cómo restauro un backup?"])
        self.vote("down", self.doc)
        self.Insight._record(row(document_ids=[self.doc.id], conflict=True))
        report = self.report(self.admin)
        for fragment in ("1 pregunta no tuvo respuesta esta semana.", "Otra pregunta suelta, sin un tema en común.", "1 respuesta salió dudosa esta semana.",
                         "En 1 los documentos se contradecían.", "1 pregunta sin respuesta · 1 voto 👎 · 1 respuesta dudosa"):
            self.assertIn(fragment, report)
        for broken in ("1 preguntas", "1 respuestas", "1 archivos", "Otras 1 ", "(0 con"):
            self.assertNotIn(broken, report)

    def test_people_late_without_a_step_in_common_do_not_get_an_all_clear(self):
        journey = make_journey(self.env, steps=[(0, "Hecho por casi todos"), (3, "Ya pasó")])
        for index, user in enumerate(self.readers):
            assignment = assign(self.env, user, journey, start=today_of(self.env, user) - timedelta(days=2))
            if index:
                assignment.progress_ids.filtered(lambda line: line.step_id.name == "Hecho por casi todos").with_user(user).write({"done": True})
        report = self.report(self.admin)
        self.assertIn("Onboarding atrasado", report)
        self.assertIn("Hay personas atrasadas, pero no en un mismo paso.", report)
        self.assertNotIn("Todo al día", report)

    def test_a_topic_is_read_as_a_phrase(self):
        self.ask(["¿Cómo pido un reembolso de gastos?", "Necesito el reembolso de gastos de viaje", "¿Cuándo cobro el reembolso de gastos?"])
        report = self.report(self.admin)
        self.assertRegex(report, r"Documentá cómo se hace «[a-zñáéíóú]+( y [a-zñáéíóú]+|, [a-zñáéíóú]+ y [a-zñáéíóú]+)?»")
        self.assertNotIn(" · gastos", report)
    def test_the_stored_data_has_ids_and_counts_never_a_title(self):
        self.ask(UNANSWERED)
        self.vote("down", self.doc)
        self.Insight._record(row(document_ids=[self.doc.id], conflict=True))
        dump = self.generate().sudo().data
        for title in ("PR 38", "Backups", "Guía de ingreso", "Carpeta abierta"):
            self.assertNotIn(title, dump)

    def test_the_examples_hide_mails_links_and_long_numbers(self):
        self.ask(["Soy ana@empresa.com y mi DNI 30123456, ¿cómo restauro un backup?", "¿Cómo restauro un backup? Mirá https://x.com/a",
                  "Necesito restaurar el backup 11 5555-1234"])
        digest = self.generate()
        for text in (digest.sudo().data, digest.with_user(self.admin).report):
            for secret in ("ana@empresa.com", "30123456", "5555", "https://x.com"):
                self.assertNotIn(secret, text)

    def test_a_manager_never_learns_the_title_of_a_document_of_a_folder_they_cannot_read(self):
        group = new_group(self.env, "Dirección")
        secret = new_document(self.env, new_source(self.env, "Dirección", [group]), "Sueldos de dirección", "texto")
        for _index in range(2):
            self.vote("down", self.doc)
        self.Insight._record(row(document_ids=[secret.id, self.doc.id], conflict=True))
        digest = self.generate()
        manager_report = digest.with_user(self.manager).report
        self.assertIn("PR 38 Backups", manager_report, "a document the manager can read is named")
        self.assertNotIn("Sueldos de dirección", manager_report)
        self.assertIn("Sueldos de dirección", digest.with_user(self.admin).report)
        allowed = new_test_user(self.env, login="buddy_d_gestor_dir", groups=MANAGER)
        allowed.groups_id = [(4, group.id)]
        self.assertIn("Sueldos de dirección", digest.with_user(allowed).report, "and the one who may read the folder sees it")

    def test_unused_documents_of_a_closed_folder_are_not_named_to_a_manager_who_cannot_read_it(self):
        group = new_group(self.env, "Cerrada")
        hidden = new_document(self.env, new_source(self.env, "Cerrada", [group]), "Contrato reservado", "texto")
        oldest = self.Insight._record(row(document_ids=[]))
        self.env.cr.execute("UPDATE buddy_insight SET create_date = now() - interval '100 days' WHERE id = %s", (oldest.id,))
        self.env.cr.execute("UPDATE buddy_document SET create_date = now() - interval '120 days' WHERE id IN %s", ((hidden.id, self.doc.id),))
        digest = self.generate()
        self.assertNotIn("Contrato reservado", digest.with_user(self.manager).report)
        self.assertIn("PR 38 Backups", digest.with_user(self.manager).report)
        self.assertIn("Contrato reservado", digest.with_user(self.admin).report)
        # The total counts every folder: a manager is told only about the documents they may read (no «there are N in folders you cannot see»).
        self.assertIn("2 documentos no se citaron", digest.with_user(self.admin).report)
        self.assertIn("1 documento no se citó", digest.with_user(self.manager).report)
        self.assertNotIn("2 documento", digest.with_user(self.manager).report)
        self.assertIn("1 documento sin uso", digest.with_user(self.manager).report)
        self.assertIn("2 documentos sin uso", digest.with_user(self.admin).report)

    def test_the_folders_and_the_onboarding_are_for_administrators_only(self):
        self.source.sudo().write({"state": "error"})
        journey = make_journey(self.env, steps=[(0, "Alta de accesos"), (1, "Charla")])
        for user in self.readers:
            assign(self.env, user, journey, start=today_of(self.env, user) - timedelta(days=5))
        digest = self.generate()
        admin_report, manager_report = digest.with_user(self.admin).report, digest.with_user(self.manager).report
        for fragment in ("Carpetas con problemas", "Onboarding atrasado", "Alta de accesos", "Carpeta abierta"):
            self.assertIn(fragment, admin_report)
            self.assertNotIn(fragment, manager_report)
        self.assertIn("los ven solo los administradores", manager_report)
        self.assertNotIn("los ven solo los administradores", admin_report)

    def test_the_report_is_text_never_html_from_the_stored_data(self):
        digest = self.generate()
        evil = {"version": 1, "window": {"from": "2026-10-01", "to": "2026-10-08"},
                "unanswered": {"total": 3, "groups": [{"terms": ["<script>alert(1)</script>", "<img src=x onerror=y>"], "count": 3,
                                                       "examples": ["<script>alert(2)</script>"]}], "ungrouped": "x", "without_text": -5},
                "downvotes": {"down": 1, "up": 0, "items": ["no", {"doc": "<b>", "down": "<i>"}, {"doc": self.doc.id, "down": True}]},
                "partial": ["<script>", "unanswered"], "unknown_section": {"x": 1}}
        digest.sudo().write({"data": json.dumps(evil)})
        digest.invalidate_recordset()
        report = digest.with_user(self.admin).report
        self.assertNotIn("<script>", report)
        self.assertNotIn("<img", report)
        self.assertIn("&lt;script&gt;", report)

    def test_garbage_in_the_data_is_an_empty_summary_not_an_error(self):
        digest = self.generate()
        for raw in ("no es json", "[]", "5", '{"version": 99}', "{" * 1000, False):
            digest.sudo().write({"data": raw})
            digest.invalidate_recordset()
            self.assertIn("vacío", digest.with_user(self.admin).report, repr(raw)[:20])

    def test_the_report_says_it_was_made_without_ai_and_without_names(self):
        report = self.report(self.admin)
        self.assertIn("sin inteligencia artificial", report)
        self.assertIn("nadie sabe quién preguntó ni quién votó", report)

    def test_a_partial_summary_says_which_parts_are_missing_but_not_the_secret_ones_to_a_manager(self):
        digest = self.generate()
        data = self.data_of(digest)
        data["partial"] = ["doubtful", "onboarding"]
        digest.sudo().write({"data": json.dumps(data)})
        digest.invalidate_recordset()
        self.assertIn("Algunas partes no se pudieron calcular esta vez (respuestas dudosas)", digest.with_user(self.manager).report)
        self.assertNotIn("onboarding atrasado", digest.with_user(self.manager).report)
        self.assertIn("Algunas partes no se pudieron calcular esta vez (respuestas dudosas, onboarding atrasado)", digest.with_user(self.admin).report)


# ------------------------------------------------------------------------------------------------------- who may do what
@tagged("post_install", "-at_install")
class TestDigestAccess(DigestCase):
    def test_a_manager_and_an_administrator_can_read_it_and_nobody_else(self):
        digest = self.generate()
        for user in (self.manager, self.admin):
            self.assertTrue(digest.with_user(user).exists())
            self.assertEqual(self.Digest.with_user(user).search_count([]), 1)
        for user in (self.ana, self.rosa):
            with self.assertRaises(AccessError):
                self.Digest.with_user(user).search([])
            with self.assertRaises(AccessError):
                digest.with_user(user).read(["date_from"])

    def test_the_raw_data_is_the_administrators_and_nobody_can_search_by_it(self):
        digest = self.generate()
        self.assertTrue(digest.with_user(self.admin).data)
        with self.assertRaises(AccessError):
            digest.with_user(self.manager).data  # noqa: B018
        with self.assertRaises(AccessError):
            digest.with_user(self.manager).read(["data"])
        for domain in ([("data", "ilike", "doc")], [("data", "!=", False)], ["!", "!", ("data", "ilike", "x")]):
            with self.subTest(domain=domain), self.assertRaises(AccessError):
                self.Digest.with_user(self.manager).search(domain)
        with self.assertRaises(AccessError):
            self.Digest.with_user(self.manager).search([], order="data")

    def test_the_figures_of_the_folders_and_of_onboarding_are_the_administrators_too(self):
        digest = self.generate()
        for name in ("unused_count", "problem_count", "late_count"):
            self.assertEqual(digest.with_user(self.admin)[name], 0)
            with self.subTest(field=name):
                with self.assertRaises(AccessError):
                    digest.with_user(self.manager)[name]  # noqa: B018
                with self.assertRaises(AccessError):
                    self.Digest.with_user(self.manager).search([(name, ">", 0)])
                with self.assertRaises(AccessError):
                    self.Digest.with_user(self.manager).read_group([], [name], [])
                with self.assertRaises(AccessError):
                    self.Digest.with_user(self.manager)._read_group([], [], [name + ":sum"])
        self.assertEqual(digest.with_user(self.manager).unanswered_count, 0, "the counts of the rest are theirs to read")

    def test_the_list_a_manager_opens_has_no_column_they_may_not_read(self):
        self.generate()
        for name in ("unused_count", "problem_count", "late_count"):
            self.assertIn(name, self.Digest.with_user(self.admin).get_view(view_type="list")["arch"], name)
            self.assertNotIn(name, self.Digest.with_user(self.manager).get_view(view_type="list")["arch"], name)
        rows = self.Digest.with_user(self.manager).web_search_read([], {"date_from": {}, "unanswered_count": {}, "downvote_count": {}, "doubtful_count": {}})
        self.assertEqual(len(rows["records"]), 1)
        self.Digest.with_user(self.manager).get_view(view_type="form")

    def test_nobody_writes_a_summary_through_the_interface(self):
        digest = self.generate()
        for user in (self.manager, self.admin, self.ana):
            with self.assertRaises(AccessError):
                self.Digest.with_user(user).create({"date_from": self.run_day, "date_to": self.run_day})
            with self.assertRaises(AccessError):
                digest.with_user(user).write({"partial": True})
        with self.assertRaises(AccessError):
            digest.with_user(self.manager).unlink()
        digest.with_user(self.admin).unlink()  # an administrator may delete one
        self.assertFalse(digest.exists())

    def test_nobody_follows_a_summary(self):
        digest = self.generate()
        self.assertEqual(digest._message_subscribe(partner_ids=[self.admin.partner_id.id]), self.env["mail.followers"])
        self.assertFalse(digest.message_follower_ids)


# ------------------------------------------------------------------------------------------------- the notice (an activity)
@tagged("post_install", "-at_install")
class TestDigestNotice(DigestCase):
    def setUp(self):
        super().setUp()
        self.Activity = self.env["mail.activity"].sudo()
        self.type = self.env.ref("buddy_ia.activity_type_digest")

    def activities(self, user=None):
        domain = [("res_model", "=", "buddy.digest"), ("activity_type_id", "=", self.type.id)]
        if user:
            domain.append(("user_id", "=", user.id))
        return self.Activity.search(domain)

    def test_the_person_who_looks_after_the_tickets_gets_one_activity_and_no_mail(self):
        self.P.set_param("buddy_ia.ticket_user_id", str(self.manager.id))
        mails, messages = self.env["mail.mail"].sudo().search_count([]), self.env["mail.message"].sudo().search_count([])
        digest = self.Digest._cron_weekly()
        self.assertTrue(digest)
        (activity,) = self.activities()
        self.assertEqual((activity.user_id, activity.res_id), (self.manager, digest.id))
        self.assertEqual(activity.date_deadline, fields.Date.context_today(self.Digest) + timedelta(days=7))
        self.assertIn("Resumen semanal de Buddy", activity.summary)
        self.assertEqual(self.env["mail.mail"].sudo().search_count([]), mails, "no e-mail is queued")
        self.assertEqual(self.env["mail.message"].sudo().search_count([]), messages, "and no «assigned to you» notification either")

    def test_without_a_ticket_responsible_the_administrators_are_told(self):
        self.Digest._cron_weekly()
        self.assertIn(self.admin, self.activities().mapped("user_id"))
        self.assertNotIn(self.manager, self.activities().mapped("user_id"))

    def test_the_choice_in_the_settings_is_respected(self):
        self.P.set_param("buddy_ia.ticket_user_id", str(self.manager.id))
        self.P.set_param("buddy_ia.digest_notify", "admins")
        self.Digest._cron_weekly()
        self.assertIn(self.admin, self.activities().mapped("user_id"))
        self.assertNotIn(self.manager, self.activities().mapped("user_id"))
        self.Digest.sudo().search([]).unlink()
        self.P.set_param("buddy_ia.digest_notify", "none")
        digest = self.Digest._cron_weekly()
        self.assertTrue(digest, "the summary is saved anyway")
        self.assertFalse(self.activities())

    def test_somebody_who_could_not_open_the_summary_is_not_told(self):
        self.P.set_param("buddy_ia.ticket_user_id", str(self.ana.id))
        self.Digest._cron_weekly()
        self.assertNotIn(self.ana, self.activities().mapped("user_id"))
        self.assertIn(self.admin, self.activities().mapped("user_id"), "the administrators are told instead")
        self.P.set_param("buddy_ia.ticket_user_id", "no es un número")
        self.assertTrue(self.Digest._recipients())

    def test_the_activity_of_last_week_gives_way_to_the_new_one(self):
        self.P.set_param("buddy_ia.digest_notify", "admins")
        first = self.generate()
        self.Digest._notify(first)
        self.assertEqual(self.activities(self.admin).mapped("res_id"), [first.id])
        second = self.Digest._generate(self.run_day + timedelta(days=7))[0]
        self.Digest._notify(second)
        self.assertEqual(self.activities(self.admin).mapped("res_id"), [second.id], "one open notice, not a pile")

    def test_the_cron_twice_the_same_day_makes_one_summary_and_one_notice(self):
        self.P.set_param("buddy_ia.digest_notify", "admins")
        self.assertTrue(self.Digest._cron_weekly())
        count = len(self.activities())
        self.assertFalse(self.Digest._cron_weekly())
        self.assertEqual(len(self.activities()), count)
        self.assertEqual(self.Digest.sudo().search_count([]), 1)

    def test_a_summary_asked_for_by_hand_does_not_stop_the_weekly_one_or_its_notice(self):
        self.P.set_param("buddy_ia.digest_notify", "admins")
        self.Digest._generate(manual=True)
        self.assertFalse(self.activities(), "asking for one notifies nobody")
        weekly = self.Digest._cron_weekly()
        self.assertTrue(weekly and not weekly.manual)
        self.assertIn(self.admin, self.activities().mapped("user_id"))

    def test_turning_the_summary_off_closes_the_notice_that_was_left_open(self):
        self.P.set_param("buddy_ia.digest_notify", "admins")
        self.Digest._cron_weekly()
        self.assertTrue(self.activities())
        settings = self.env["res.config.settings"].with_user(self.admin).create({"buddy_digest_enabled": False})
        settings.set_values()
        self.assertFalse(self.activities(), "a notice for a summary nobody is making any more")
        self.assertEqual(self.Digest.sudo().search_count([]), 1, "the summary itself stays")

    def test_the_administrator_can_turn_the_summary_off(self):
        self.P.set_param("buddy_ia.digest_enabled", "0")
        self.assertFalse(self.Digest._cron_weekly())
        self.assertEqual(self.Digest.sudo().search_count([]), 0)
        self.assertFalse(self.activities())
        self.P.set_param("buddy_ia.digest_enabled", "1")
        self.assertTrue(self.Digest._cron_weekly())

    def test_the_number_of_people_told_has_a_limit(self):
        self.P.set_param("buddy_ia.digest_notify", "admins")
        for index in range(3):
            new_test_user(self.env, login="buddy_d_admin%d" % index, groups="base.group_user,base.group_system")
        with patch.object(buddy_digest, "MAX_RECIPIENTS", 2):
            self.assertEqual(len(self.Digest._recipients()), 2)

    def test_a_person_who_fails_does_not_stop_the_rest(self):
        self.P.set_param("buddy_ia.digest_notify", "admins")
        digest = self.generate()
        new_test_user(self.env, login="buddy_d_admin_ok", groups="base.group_user,base.group_system")
        recipients = self.Digest._recipients()
        self.assertGreaterEqual(len(recipients), 2)
        original, calls = type(self.Digest)._notice_texts, []

        def flaky(this):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return original(this)

        with patch.object(type(self.Digest), "_notice_texts", flaky), mute_logger("odoo.addons.buddy_ia.models.buddy_digest"):
            self.assertEqual(self.Digest._notify(digest), len(recipients) - 1)
        self.assertEqual(len(self.activities()), len(recipients) - 1)

    def test_the_manual_button_does_not_notify_anybody(self):
        settings = self.env["res.config.settings"].with_user(self.admin).create({})
        action = settings.action_buddy_digest_now()
        self.assertEqual((action["res_model"], action["view_mode"]), ("buddy.digest", "form"))
        self.assertFalse(self.activities())
        asked = self.Digest.sudo().browse(action["res_id"])
        self.assertEqual((asked.manual, asked.date_last), (True, fields.Date.today()))


# ------------------------------------------------------------------------------------------------------------ retention
@tagged("post_install", "-at_install")
class TestDigestRetention(DigestCase):
    def test_the_summaries_are_kept_as_long_as_the_statistics(self):
        old = self.Digest._generate(fields.Date.today() - timedelta(days=100))[0]
        recent = self.Digest._generate(fields.Date.today() - timedelta(days=10))[0]
        self.Insight._cron_purge()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.P.set_param("buddy_ia.insight_days", "5")
        self.Insight._cron_purge()
        self.assertFalse(recent.exists())

    def test_a_summary_goes_when_its_whole_week_is_past_the_limit_not_only_its_last_day(self):
        # Made 8 days ago it counts the 7 days before that: its oldest questions are 15 days old (the examples come from the statistics).
        digest = self.Digest._generate(fields.Date.today() - timedelta(days=8))[0]
        self.P.set_param("buddy_ia.insight_days", "10")
        self.Insight._cron_purge()
        self.assertFalse(digest.exists(), "the first day it counts is already past 10 days")
        again = self.Digest._generate(fields.Date.today() - timedelta(days=8))[0]
        self.P.set_param("buddy_ia.insight_days", "20")
        self.Insight._cron_purge()
        self.assertTrue(again.exists())

    def test_a_bad_retention_never_means_keep_forever(self):
        old = self.Digest._generate(fields.Date.today() - timedelta(days=100))[0]
        self.P.set_param("buddy_ia.insight_days", "no es un número")
        self.Insight._cron_purge()
        self.assertFalse(old.exists(), "falls back to 90 days")

    def test_deleting_the_statistics_deletes_the_summaries_too(self):
        self.generate()
        self.Insight._delete_everything()
        self.assertEqual(self.Digest.sudo().search_count([]), 0)

    def test_the_button_of_the_settings_says_it_deleted_the_summaries(self):
        self.generate()
        settings = self.env["res.config.settings"].with_user(self.admin).create({})
        action = settings.action_buddy_delete_insights()
        self.assertIn("resúmenes semanales", action["params"]["message"])
        self.assertEqual(self.Digest.sudo().search_count([]), 0)


# ---------------------------------------------------------------------------------------------------------------- settings
@tagged("post_install", "-at_install")
class TestDigestSettings(TransactionCase):
    def setUp(self):
        super().setUp()
        self.P = self.env["ir.config_parameter"].sudo()
        for key in ("proactive_chips", "proactive_nudge", "digest_enabled", "digest_notify"):
            self.P.search([("key", "=", "buddy_ia." + key)]).unlink()
        self.admin = new_test_user(self.env, login="buddy_ds_admin", groups="base.group_user,base.group_system")
        self.employee = new_test_user(self.env, login="buddy_ds_emp", groups="base.group_user")
        self.manager = new_test_user(self.env, login="buddy_ds_man", groups=MANAGER)

    def test_everything_is_on_by_default_and_the_notice_goes_to_the_ticket_responsible(self):
        values = self.env["res.config.settings"].get_values()
        self.assertEqual([values[name] for name in ("buddy_proactive_chips", "buddy_proactive_nudge", "buddy_digest_enabled")], [True, True, True])
        self.assertEqual(self.env["res.config.settings"].create({}).buddy_digest_notify, "responsible")

    def test_the_switches_are_saved_as_explicit_values_and_come_back(self):
        settings = self.env["res.config.settings"].with_user(self.admin).create({"buddy_proactive_chips": False, "buddy_proactive_nudge": False,
                                                                                   "buddy_digest_enabled": False, "buddy_digest_notify": "none"})
        settings.set_values()
        self.assertEqual([self.P.get_param("buddy_ia." + key) for key in ("proactive_chips", "proactive_nudge", "digest_enabled", "digest_notify")],
                         ["0", "0", "0", "none"])
        values = self.env["res.config.settings"].get_values()
        self.assertEqual([values[name] for name in ("buddy_proactive_chips", "buddy_proactive_nudge", "buddy_digest_enabled")], [False, False, False])
        again = self.env["res.config.settings"].with_user(self.admin).create({"buddy_proactive_chips": True, "buddy_proactive_nudge": True,
                                                                                "buddy_digest_enabled": True, "buddy_digest_notify": "admins"})
        again.set_values()
        self.assertEqual([self.P.get_param("buddy_ia." + key) for key in ("proactive_chips", "proactive_nudge", "digest_enabled", "digest_notify")],
                         ["1", "1", "1", "admins"])

    def test_the_new_buttons_are_for_administrators_only(self):
        for user in (self.employee, self.manager):
            settings = self.env["res.config.settings"].with_user(user).browse()
            for name in ("action_buddy_digest_now", "action_buddy_open_digests"):
                with self.subTest(user=user.login, button=name), self.assertRaises(AccessError):
                    getattr(settings, name)()
            with self.assertRaises(AccessError):
                self.env["buddy.digest"].with_user(user)._generate_now()

    def test_the_administrator_gets_the_summary_of_today_and_asking_again_remakes_the_same(self):
        settings = self.env["res.config.settings"].with_user(self.admin).create({})
        first = settings.action_buddy_digest_now()
        second = settings.action_buddy_digest_now()
        self.assertEqual(first["res_id"], second["res_id"])
        self.assertEqual(self.env["buddy.digest"].sudo().search_count([("id", "=", first["res_id"])]), 1)
        self.assertTrue(self.env["buddy.digest"].sudo().browse(first["res_id"]).manual)
        self.assertEqual(settings.action_buddy_open_digests()["res_model"], "buddy.digest")

    def test_the_cron_the_activity_type_and_the_menu_exist(self):
        cron = self.env.ref("buddy_ia.buddy_cron_digest")
        self.assertEqual((cron.interval_type, cron.interval_number, cron.active, cron.model_id.model), ("weeks", 1, True, "buddy.digest"))
        self.assertEqual((cron.nextcall.weekday(), cron.nextcall.hour), (0, 11), "a Monday at 11:00 UTC, 08:00 in Argentina")
        self.assertEqual(self.env.ref("buddy_ia.activity_type_digest").res_model, "buddy.digest")
        menu = self.env.ref("buddy_ia.buddy_menu_digest")
        self.assertEqual(menu.parent_id, self.env.ref("buddy_ia.buddy_menu_insights"))
        self.assertEqual(set(menu.groups_id), {self.env.ref("base.group_system"), self.env.ref("buddy_ia.group_buddy_manager")})
