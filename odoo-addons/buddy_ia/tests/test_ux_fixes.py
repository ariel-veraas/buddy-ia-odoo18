"""Fixes of the final UX review (18.0.4.7.0): sample contacts never offer a mail link, the Configuración menu, the
guide on the screen, failures in the checklist, dialect, remembered «advanced» block, readable ticket refusals."""
import json
from unittest.mock import patch

from odoo.tests.common import HttpCase, TransactionCase, new_test_user, tagged

from ..models import prompts
from .common import local_test_transaction, new_user
from .test_engine import EngineCase

PARAMS = "ir.config_parameter"


@tagged("post_install", "-at_install")
class TestContactsAndGuides(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Contact = self.env["buddy.contact"]
        self.Settings = self.env["res.config.settings"]

    def test_a_sample_address_gives_a_card_without_a_mail_link(self):
        self.Contact.search([]).unlink()
        self.Contact.create({"code": "general", "label": "Mesa de ayuda", "name": "Equipo de ayuda", "email": "ayuda@ejemplo.test"})
        self.Contact.create({"code": "it", "label": "Mesa de IT", "name": "Equipo de IT", "email": "it@miempresa.com"})
        engine = self.env["buddy.engine"]
        sample = engine._contact_for("general")
        self.assertEqual((sample["label"], sample["name"], sample["contact"]), ("Mesa de ayuda", "Equipo de ayuda", ""))
        self.assertEqual(engine._contact_for("it")["contact"], "it@miempresa.com")
        self.assertEqual(engine._contact_for("rrhh")["role"], "general", "falls back to «general», still without the sample address")
        self.assertEqual(engine._contact_for("rrhh")["contact"], "")

    def test_sample_rows_are_marked_in_the_list(self):
        self.Contact.search([]).unlink()
        sample = self.Contact.create({"code": "general", "label": "A", "name": "A", "email": "a@ejemplo.test"})
        real = self.Contact.create({"code": "it", "label": "B", "name": "B", "email": "b@miempresa.com"})
        self.assertEqual((sample.is_placeholder, real.is_placeholder), (True, False))
        real.email = "b@example.com"
        self.assertTrue(real.is_placeholder)
        arch = self.Contact.get_views([(False, "list")])["views"]["list"]["arch"]
        self.assertIn("decoration-warning", arch)
        self.assertIn("is_placeholder", arch)

    def test_the_api_key_box_does_not_ask_the_browser_to_fill_a_saved_password(self):
        arch = self.Settings.get_views([(False, "form")])["views"]["form"]["arch"]
        self.assertIn('autocomplete="new-password"', arch)
        self.assertNotIn('buddy_api_key_input" password="True" placeholder="Pegá acá la clave de API" autocomplete="off"', arch)

    def test_there_is_a_configuration_entry_inside_the_buddy_menu_for_administrators(self):
        menu = self.env.ref("buddy_ia.buddy_menu_settings")
        self.assertEqual(menu.parent_id, self.env.ref("buddy_ia.buddy_menu_root"))
        self.assertEqual(menu.groups_id, self.env.ref("base.group_system"))
        action = self.env.ref("buddy_ia.buddy_settings_action")
        self.assertEqual((action.res_model, action.target), ("res.config.settings", "inline"))
        self.assertIn("buddy_ia", action.context)

    def test_no_guide_sends_the_person_to_a_file_of_the_repository(self):
        settings_arch = self.Settings.get_views([(False, "form")])["views"]["form"]["arch"]
        # only Buddy's own block (the arch holds the settings of every installed app)
        settings_arch = settings_arch[settings_arch.index('name="buddy_ia"'):]
        settings_arch = settings_arch[:settings_arch.index("</app>")]
        for model, kind in (("buddy.source", "form"), ("buddy.contact", "list")):
            arch = self.env[model].get_views([(False, kind)])["views"][kind]["arch"]
            self.assertNotIn("docs/", arch, model)
            self.assertNotIn(".md", arch, model)
        self.assertNotIn("docs/", settings_arch)
        self.assertNotIn(".md", settings_arch)
        # the guide is open and comes BEFORE the box where the key is loaded
        self.assertLess(settings_arch.index("Cómo conseguir la clave de Google"), settings_arch.index("buddy_drive_key_file"))
        guide = settings_arch[settings_arch.index("Cómo conseguir la clave de Google"):settings_arch.index("buddy_drive_key_file")]
        self.assertNotIn("<details", guide)
        for step in ("console.cloud.google.com", "Google Drive API", "Cuentas de servicio", "JSON", "Lector", "Service account key creation is disabled"):
            self.assertIn(step, guide)

    def test_the_wording_is_the_same_everywhere(self):
        arch = self.Settings.get_views([(False, "form")])["views"]["form"]["arch"]
        arch = arch[arch.index('name="buddy_ia"'):]
        self.assertNotIn("Pasajes", arch)
        self.assertNotIn("(avanzado: 40 por día)", arch, "no number written in a help that the setting can change")
        self.assertEqual(self.Settings._fields["buddy_rate_per_minute"].string, "Consultas por minuto y por persona")
        self.assertEqual(self.Settings._fields["buddy_rate_per_day"].string, "Consultas por día y por persona")


@tagged("post_install", "-at_install")
class TestSettingsExtras(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env[PARAMS].sudo()
        self.Settings = self.env["res.config.settings"]

    def test_the_advanced_block_stays_open_after_saving(self):
        self.params.search([("key", "=", "buddy_ia.show_advanced")]).unlink()
        self.assertFalse(self.Settings.create({}).buddy_show_advanced)
        self.Settings.create({"buddy_show_advanced": True}).execute()
        self.assertTrue(self.Settings.create({}).buddy_show_advanced)
        self.Settings.create({"buddy_show_advanced": False}).execute()
        self.assertFalse(self.Settings.create({}).buddy_show_advanced)

    def test_failures_of_the_last_24_hours_show_up_with_their_last_reason(self):
        Insight = self.env["buddy.insight"].sudo()
        Insight.search([]).unlink()
        self.assertFalse(self.Settings.create({}).buddy_failures_text)
        Insight._record({"needs_human": False, "error": "provider_timeout"})
        Insight._record({"needs_human": False, "error": "provider_error"})
        Insight._record({"needs_human": True, "question": "una sin respuesta no es un fallo"})
        text = self.Settings.create({}).buddy_failures_text
        self.assertIn("2 consultas fallaron en las últimas 24 horas", text)
        self.assertIn("El proveedor de IA falló", text, "the most recent one, in words")
        self.assertNotIn("provider_error", text)
        arch = self.Settings.get_views([(False, "form")])["views"]["form"]["arch"]
        self.assertGreaterEqual(arch.count("buddy_failures_text"), 2, "in the checklist and in the usage block")

    def test_old_failures_do_not_count(self):
        Insight = self.env["buddy.insight"].sudo()
        Insight.search([]).unlink()
        record = Insight._record({"needs_human": False, "error": "provider_error"})
        self.env.cr.execute("UPDATE buddy_insight SET create_date = now() - interval '2 days' WHERE id = %s", (record.id,))
        record.invalidate_recordset()
        self.assertFalse(self.Settings.create({}).buddy_failures_text)

    def test_the_dialect_setting_defaults_to_rioplatense_and_is_stored(self):
        self.params.search([("key", "=", "buddy_ia.dialect")]).unlink()
        self.assertEqual(self.Settings.default_get(["buddy_dialect"])["buddy_dialect"], "rioplatense")
        self.Settings.create({"buddy_dialect": "neutral"}).execute()
        self.assertEqual(self.params.get_param("buddy_ia.dialect"), "neutral")


@tagged("post_install", "-at_install")
class TestDialectInThePrompt(EngineCase):
    def setUp(self):
        super().setUp()
        self.user = new_user(self.env, "dialect")
        self.eng = self.env["buddy.engine"].with_user(self.user)

    def system_prompt(self):
        cfg = self.env["buddy.engine"]._cfg()
        return self.env["buddy.engine"]._build_messages("hola", [], {"name": "x", "is_new": False}, [], cfg)[0]["content"]

    def test_the_default_is_voseo_and_neutral_changes_the_style_line_only(self):
        self.P.search([("key", "=", "buddy_ia.dialect")]).unlink()
        self.assertIn("español rioplatense (voseo)", self.system_prompt())
        self.P.set_param("buddy_ia.dialect", "neutral")
        neutral = self.system_prompt()
        self.assertIn("español neutro (tuteo, sin regionalismos)", neutral)
        self.assertNotIn("rioplatense (voseo)", neutral)
        self.assertIn("Reglas:", neutral)

    def test_an_unknown_value_falls_back_to_the_default(self):
        self.P.set_param("buddy_ia.dialect", "klingon")
        self.assertIn("rioplatense", self.system_prompt())
        self.assertEqual(self.env["buddy.engine"]._cfg()["dialect"], prompts.DEFAULT_DIALECT)


@tagged("post_install", "-at_install")
class TestTicketRefusalOverHttp(HttpCase):
    LOGIN = "buddy_ticket_cap"

    def test_a_refused_ticket_says_why(self):
        new_test_user(self.env, login=self.LOGIN, groups="base.group_user")
        self.env["ir.config_parameter"].sudo().set_param("buddy_ia.tickets_per_day", "1")
        self.authenticate(self.LOGIN, self.LOGIN)

        def call(**params):
            payload = json.dumps({"jsonrpc": "2.0", "method": "call", "params": params})
            return self.url_open("/buddy/ticket", data=payload, headers={"Content-Type": "application/json"}).json()["result"]

        engine = self.env.registry["buddy.engine"]
        with patch.object(engine, "_fresh_engine", local_test_transaction), patch.object(
                engine, "_authorization_hash", return_value="authorization-A"):
            first = call(title="Uno", description="d")
            second = call(title="Dos", description="d")
        self.assertTrue(first.get("id"), first)
        self.assertEqual(second["error"], "ticket_rejected")
        self.assertIn("máximo de tickets por día", second["message"])


@tagged("post_install", "-at_install")
class TestProviderAuthAndCosmetics(EngineCase):
    """A refused key is its own error (not «unavailable, try again»), and the cosmetic fixes of the final QA."""

    def setUp(self):
        super().setUp()
        self.user = new_user(self.env, "auth")
        self.eng = self.env["buddy.engine"].with_user(self.user)
        for patcher in (patch.object(self.cls, "_authorization_hash", return_value="authorization-A"),
                        patch.object(self.cls, "_retrieve", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.env["buddy.insight"].sudo().search([]).unlink()

    def failing_chat(self, error, **kwargs):
        """The error code of a query that fails (not assertRaises: Odoo's rolls the rows back)."""
        from ..models.buddy_engine import BuddyBusy
        with patch.object(self.env.registry["buddy.provider"], "_chat", side_effect=error):
            try:
                self.eng.chat("¿Cómo pido una cosa?", **kwargs)
            except BuddyBusy as busy:
                return busy.code
        self.fail("the query was expected to fail")

    def test_a_refused_key_is_provider_auth_in_the_answer_and_in_the_statistics(self):
        from ..models.buddy_provider import ProviderError
        self.assertEqual(self.failing_chat(ProviderError("invalid_key", "clave")), "provider_auth")
        (row,) = self.env["buddy.insight"].sudo().search([])
        self.assertEqual((row.error, row.outcome), ("provider_auth", "failed"))
        labels = dict(self.env["buddy.insight"]._fields["error"]._description_selection(self.env))
        self.assertEqual(labels["provider_auth"], "Clave o permisos del proveedor")

    def test_other_provider_failures_stay_provider_error(self):
        from ..models.buddy_provider import ProviderError
        self.assertEqual(self.failing_chat(ProviderError("unavailable", "caído", status=500)), "provider_error")
        self.assertEqual(self.failing_chat(ProviderError("rate_limited", "cuota")), "provider_error")

    def test_the_administrator_sees_it_in_the_checklist_in_words(self):
        from ..models.buddy_provider import ProviderError
        self.failing_chat(ProviderError("invalid_key", "clave"))
        text = self.env["res.config.settings"].create({}).buddy_failures_text
        self.assertIn("1 consulta falló", text)
        self.assertIn("Clave o permisos del proveedor", text)

    def test_the_failure_count_is_singular_or_plural(self):
        Insight = self.env["buddy.insight"].sudo()
        Insight._record({"error": "provider_error"})
        self.assertTrue(self.env["res.config.settings"].create({}).buddy_failures_text.startswith("1 consulta falló en las últimas 24 horas"))
        Insight._record({"error": "provider_timeout"})
        self.assertTrue(self.env["res.config.settings"].create({}).buddy_failures_text.startswith("2 consultas fallaron en las últimas 24 horas"))

    def test_the_outcome_is_readable_and_the_charts_group_by_it(self):
        Insight = self.env["buddy.insight"].sudo()
        answered, unanswered, failed = (Insight._record({"needs_human": False}), Insight._record({"needs_human": True}),
                                        Insight._record({"needs_human": True, "error": "provider_error"}))
        self.assertEqual((answered.outcome, unanswered.outcome, failed.outcome), ("answered", "unanswered", "failed"))
        labels = dict(Insight._fields["outcome"]._description_selection(self.env))
        self.assertEqual(set(labels.values()), {"Respondida", "Sin respuesta", "Con error"})
        views = Insight.get_views([(False, "graph"), (False, "pivot")])["views"]
        for kind in ("graph", "pivot"):
            self.assertIn('name="outcome"', views[kind]["arch"], kind)
            self.assertNotIn('name="needs_human"', views[kind]["arch"], "a boolean would be drawn as true / false")

    def test_clearing_a_key_says_what_happened_and_refreshes_the_screen(self):
        Settings = self.env["res.config.settings"]
        for method, expected in (("action_buddy_clear_api_key", "Clave de API quitada"), ("action_buddy_clear_drive_key", "Clave de Drive quitada")):
            result = getattr(Settings.create({}), method)()
            self.assertEqual(result["tag"], "display_notification")
            self.assertEqual(result["params"]["title"], expected)
            self.assertEqual(result["params"]["next"]["tag"], "soft_reload")
            self.assertNotIn("probar la conexión", result["params"]["message"].lower())

    def test_the_folder_views_say_who_sees_what_so_odoo_has_nothing_to_warn_about(self):
        form = self.env.ref("buddy_ia.buddy_source_view_form").arch_db
        listing = self.env.ref("buddy_ia.buddy_source_view_list").arch_db
        self.assertNotIn("decoration-danger=\"state", listing.split("<field")[0], "the row itself no longer depends on a field only administrators read")
        # every element that reads the state, the error or the e-mail of the service account says who may see it
        self.assertGreaterEqual(form.count('groups="base.group_system"'), 7)
