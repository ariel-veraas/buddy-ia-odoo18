"""Limits that protect the provider bill (per minute, per day, company-wide), the history sent with each question, the
«configured» flag of /buddy/config and the messages the widget shows for each limit."""
import json
import runpy
from unittest.mock import patch

from odoo.modules import get_module_path
from odoo.tests.common import HttpCase, new_test_user, tagged

from ..models.buddy_engine import (DEFAULT_DAILY_TOTAL, DEFAULT_RATE_DAY, HISTORY_CHARS, BuddyBusy)
from .common import new_user
from .test_engine import CHUNK, VALID, EngineCase


@tagged("post_install", "-at_install")
class TestBuddyLimits(EngineCase):
    def setUp(self):
        super().setUp()
        self.user = new_user(self.env, "lim")
        self.eng = self.env["buddy.engine"].with_user(self.user)
        patcher = patch.object(self.cls, "_authorization_hash", return_value="authorization-A")
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("max_daily_total", "max_daily_cost", "price_in_per_m", "price_out_per_m"):
            self.P.search([("key", "=", "buddy_ia." + key)]).unlink()

    def chat(self, engine=None, message="Pregunta"):
        with patch.object(self.cls, "_retrieve", return_value=[CHUNK]), patch.object(
                self.cls, "_ask_model", return_value=dict(VALID)):
            return (engine or self.eng).chat(message)

    def refused(self, engine=None):
        with self.assertRaises(BuddyBusy) as caught:
            self.chat(engine)
        return caught.exception.code

    def reservations(self):
        return self.env["buddy.usage"].sudo().search_count([("scope", "=", "chat")])

    # --- per person: minute vs day ---------------------------------------------------------------------
    def test_the_daily_limit_and_the_minute_limit_are_different_errors(self):
        self.P.set_param("buddy_ia.rate_per_minute", "1")
        self.P.set_param("buddy_ia.rate_per_day", "100")
        self.chat()
        self.assertEqual(self.refused(), "rate_limited", "one a minute: just wait")
        self.P.set_param("buddy_ia.rate_per_minute", "1000")
        self.P.set_param("buddy_ia.rate_per_day", "2")
        self.chat()
        self.assertEqual(self.refused(), "daily_limit", "two a day, both used")

    def test_when_both_are_reached_the_daily_one_is_the_one_reported(self):
        self.P.set_param("buddy_ia.rate_per_minute", "1")
        self.P.set_param("buddy_ia.rate_per_day", "1")
        self.chat()
        self.assertEqual(self.refused(), "daily_limit", "waiting a minute would not help, and the person must be told")

    def test_the_default_per_person_limit_is_40_a_day(self):
        self.assertEqual(DEFAULT_RATE_DAY, 40)
        self.P.search([("key", "=", "buddy_ia.rate_per_day")]).unlink()
        self.assertEqual(self.env["buddy.engine"]._cfg()["rate_day"], 40)
        self.assertEqual(self.env["res.config.settings"].default_get(["buddy_rate_per_day"])["buddy_rate_per_day"], 40)

    def test_the_migration_moves_only_the_old_default(self):
        # The folder name (a version) is not an importable package: the script is run by its path.
        module = runpy.run_path(get_module_path("buddy_ia") + "/migrations/18.0.4.6.0/post-migration.py")
        self.P.set_param("buddy_ia.rate_per_day", "150")
        module["migrate"](self.env.cr, "18.0.4.5.0")
        self.assertEqual(self.P.get_param("buddy_ia.rate_per_day"), "40")
        self.P.set_param("buddy_ia.rate_per_day", "90")
        module["migrate"](self.env.cr, "18.0.4.5.0")
        self.assertEqual(self.P.get_param("buddy_ia.rate_per_day"), "90", "a value somebody chose stays")
        self.P.set_param("buddy_ia.rate_per_day", False)
        module["migrate"](self.env.cr, "18.0.4.5.0")
        self.assertFalse(self.P.get_param("buddy_ia.rate_per_day"))

    # --- the whole company ------------------------------------------------------------------------------
    def test_the_company_wide_daily_limit_stops_everybody_without_spending_their_quota(self):
        self.P.set_param("buddy_ia.max_daily_total", "2")
        first, second, third = (self.env["buddy.engine"].with_user(new_user(self.env, name)) for name in ("a1", "a2", "a3"))
        self.chat(first)
        self.chat(second)
        self.assertEqual(self.reservations(), 2)
        self.assertEqual(self.refused(third), "system_limit")
        self.assertEqual(self.refused(), "system_limit", "also for somebody with plenty of personal quota left")
        self.assertEqual(self.reservations(), 2, "a refused query reserves nothing")

    def test_the_default_company_limit_is_1000_and_it_is_validated(self):
        self.assertEqual(DEFAULT_DAILY_TOTAL, 1000)
        self.assertEqual(self.env["buddy.engine"]._cfg()["max_total"], 1000)
        for bad in ("0", "-1", "1000001", "abc"):
            self.P.set_param("buddy_ia.max_daily_total", bad)
            with self.assertRaises(BuddyBusy) as caught:
                self.env["buddy.engine"]._cfg()
            self.assertEqual(caught.exception.code, "not_configured", bad)

    def test_the_diagnostic_does_not_count_for_the_company_limit(self):
        self.P.set_param("buddy_ia.max_daily_total", "1")
        usage, error = self.eng._reserve_usage({"rate_min": 5, "rate_day": 50, "scope": "diagnostic", "max_total": 1}, 0, "diag-1")
        self.assertIsNone(error)
        self.assertEqual(usage.scope, "diagnostic")
        self.chat()  # the single allowed query still goes through

    def test_the_cost_limit_needs_prices_and_counts_the_estimated_cost_of_the_last_day(self):
        self.P.set_param("buddy_ia.max_daily_cost", "5")
        self.chat()
        self.chat()  # no prices: every cost is 0, the cap cannot trigger
        self.P.set_param("buddy_ia.price_in_per_m", "1000000")
        self.env["buddy.insight"].sudo()._record({"tokens_in": 6, "tokens_out": 0, "needs_human": False})
        self.assertEqual(self.refused(), "system_limit", "6 USD estimated against a cap of 5")
        self.P.set_param("buddy_ia.max_daily_cost", "7")
        self.chat()
        self.P.set_param("buddy_ia.max_daily_cost", "0")
        self.chat()  # 0 = no cost cap

    def test_invalid_cost_cap_is_not_configured(self):
        for bad in ("-1", "1000001", "abc", "nan"):
            self.P.set_param("buddy_ia.max_daily_cost", bad)
            with self.assertRaises(BuddyBusy):
                self.env["buddy.engine"]._cfg()

    # --- history sent to the provider ------------------------------------------------------------------
    def test_the_history_sent_with_a_question_is_capped_at_6000_characters(self):
        self.assertEqual(HISTORY_CHARS, 6000)
        Message = self.env["buddy.message"].sudo()
        for number in range(10):
            Message.create({"user_id": self.user.id, "role": "user" if number % 2 == 0 else "assistant",
                            "content": ("mensaje %d " % number) + "x" * 1900})
        history = self.eng._history({})
        self.assertLessEqual(sum(len(item["content"]) for item in history), HISTORY_CHARS)
        self.assertGreaterEqual(len(history), 2, "the most recent turns are the ones kept")
        self.assertTrue(history[-1]["content"].startswith("mensaje 9"))

    # --- «configured» ----------------------------------------------------------------------------------
    def test_configured_means_a_key_and_a_model_or_a_provider_default(self):
        engine = self.env["buddy.engine"]
        self.assertTrue(engine._configured())
        self.P.set_param("buddy_ia.api_key", False)
        self.assertFalse(engine._configured())
        self.P.set_param("buddy_ia.api_key", "k")
        self.P.set_param("buddy_ia.chat_model", False)
        self.assertFalse(engine._configured(), "the test provider is «custom»: no default model")
        self.P.set_param("buddy_ia.provider", "gemini")
        self.assertTrue(engine._configured(), "Gemini has a default model")
        self.P.set_param("buddy_ia.provider", "openai")
        self.assertFalse(engine._configured())
        self.P.set_param("buddy_ia.chat_model", "gpt-x")
        self.assertTrue(engine._configured())


@tagged("post_install", "-at_install")
class TestBuddyLimitsOverHttp(HttpCase):
    LOGIN = "buddy_limits_http"

    def setUp(self):
        super().setUp()
        new_test_user(self.env, login=self.LOGIN, groups="base.group_user")
        self.authenticate(self.LOGIN, self.LOGIN)
        self.cls = self.env.registry["buddy.engine"]

    def call(self, route, **params):
        payload = json.dumps({"jsonrpc": "2.0", "method": "call", "params": params})
        return self.url_open(route, data=payload, headers={"Content-Type": "application/json"}).json()["result"]

    def test_each_limit_has_its_own_message_for_the_widget(self):
        messages = {}
        for code in ("rate_limited", "daily_limit", "system_limit"):
            with patch.object(self.cls, "chat", side_effect=BuddyBusy(code)):
                result = self.call("/buddy/chat", message="hola")
            self.assertEqual(result["error"], code)
            messages[code] = result["answer"]
        self.assertEqual(len(set(messages.values())), 3, "three different texts")
        self.assertIn("hoy", messages["daily_limit"].lower())
        self.assertIn("unas horas", messages["daily_limit"].lower())
        self.assertNotIn("mañana", messages["daily_limit"].lower() + messages["system_limit"].lower())
        self.assertNotIn("probá de nuevo", messages["daily_limit"].lower(), "no invitation to retry a daily limit")
        self.assertNotIn("Ajustes", messages["system_limit"], "an employee cannot act on the settings")
        self.assertIn("Buddy → Tickets", messages["system_limit"])
        self.assertIn("empresa", messages["system_limit"].lower())
        self.assertIn("muy seguido", messages["rate_limited"])

    def test_config_says_whether_the_buddy_is_configured(self):
        params = self.env["ir.config_parameter"].sudo()
        with patch.object(self.cls, "_session_generation", return_value=2):
            params.set_param("buddy_ia.api_key", "k")
            params.set_param("buddy_ia.provider", "gemini")
            config = self.call("/buddy/config")
            self.assertTrue(config["configured"])
            self.assertIs(config["is_admin"], False, "an employee")
            params.set_param("buddy_ia.api_key", False)
            result = self.call("/buddy/config")
        self.assertIs(result["configured"], False)
        self.assertEqual(result["session_generation"], 2, "the rest of the answer is unchanged")
        self.assertNotIn("api_key", json.dumps(result))
