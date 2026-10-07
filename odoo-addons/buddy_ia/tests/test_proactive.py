"""The suggestions of the chat chips that fit the person (E4b): the questions of their own onboarding steps and the topics the
company asks about most. What they may and may not reveal, the switch of the administrator, and the route of the widget."""
import json
import re
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, new_test_user, tagged
from odoo.tools import mute_logger

from .common import new_document, new_group, new_source, new_user
from .test_insight import row
from .test_journey import JourneyCase, assign, make_journey

TOPIC = "¿Qué dice «%s»?"


class ProactiveCase(JourneyCase):
    def setUp(self):
        super().setUp()
        self.Proactive = self.env["buddy.proactive"]
        self.Proactive._clear_cache()
        self.addCleanup(self.Proactive._clear_cache)
        for key in ("proactive_chips", "feedback_min_audience", "insight_days", "insight_questions"):
            self.P.search([("key", "=", "buddy_ia." + key)]).unlink()
        self.env["buddy.insight"].sudo().search([]).unlink()
        # A folder that five people read (the minimum audience) and a person who is one of them.
        self.group = new_group(self.env, "Lectores")
        self.readers = [new_user(self.env, "lector%d" % index, extra_groups=(self.group,)) for index in range(5)]
        self.reader = self.readers[0]
        self.source = new_source(self.env, "Carpeta abierta", [self.group])
        self.doc = new_document(self.env, self.source, "PR 38 Backups.docx", "texto de backups")

    def suggestions(self, user, kind=None):
        found = self.Proactive.with_user(user).suggestions()["suggestions"]
        return [chip for chip in found if kind is None or chip["kind"] == kind]

    def texts(self, user, kind=None):
        return [chip["text"] for chip in self.suggestions(user, kind)]

    def cite(self, documents, times, days=(1, 2, 3), **over):
        """`times` queries that cited `documents`, spread over the CLOSED days `days` ago (today is never counted for the topics)."""
        for index in range(times):
            record = self.env["buddy.insight"]._record(row(document_ids=documents.ids, **over))
            if days:
                self.env.cr.execute("UPDATE buddy_insight SET create_date = now() - %s * interval '1 day' WHERE id = %s", (days[index % len(days)], record.id))

    def age_insights(self, days):
        self.env.cr.execute("UPDATE buddy_insight SET create_date = create_date - %s * interval '1 day'", (days,))


# ------------------------------------------------------------------------------------------------- the topics of the company
@tagged("post_install", "-at_install")
class TestTopics(ProactiveCase):
    def test_a_document_cited_by_many_queries_on_several_days_is_offered_to_the_people_who_may_read_it(self):
        self.cite(self.doc, 10)
        self.assertEqual(self.texts(self.reader, "topic"), [TOPIC % "PR 38 Backups"])

    def test_with_fewer_than_ten_queries_nothing_is_said(self):
        self.cite(self.doc, 9)
        self.assertEqual(self.suggestions(self.reader), [])
        self.cite(self.doc, 1)
        self.Proactive._clear_cache()
        self.assertEqual(len(self.suggestions(self.reader)), 1)

    def test_a_long_afternoon_of_one_person_is_not_a_topic_of_the_company(self):
        # Fifty queries, but on only two different days: not enough days to tell a topic from somebody's afternoon.
        self.cite(self.doc, 50, days=(1, 2))
        self.assertEqual(self.suggestions(self.reader), [])
        self.cite(self.doc, 3, days=(5,))
        self.Proactive._clear_cache()
        self.assertEqual(len(self.suggestions(self.reader)), 1, "three different days")

    def test_todays_queries_are_never_counted_so_nobody_sees_when_a_document_was_consulted(self):
        self.cite(self.doc, 20, days=())
        self.assertEqual(self.suggestions(self.reader), [])
        self.cite(self.doc, 10)
        self.Proactive._clear_cache()
        self.assertEqual(len(self.suggestions(self.reader)), 1)
        self.cite(self.doc, 500, days=())  # a burst of today changes nothing: the topics only move with the closed days
        self.Proactive._clear_cache()
        self.assertEqual(self.texts(self.reader, "topic"), [TOPIC % "PR 38 Backups"])

    def test_only_the_last_thirty_days_count(self):
        self.cite(self.doc, 10)
        self.age_insights(31)
        self.Proactive._clear_cache()
        self.assertEqual(self.suggestions(self.reader), [])

    def test_queries_nobody_could_answer_and_failed_ones_do_not_count(self):
        self.cite(self.doc, 8)
        self.cite(self.doc, 1, needs_human=True)
        self.cite(self.doc, 1, error="provider_error")
        self.assertEqual(self.suggestions(self.reader), [])

    def test_a_person_who_cannot_read_the_folder_is_not_told_the_title(self):
        self.cite(self.doc, 12)
        self.assertEqual(self.suggestions(self.ana), [], "the document exists and is popular, but ana may not read it")
        self.assertEqual(len(self.suggestions(self.reader)), 1)

    def test_a_folder_that_fewer_than_five_people_read_never_shows_up_whatever_its_count(self):
        small_group = new_group(self.env, "Chicos")
        members = [new_user(self.env, "chico%d" % index, extra_groups=(small_group,)) for index in range(4)]
        small = new_document(self.env, new_source(self.env, "Chica", [small_group]), "Sueldos del equipo", "texto")
        self.cite(small, 40)
        self.assertEqual(self.suggestions(members[0]), [], "with four possible readers a count of 40 would describe somebody")
        members.append(new_user(self.env, "chico5", extra_groups=(small_group,)))
        self.Proactive._clear_cache()
        self.assertEqual(self.texts(members[0], "topic"), [TOPIC % "Sueldos del equipo"])

    def test_the_minimum_audience_can_be_raised_with_the_one_of_the_votes_but_never_lowered_below_five(self):
        small_group = new_group(self.env, "Dos")
        pair = [new_user(self.env, "dos%d" % index, extra_groups=(small_group,)) for index in range(2)]
        small = new_document(self.env, new_source(self.env, "Pareja", [small_group]), "Solo dos", "texto")
        self.cite(small, 12)
        self.assertEqual(self.suggestions(pair[0]), [])
        for value in ("1", "2"):
            self.P.set_param("buddy_ia.feedback_min_audience", value)  # «1» turns the protection of the votes off: not the one of the topics
            self.Proactive._clear_cache()
            self.assertEqual(self.suggestions(pair[0]), [], value)
        self.cite(self.doc, 12)
        self.P.set_param("buddy_ia.feedback_min_audience", "6")
        self.Proactive._clear_cache()
        self.assertEqual(self.suggestions(self.reader), [], "raised to six: the five readers of the folder are not enough")

    def test_an_archived_folder_and_a_document_that_is_not_ready_are_not_offered(self):
        other = new_document(self.env, self.source, "Documento vacío", "texto", state="empty")
        self.cite(self.doc | other, 12)
        self.assertEqual(self.texts(self.reader, "topic"), [TOPIC % "PR 38 Backups"])
        self.source.active = False
        self.Proactive._clear_cache()
        self.assertEqual(self.suggestions(self.reader), [])

    def test_the_most_cited_come_first_and_at_most_four_are_offered(self):
        documents = [new_document(self.env, self.source, "Doc %d" % index, "texto") for index in range(6)]
        for index, document in enumerate(documents):
            self.cite(document, 10 + index)
        self.assertEqual(self.texts(self.reader, "topic"), [TOPIC % ("Doc %d" % index) for index in (5, 4, 3, 2)])

    def test_nothing_but_titles_leaves_the_server_never_a_count_a_question_or_a_name(self):
        self.P.set_param("buddy_ia.insight_questions", "True")
        self.cite(self.doc, 12, question="Mi pregunta secreta sobre el sueldo de Zulema", needs_human=False)
        payload = self.Proactive.with_user(self.reader).suggestions()
        dump = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secreta", dump)
        self.assertNotIn("Zulema", dump)
        self.assertEqual(set(payload), {"suggestions"})
        for chip in payload["suggestions"]:
            self.assertEqual(set(chip), {"kind", "text", "label"})
            self.assertFalse(re.search(r"\d{2,}", chip["text"].replace("38", "")), "no counts: " + chip["text"])

    def test_a_long_title_is_cut_to_fit_a_button_and_a_hostile_one_is_one_plain_line(self):
        long = new_document(self.env, self.source, "Procedimiento de alta de proveedores nuevos con aprobación del área de compras y finanzas.pdf", "t")
        hostile = new_document(self.env, self.source, "Línea uno\nLínea dos\x07<img src=x>", "t")
        self.cite(long | hostile, 12)
        chips = self.suggestions(self.reader, "topic")
        self.assertEqual(len(chips), 2)
        for chip in chips:
            self.assertLessEqual(len(chip["label"]), 80, "the button is short")
            self.assertLessEqual(len(chip["text"]), 200)
            self.assertNotIn("\n", chip["text"])
            self.assertNotIn("\x07", chip["text"])
        question = [chip["text"] for chip in chips if "Procedimiento" in chip["text"]][0]
        self.assertEqual(question, "¿Qué dice «Procedimiento de alta de proveedores nuevos con aprobación del área de compras y finanzas»?",
                         "the question carries the whole title: a cut one would search for something else")
        hostile_text = [chip["text"] for chip in chips if "Línea" in chip["text"]][0]
        self.assertEqual(hostile_text, "¿Qué dice «Línea uno Línea dosimg srcx»?", "reduced to plain words")

    def test_the_topics_are_computed_once_and_kept_for_everybody(self):
        self.cite(self.doc, 12)
        calls = []
        cls = type(self.Proactive)
        original = cls._compute_topics

        def counting(this):
            calls.append(1)
            return original(this)

        with patch.object(cls, "_compute_topics", counting):
            for user in self.readers + [self.ana, self.beto]:
                self.suggestions(user)
            self.assertEqual(len(calls), 1, "seven people, one computation")
            self.Proactive._clear_cache()
            self.suggestions(self.reader)
            self.assertEqual(len(calls), 2)

    def test_deleting_the_statistics_also_forgets_the_topics(self):
        self.cite(self.doc, 12)
        self.assertEqual(len(self.suggestions(self.reader)), 1)
        self.env["buddy.insight"]._delete_everything()
        self.assertEqual(self.suggestions(self.reader), [])

    def test_another_process_that_kept_the_topics_stops_offering_them_once_the_statistics_are_gone(self):
        self.cite(self.doc, 12)
        self.assertEqual(len(self.suggestions(self.reader)), 1)  # cached by this process
        self.env.cr.execute("DELETE FROM buddy_insight")  # what the button did in another worker: this process was not told
        self.assertEqual(self.suggestions(self.reader), [], "an empty table is checked on every call: the cache is not served")

    def test_a_failing_query_is_remembered_for_a_minute_instead_of_being_retried_by_every_request(self):
        self.cite(self.doc, 12)
        calls = []

        def broken(this):
            calls.append(1)
            raise RuntimeError("slow")

        with patch.object(type(self.Proactive), "_compute_topics", broken), mute_logger("odoo.addons.buddy_ia.models.buddy_proactive"):
            for _index in range(3):
                self.assertEqual(self.suggestions(self.reader), [])
        self.assertEqual(len(calls), 1, "three requests, one attempt")

    def test_a_failing_query_means_no_topics_and_the_chat_keeps_working(self):
        with patch.object(type(self.Proactive), "_compute_topics", side_effect=RuntimeError("boom")):
            self.assertEqual(self.Proactive.with_user(self.reader).suggestions(), {"suggestions": []})

    def test_the_administrator_can_turn_them_off(self):
        self.cite(self.doc, 12)
        self.assertEqual(len(self.suggestions(self.reader)), 1)
        self.P.set_param("buddy_ia.proactive_chips", "0")
        self.assertEqual(self.Proactive.with_user(self.reader).suggestions(), {"suggestions": []})
        self.P.set_param("buddy_ia.proactive_chips", "1")
        self.assertEqual(len(self.suggestions(self.reader)), 1)

    def test_only_internal_users_may_ask(self):
        portal = new_test_user(self.env, login="buddy_p_portal", groups="base.group_portal")
        with self.assertRaises(AccessError):
            self.Proactive.with_user(portal).suggestions()


# ------------------------------------------------------------------------------------------------- the onboarding steps
@tagged("post_install", "-at_install")
class TestStepQuestions(ProactiveCase):
    def setUp(self):
        super().setUp()
        self.journey = make_journey(self.env, steps=[
            (0, "Pedí tu día libre", {"kind": "ask", "question": "¿Cómo pido un día libre?"}),
            (0, "Leé la guía", {"kind": "read", "document_id": self.doc.id}),
            (3, "Tu equipo", {"question": "¿Dónde pido mi notebook?"}),
            (30, "Mucho después", {"question": "¿Y la evaluación?"}),
            (0, "Sin pregunta", {}),
        ])
        self.assignment = assign(self.env, self.reader, self.journey)

    def steps(self, user=None):
        return self.suggestions(user or self.reader)

    def test_the_questions_of_the_steps_due_today_come_first_and_the_next_ones_after(self):
        found = self.steps()
        self.assertEqual([(chip["kind"], chip["text"]) for chip in found],
                         [("step", "¿Cómo pido un día libre?"), ("step", TOPIC % "PR 38 Backups"), ("next", "¿Dónde pido mi notebook?")])

    def test_a_step_of_the_kind_ask_carries_the_id_of_the_progress_row_so_the_widget_can_tick_it(self):
        found = self.steps()
        row_ask = self.assignment.progress_ids.filtered(lambda line: line.step_id.name == "Pedí tu día libre")
        self.assertEqual(found[0]["ask_id"], row_ask.id)
        self.assertEqual([chip.get("ask_id") for chip in found[1:]], [None, None], "only the step that IS asking the buddy")

    def test_steps_far_away_or_without_a_question_are_not_offered(self):
        texts = self.texts(self.reader)
        self.assertNotIn("¿Y la evaluación?", texts)
        self.assertEqual(len(texts), 3)

    def test_what_is_done_is_not_offered_again(self):
        self.assignment.progress_ids.filtered(lambda line: line.step_id.name == "Pedí tu día libre").with_user(self.reader).write({"done": True})
        self.assertNotIn("¿Cómo pido un día libre?", self.texts(self.reader))

    def test_somebody_elses_steps_never_show_up(self):
        other = make_journey(self.env, steps=[(0, "Ajeno", {"question": "¿Cuánto gano yo?"})])
        assign(self.env, self.beto, other)
        self.assertNotIn("¿Cuánto gano yo?", self.texts(self.reader))
        self.assertEqual(self.texts(self.beto), ["¿Cuánto gano yo?"])
        self.assertEqual(self.suggestions(self.ana), [], "ana has no journey at all")

    def test_whoever_assigned_the_journey_does_not_get_the_questions_of_the_people_they_assigned(self):
        # Rosa (a manager of onboarding) assigned this journey to somebody: she reads their progress but it is not HER step.
        journey = make_journey(self.env, steps=[(0, "De otra persona", {"question": "¿Cuándo cobro?"})])
        self.Assignment.with_user(self.rosa).create({"user_id": self.ana.id, "journey_id": journey.id, "start_date": self.today})
        self.assertEqual(self.suggestions(self.rosa), [])
        self.assertEqual(self.texts(self.ana), ["¿Cuándo cobro?"])

    def test_a_cancelled_assignment_an_archived_journey_and_one_that_has_not_started_give_nothing(self):
        self.assignment.write({"state": "cancelled"})
        self.assertEqual(self.steps(), [])
        self.assignment.write({"state": "active"})
        self.assertEqual(len(self.steps()), 3)
        self.journey.active = False
        self.assertEqual(self.steps(), [])
        self.journey.active = True
        self.assignment.start_date = self.today + timedelta(days=5)
        self.assertEqual(self.steps(), [])

    def test_a_document_the_person_cannot_read_gives_no_chip_for_a_step_that_points_to_it(self):
        group = new_group(self.env, "Cerrada")
        hidden = new_document(self.env, new_source(self.env, "Cerrada", [group]), "Secreto de dirección", "texto")
        journey = make_journey(self.env, steps=[(0, "Leer secreto", {"kind": "read", "document_id": hidden.id})])
        assign(self.env, self.beto, journey)
        self.assertEqual(self.steps(self.beto), [])

    def test_the_same_question_is_not_offered_twice(self):
        self.cite(self.doc, 5)
        self.assertEqual(self.texts(self.reader).count(TOPIC % "PR 38 Backups"), 1, "the step «leer» and the topic ask the same thing")

    def test_a_step_question_is_one_clean_line(self):
        journey = make_journey(self.env, steps=[(0, "Rara", {"question": "¿Cómo  hago\n esto?"})])
        assign(self.env, self.beto, journey)
        self.assertEqual(self.texts(self.beto), ["¿Cómo hago esto?"])


# ------------------------------------------------------------------------------------------------- the route
@tagged("post_install", "-at_install")
class TestSuggestionsRoute(HttpCase):
    LOGIN = "buddy_sr_ana"

    def setUp(self):
        super().setUp()
        self.ana = new_test_user(self.env, login=self.LOGIN, groups="base.group_user")
        new_test_user(self.env, login="buddy_sr_portal", groups="base.group_portal")
        self.env["buddy.proactive"]._clear_cache()
        journey = make_journey(self.env, steps=[(0, "Hoy", {"question": "¿Cómo pido un día libre?"})])
        assign(self.env, self.ana, journey)

    def csrf(self):
        match = re.search(r'csrf_token: "([^"]+)"', self.url_open("/odoo").text)
        self.assertTrue(match)
        return match.group(1)

    def post(self, body, token="auto", content_type="application/json", login=None):
        self.authenticate(login or self.LOGIN, login or self.LOGIN)
        headers = {"Content-Type": content_type}
        if token == "auto":
            token = self.csrf()
        if token is not None:
            headers["X-CSRF-Token"] = token
        return self.url_open("/buddy/suggestions", data=body if isinstance(body, (str, bytes)) else json.dumps(body), headers=headers)

    def test_the_person_gets_their_own_suggestions(self):
        response = self.post({})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual([(chip["kind"], chip["text"]) for chip in data["suggestions"]], [("step", "¿Cómo pido un día libre?")])

    def test_content_type_csrf_and_method(self):
        for content_type in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x"):
            with self.subTest(content_type=content_type):
                self.assertEqual(self.post({}, content_type=content_type).status_code, 415)
        for token in (None, "", "basura"):
            with self.subTest(token=token):
                self.assertEqual(self.post({}, token=token).status_code, 400)
        self.authenticate(self.LOGIN, self.LOGIN)
        self.assertIn(self.url_open("/buddy/suggestions").status_code, (404, 405))

    def test_only_internal_users_and_the_session_of_the_token(self):
        stolen = self.csrf()
        self.assertIn(self.post({}, login="buddy_sr_portal").status_code, (400, 403))
        self.authenticate(None, None)
        anonymous = self.url_open("/buddy/suggestions", data="{}", headers={"Content-Type": "application/json"})
        self.assertNotIn("application/json", anonymous.headers.get("Content-Type", ""))
        new_test_user(self.env, login="buddy_sr_otra", groups="base.group_user")
        self.assertEqual(self.post({}, token=stolen, login="buddy_sr_otra").status_code, 400)

    def test_invalid_bodies_are_refused(self):
        for body in ({"extra": 1}, [1], "texto", None, {"suggestions": []}):
            with self.subTest(body=body):
                response = self.post(body)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["ok"])
        for raw in ("{", "\x00", "[" * 100 + "]" * 100):
            with self.subTest(raw=raw[:10]):
                self.assertEqual(self.post(raw).status_code, 400)

    def test_the_body_is_capped(self):
        self.assertEqual(self.post(json.dumps({"x": "a" * 1000})).status_code, 413)

    def test_it_is_rate_limited_per_person(self):
        from ..controllers import main
        with patch.object(main, "_SUGGESTION_READS", main.RateWindow(2, 60)):
            self.assertEqual(self.post({}).status_code, 200)
            self.assertEqual(self.post({}).status_code, 200)
            third = self.post({})
        self.assertEqual(third.status_code, 429)
        self.assertEqual(third.json()["error"], "rate_limited")

    def test_the_switch_of_the_administrator_empties_the_answer(self):
        self.env["ir.config_parameter"].sudo().set_param("buddy_ia.proactive_chips", "0")
        self.assertEqual(self.post({}).json(), {"ok": True, "suggestions": []})

    def test_the_config_tells_the_widget_what_the_administrator_allows(self):
        from ..controllers.main import public_config
        params = self.env["ir.config_parameter"].sudo()
        self.assertEqual(public_config(params.get_param)["proactive"], {"suggestions": True, "nudge": True})
        params.set_param("buddy_ia.proactive_nudge", "0")
        params.set_param("buddy_ia.proactive_chips", "False")
        self.assertEqual(public_config(params.get_param)["proactive"], {"suggestions": False, "nudge": False})
        params.set_param("buddy_ia.proactive_nudge", "1")
        self.assertEqual(public_config(params.get_param)["proactive"], {"suggestions": False, "nudge": True})
