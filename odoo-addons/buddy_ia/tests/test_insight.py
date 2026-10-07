"""Statistics of the queries (buddy.insight): what is stored and what is not, who can see it, retention, and the rows the
engine writes for answers, failures and streamed answers."""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged

from ..models.buddy_engine import INSIGHT_QUESTION_CHARS, BuddyBusy
from ..models.buddy_insight import DEFAULT_RETENTION_DAYS, QUESTION_LIMIT
from .common import new_document, new_group, new_source, new_user
from .test_engine import CHUNK, VALID, EngineCase

PARAMS = "ir.config_parameter"


def row(**over):
    """What the engine hands to buddy.insight._record."""
    data = {"question": "¿Cómo pido vacaciones?", "needs_human": False, "retrieved_count": 3, "best_score": 0.42,
            "document_ids": [], "document_titles": [], "model": "qa-chat", "tokens_in": 120, "tokens_out": 35,
            "latency_ms": 840, "error": False}
    data.update(over)
    return data


@tagged("post_install", "-at_install")
class TestInsightModel(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env[PARAMS].sudo()
        for key in ("insight_questions", "price_in_per_m", "price_out_per_m", "insight_days"):
            self.params.search([("key", "=", "buddy_ia." + key)]).unlink()
        self.Insight = self.env["buddy.insight"]

    # --- what is stored -------------------------------------------------------------------------------
    def test_a_row_has_numbers_and_never_says_who_asked(self):
        record = self.Insight._record(row())
        self.assertEqual((record.retrieved_count, record.best_score, record.model_name, record.tokens_in, record.tokens_out,
                          record.latency_ms), (3, 0.42, "qa-chat", 120, 35, 840))
        for name in ("user_id", "partner_id", "login"):
            self.assertNotIn(name, self.Insight._fields)
        self.assertFalse(record.error)

    def test_the_question_is_stored_only_when_nobody_answered_it_or_the_setting_is_on(self):
        answered = self.Insight._record(row())
        self.assertFalse(answered.question, "default: an answered question leaves no text")
        unanswered = self.Insight._record(row(needs_human=True))
        self.assertEqual(unanswered.question, "¿Cómo pido vacaciones?")
        self.assertTrue(unanswered.needs_human)
        self.params.set_param("buddy_ia.insight_questions", "True")
        self.assertEqual(self.Insight._record(row()).question, "¿Cómo pido vacaciones?", "«Guardar preguntas» keeps them all")
        self.params.set_param("buddy_ia.insight_questions", False)
        self.assertFalse(self.Insight._record(row()).question)

    def test_the_question_is_cleaned_and_bounded(self):
        record = self.Insight._record(row(needs_human=True, question="  hola\u0000 mundo\u0001  " + "x" * 2000))
        self.assertTrue(record.question.startswith("hola mundo"))
        self.assertLessEqual(len(record.question), QUESTION_LIMIT)
        self.assertEqual(QUESTION_LIMIT, INSIGHT_QUESTION_CHARS)
        self.assertFalse(self.Insight._record(row(needs_human=True, question="  \u0000 ")).question, "nothing left, nothing stored")

    def test_documents_are_the_ones_that_still_exist_and_their_titles_are_read_not_stored(self):
        source = new_source(self.env, "Insight", [new_group(self.env, "Ins")])
        document = new_document(self.env, source, "Guía de vacaciones", "texto")
        record = self.Insight._record(row(document_ids=[document.id, 987654321]))
        self.assertEqual(record.document_ids, document, "an id that no longer exists is dropped")
        self.assertEqual(record.document_titles, "Guía de vacaciones")
        self.assertFalse(self.Insight._fields["document_titles"].store, "titles are computed with the viewer's rights, never kept")
        document.unlink()
        self.assertFalse(record.document_ids, "deleting a document (every sync does) leaves the row intact")
        self.assertTrue(record.exists())

    def test_cost_uses_the_prices_at_the_time_and_is_zero_without_them(self):
        self.assertEqual(self.Insight._record(row()).cost, 0.0)
        self.params.set_param("buddy_ia.price_in_per_m", "3")
        self.params.set_param("buddy_ia.price_out_per_m", "15")
        record = self.Insight._record(row(tokens_in=1_000_000, tokens_out=200_000))
        self.assertAlmostEqual(record.cost, 3.0 + 3.0)
        self.params.set_param("buddy_ia.price_in_per_m", "10")
        self.assertAlmostEqual(record.cost, 6.0, msg="an old row keeps the price it was computed with")
        self.params.set_param("buddy_ia.price_in_per_m", "no-es-un-numero")
        self.params.set_param("buddy_ia.price_out_per_m", "-5")
        self.assertEqual(self.Insight._record(row(tokens_in=1000, tokens_out=1000)).cost, 0.0, "garbage prices count as 0")

    def test_provider_without_token_counts_leaves_zeros(self):
        record = self.Insight._record(row(tokens_in=None, tokens_out=None))
        self.assertEqual((record.tokens_in, record.tokens_out, record.cost), (0, 0, 0.0))

    def test_errors_are_a_closed_list_and_scores_stay_between_0_and_1(self):
        self.assertEqual(self.Insight._record(row(error="provider_timeout")).error, "provider_timeout")
        self.assertEqual(self.Insight._record(row(error="algo-raro")).error, "other")
        self.assertEqual(self.Insight._record(row(best_score=7)).best_score, 1.0)
        self.assertEqual(self.Insight._record(row(best_score=-3)).best_score, 0.0)

    # --- who can see it ------------------------------------------------------------------------------------
    def test_only_managers_and_administrators_read_the_rows_and_nobody_edits_them(self):
        record = self.Insight._record(row(needs_human=True))
        employee = new_test_user(self.env, login="buddy_ins_emp", groups="base.group_user")
        manager = new_test_user(self.env, login="buddy_ins_mgr", groups="base.group_user,buddy_ia.group_buddy_manager")
        admin = new_test_user(self.env, login="buddy_ins_adm", groups="base.group_user,base.group_system")
        with self.assertRaises(AccessError):
            self.Insight.with_user(employee).search([])
        with self.assertRaises(AccessError):
            record.with_user(employee).read(["question"])
        for user in (manager, admin):
            seen = self.Insight.with_user(user).search([])
            self.assertIn(record, seen, user.login)
            self.assertEqual(record.with_user(user).question, "¿Cómo pido vacaciones?")
            with self.assertRaises(AccessError):
                self.Insight.with_user(user).create({"question": "forjada"})
            with self.assertRaises(AccessError):
                record.with_user(user).write({"needs_human": False})
            with self.assertRaises(AccessError):
                record.with_user(user).unlink()
        self.assertTrue(record.exists())

    def test_the_manager_group_is_not_given_to_everybody_nor_implied_by_the_administrators(self):
        manager_group = self.env.ref("buddy_ia.group_buddy_manager")
        self.assertNotIn(manager_group, self.env.ref("base.group_system").implied_ids)
        employee = new_test_user(self.env, login="buddy_ins_emp2", groups="base.group_user")
        self.assertFalse(employee.has_group("buddy_ia.group_buddy_manager"))
        self.assertNotIn(manager_group, self.env.ref("base.group_user").implied_ids)

    def test_the_manager_does_not_gain_the_configuration(self):
        manager = new_test_user(self.env, login="buddy_ins_mgr2", groups="base.group_user,buddy_ia.group_buddy_manager")
        with self.assertRaises(AccessError):
            self.env["buddy.contact"].with_user(manager).create({"code": "x", "label": "x", "name": "x", "email": "x@x.com"})
        with self.assertRaises(AccessError):
            self.env["buddy.source"].with_user(manager).create({"name": "x", "folder_url": "x"})
        with self.assertRaises(AccessError):
            self.env["res.config.settings"].with_user(manager).browse().action_buddy_open_unanswered()

    def test_menus_and_actions_are_for_managers_and_unanswered_is_the_default_filter(self):
        both = self.env.ref("buddy_ia.group_buddy_manager") | self.env.ref("base.group_system")
        for xmlid in ("buddy_menu_insights", "buddy_menu_unanswered", "buddy_menu_usage"):
            self.assertEqual(self.env.ref("buddy_ia." + xmlid).groups_id, both, xmlid)
        action = self.env.ref("buddy_ia.buddy_insight_unanswered_action")
        self.assertIn("search_default_unanswered", action.context)
        self.assertEqual(action.name, "Preguntas sin respuesta")
        views = self.Insight.get_views([(False, "list"), (False, "form"), (False, "search"), (False, "graph"), (False, "pivot")])["views"]
        self.assertIn('name="unanswered"', views["search"]["arch"])
        self.assertIn('create="0"', views["list"]["arch"])
        self.assertIn('edit="0"', views["form"]["arch"])
        self.assertIn("day", views["graph"]["arch"])
        self.assertEqual(self.env.ref("buddy_ia.buddy_insight_usage_action").view_mode.split(",")[:2], ["graph", "pivot"])

    # --- retention -----------------------------------------------------------------------------------------
    def age(self, record, days):
        self.env.cr.execute("UPDATE buddy_insight SET create_date = %s WHERE id = %s",
                            (fields.Datetime.now() - timedelta(days=days), record.id))
        record.invalidate_recordset()

    def test_the_cron_deletes_rows_older_than_90_days_by_default(self):
        old, recent = self.Insight._record(row()), self.Insight._record(row())
        self.age(old, DEFAULT_RETENTION_DAYS + 1)
        self.age(recent, DEFAULT_RETENTION_DAYS - 1)
        self.Insight._cron_purge()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_retention_is_configurable_and_a_bad_value_never_means_keep_forever(self):
        record = self.Insight._record(row())
        self.age(record, 10)
        self.params.set_param("buddy_ia.insight_days", "30")
        self.Insight._cron_purge()
        self.assertTrue(record.exists())
        self.params.set_param("buddy_ia.insight_days", "7")
        self.Insight._cron_purge()
        self.assertFalse(record.exists())
        for bad in ("0", "-4", "abc"):
            fresh = self.Insight._record(row())
            self.age(fresh, DEFAULT_RETENTION_DAYS + 5 if bad != "0" else 4000)
            self.params.set_param("buddy_ia.insight_days", bad)
            self.Insight._cron_purge()
            self.assertFalse(fresh.exists(), bad)

    def test_the_purge_cron_is_installed_and_daily(self):
        cron = self.env.ref("buddy_ia.buddy_cron_purge_insight")
        self.assertEqual((cron.interval_number, cron.interval_type, cron.active), (1, "days", True))
        self.assertIn("_cron_purge", cron.code)

    def test_deleting_everything_is_for_administrators_through_the_settings_button(self):
        self.Insight._record(row(needs_human=True))
        self.Insight._record(row())
        manager = new_test_user(self.env, login="buddy_ins_mgr3", groups="base.group_user,buddy_ia.group_buddy_manager")
        with self.assertRaises(AccessError):
            self.env["res.config.settings"].with_user(manager).browse().action_buddy_delete_insights()
        self.assertGreaterEqual(self.Insight.search_count([]), 2)
        result = self.env["res.config.settings"].create({}).action_buddy_delete_insights()
        self.assertEqual(result["params"]["type"], "success")
        self.assertEqual(self.Insight.search_count([]), 0)

    def test_figures_for_the_settings_screen(self):
        self.assertEqual(self.Insight._usage_summary(fields.Datetime.now() - timedelta(days=1))[0], 0)
        self.params.set_param("buddy_ia.price_in_per_m", "10")
        self.Insight._record(row(tokens_in=1000, tokens_out=100))
        self.Insight._record(row(tokens_in=500, tokens_out=50))
        count, tokens_in, tokens_out, cost = self.Insight._usage_summary(fields.Datetime.now() - timedelta(days=1))
        self.assertEqual((count, tokens_in, tokens_out), (2, 1500, 150))
        self.assertAlmostEqual(cost, 0.015)
        self.assertAlmostEqual(self.Insight._spent_since(fields.Datetime.now() - timedelta(days=1)), 0.015)
        self.assertEqual(self.Insight._spent_since(fields.Datetime.now() + timedelta(days=1)), 0.0)


@tagged("post_install", "-at_install")
class TestInsightFromTheEngine(EngineCase):
    """The rows the engine writes: one per query, with what the provider and the search really did."""

    def setUp(self):
        super().setUp()
        self.user = new_user(self.env, "ins")
        self.eng = self.env["buddy.engine"].with_user(self.user)
        for patcher in (patch.object(self.cls, "_authorization_hash", return_value="authorization-A"),):
            patcher.start()
            self.addCleanup(patcher.stop)
        for key in ("insight_questions", "price_in_per_m", "price_out_per_m"):
            self.P.search([("key", "=", "buddy_ia." + key)]).unlink()
        source = new_source(self.env, "Insight motor", [new_group(self.env, "InsM")])
        self.document = new_document(self.env, source, "Guía", "Abrí Ventas.")
        self.chunk = dict(CHUNK, doc_id=self.document.id, title="Guía")
        self.neighbour = dict(CHUNK, id=2, doc_id=self.document.id, sequence=1, score=0.3, neighbor=True)
        self.env["buddy.insight"].sudo().search([]).unlink()

    def rows(self):
        return self.env["buddy.insight"].sudo().search([], order="id")

    def provider(self, output=None, error=None, tokens=(120, 35)):
        test = self

        def fake_chat(provider, model, messages, timeout, usage=None, **kwargs):
            test.sent_usage = usage
            if error:
                raise error
            if usage is not None and tokens:
                usage.update({"in": tokens[0], "out": tokens[1]})
            return json.dumps(dict(VALID if output is None else output))

        patcher = patch.object(self.env.registry["buddy.provider"], "_chat", fake_chat)
        patcher.start()
        self.addCleanup(patcher.stop)

    def chat(self, message="¿Cómo pido vacaciones?", chunks=None, **kwargs):
        chunks = [self.chunk, self.neighbour] if chunks is None else chunks
        with patch.object(self.cls, "_retrieve", return_value=list(chunks)):
            return self.eng.chat(message, **kwargs)

    def test_an_answered_question_leaves_numbers_tokens_documents_and_no_text(self):
        self.provider(dict(VALID, sources_used=[1]))
        self.chat()
        (record,) = self.rows()
        self.assertFalse(record.needs_human)
        self.assertFalse(record.question, "an answered question is not kept by default")
        self.assertEqual((record.retrieved_count, record.best_score), (1, 0.9), "neighbours are not counted as retrieved")
        self.assertEqual((record.tokens_in, record.tokens_out, record.model_name), (120, 35, "qa-chat"))
        self.assertEqual(record.document_ids, self.document)
        self.assertEqual(record.document_titles, "Guía")
        self.assertGreaterEqual(record.latency_ms, 0)
        self.assertFalse(record.error)
        self.assertIsNotNone(self.sent_usage, "the engine hands the provider a place for the token counts")

    def test_a_question_nobody_could_answer_keeps_its_text_and_uses_no_document(self):
        self.provider(dict(VALID, needs_human=True, contact_role="general", ring="doubt",
                           ticket={"title": "T", "description": "d"}))
        self.chat("¿Quién aprueba los viáticos?")
        (record,) = self.rows()
        self.assertTrue(record.needs_human)
        self.assertEqual(record.question, "¿Quién aprueba los viáticos?")
        self.assertEqual(record.retrieved_count, 1, "it did find something, which turned out not to answer it")
        self.assertFalse(record.document_ids)
        self.assertNotIn(str(self.user.id), (record.question or "") + (record.document_titles or ""))

    def test_the_setting_keeps_every_question(self):
        self.P.set_param("buddy_ia.insight_questions", "True")
        self.provider()
        self.chat("¿Cómo pido vacaciones?")
        self.assertEqual(self.rows().question, "¿Cómo pido vacaciones?")

    def test_prices_turn_tokens_into_cost(self):
        self.P.set_param("buddy_ia.price_in_per_m", "1000")
        self.P.set_param("buddy_ia.price_out_per_m", "2000")
        self.provider(tokens=(1000, 500))
        self.chat()
        self.assertAlmostEqual(self.rows().cost, 1.0 + 1.0)

    def test_a_provider_that_reports_nothing_gives_zero_tokens(self):
        self.provider(tokens=None)
        self.chat()
        self.assertEqual((self.rows().tokens_in, self.rows().tokens_out), (0, 0))

    def failure_code(self, **kwargs):
        """The error code of a query that fails. Not assertRaises: Odoo's one rolls the block back, rows included."""
        try:
            self.chat(**kwargs)
        except BuddyBusy as error:
            return error.code
        self.fail("the query was expected to fail")

    def test_a_failure_is_one_row_with_its_error_and_the_user_still_gets_the_error(self):
        self.provider(error=TimeoutError("lento"))
        self.assertEqual(self.failure_code(), "provider_timeout")
        (record,) = self.rows()
        self.assertEqual(record.error, "provider_timeout")
        self.assertFalse(record.needs_human)
        self.assertFalse(record.question, "a failure is not an unanswered question")

    def test_a_retried_request_does_not_count_twice(self):
        self.provider()
        request_id = str(uuid.uuid4())
        self.chat(request_id=request_id)
        self.chat(request_id=request_id)
        self.assertEqual(len(self.rows()), 1, "the second call is answered from the stored response")

    def test_a_session_that_changed_meanwhile_is_not_counted_twice(self):
        self.provider()
        real = self.cls._request_live

        def fence(engine, token, response=None, error=None, insight=None):
            if response is not None:  # the permissions change while the provider was answering
                engine.env["buddy.session"].sudo().search([("user_id", "=", engine.env.uid)]).write({"generation": 99})
            return real(engine, token, response=response, error=error, insight=insight)

        with patch.object(self.cls, "_request_live", fence):
            self.assertEqual(self.failure_code(), "session_changed")
        self.assertEqual(len(self.rows()), 1, "one query, one row, even if the answer had to be discarded")

    def test_a_statistics_failure_never_breaks_the_answer(self):
        self.provider()
        with patch.object(self.env.registry["buddy.insight"], "_record", side_effect=ValueError("boom")):
            result = self.chat()
        self.assertEqual(result["answer"], VALID["answer"])
        self.assertEqual(self.messages_of(self.user), 2)

    def test_streamed_answers_are_counted_with_their_tokens(self):
        def fake_stream(provider, model, messages, timeout, usage=None, **kwargs):
            usage.update({"in": 77, "out": 21})
            yield json.dumps(dict(VALID, sources_used=[1]))

        with patch.object(self.env.registry["buddy.provider"], "_chat_stream", fake_stream), \
                patch.object(self.cls, "_retrieve", return_value=[self.chunk]):
            events = list(self.eng.chat_stream("Pregunta por stream"))
        self.assertEqual(events[-1][0], "done")
        (record,) = self.rows()
        self.assertEqual((record.tokens_in, record.tokens_out, record.document_ids), (77, 21, self.document))

    def test_a_stream_the_person_abandons_is_counted_with_its_reason(self):
        def fake_stream(provider, model, messages, timeout, usage=None, **kwargs):
            usage.update({"in": 40, "out": 2})
            yield '{"answer": "Empiezo'
            yield ' y sigo"}'

        with patch.object(self.env.registry["buddy.provider"], "_chat_stream", fake_stream), \
                patch.object(self.cls, "_retrieve", return_value=[self.chunk]):
            stream = self.eng.chat_stream("Pregunta abandonada")
            next(stream)
            stream.close()
        (record,) = self.rows()
        self.assertEqual((record.error, record.tokens_in), ("client_disconnected", 40))

    def test_a_stream_with_an_invalid_answer_is_counted_as_such(self):
        def fake_stream(provider, model, messages, timeout, usage=None, **kwargs):
            yield "esto no es json"

        with patch.object(self.env.registry["buddy.provider"], "_chat_stream", fake_stream), \
                patch.object(self.cls, "_retrieve", return_value=[self.chunk]):
            events = list(self.eng.chat_stream("Pregunta"))
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(self.rows().error, "invalid_response")
