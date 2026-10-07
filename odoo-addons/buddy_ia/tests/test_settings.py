"""Settings (AI provider, Google Drive key, personalization), first-use checklist, contacts and ticket views,
against a real Odoo registry. Secrets are written once and never come back to the browser."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase, new_test_user, tagged

from ..models.buddy_source import DRIVE_KEY_PARAM
from ..models.drive_client import TOKEN_URI
from ..models.res_config_settings import API_KEY_PARAM, ResConfigSettings
from .common import detach_groups, new_document, new_group, new_source

PARAMS = "ir.config_parameter"
EMAIL = "buddy-bot@proyecto-demo.iam.gserviceaccount.com"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()
KEY_FRAGMENT = "".join(PRIVATE_PEM.splitlines()[1:-1])[:40]


def service_account(**over):
    data = {"type": "service_account", "project_id": "proyecto-demo", "private_key_id": "abc123def456",
            "private_key": PRIVATE_PEM, "client_email": EMAIL, "client_id": "112233445566778899",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": TOKEN_URI,
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/buddy-bot",
            "universe_domain": "googleapis.com"}
    data.update(over)
    return json.dumps({key: value for key, value in data.items() if value is not None})


@tagged("post_install", "-at_install")
class TestBuddySettings(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env[PARAMS].sudo()
        self.Settings = self.env["res.config.settings"]

    def _save(self, **values):
        self.Settings.create(values).execute()

    # --- personalization ------------------------------------------------------------------------
    def test_customization_is_stored_as_config_parameters(self):
        self._save(buddy_greeting="Buenas, {nombre}", buddy_tagline="Tu compañera", buddy_suggestion_1="¿Cómo pido vacaciones?",
                   buddy_suggestion_3="Necesito IT", buddy_primary_color="#112233", buddy_extra_instructions="Tratá a todos de vos.")
        get = self.params.get_param
        self.assertEqual(get("buddy_ia.greeting"), "Buenas, {nombre}")
        self.assertEqual(get("buddy_ia.tagline"), "Tu compañera")
        self.assertEqual(get("buddy_ia.suggestion_1"), "¿Cómo pido vacaciones?")
        self.assertEqual(get("buddy_ia.suggestion_3"), "Necesito IT")
        self.assertEqual(get("buddy_ia.primary_color"), "#112233")
        self.assertEqual(get("buddy_ia.extra_instructions"), "Tratá a todos de vos.")

    def test_hidden_for_everyone_defaults_to_visible_and_round_trips(self):
        self.params.search([("key", "=", "buddy_ia.hidden")]).unlink()
        self.assertFalse(self.Settings.create({}).buddy_hidden, "a fresh install shows the buddy")
        self._save(buddy_hidden=True)
        self.assertEqual(self.params.get_param("buddy_ia.hidden"), "True")
        self.assertTrue(self.Settings.create({}).buddy_hidden)
        self._save(buddy_hidden=False)
        self.assertFalse(self.Settings.create({}).buddy_hidden, "unchecking shows the buddy again")

    # --- character, accessory and idle gestures -------------------------------------------------------
    def test_look_defaults_are_pi_without_accessory_and_gestures_on(self):
        for key in ("character", "accessory", "idle_enabled", "idle_frequency", "idle_groups"):
            self.params.search([("key", "=", "buddy_ia." + key)]).unlink()
        defaults = self.Settings.create({})
        self.assertEqual((defaults.buddy_character, defaults.buddy_accessory, defaults.buddy_idle_frequency),
                         ("mochi", "none", "normal"))
        self.assertTrue(defaults.buddy_idle_enabled)
        self.assertTrue(defaults.buddy_idle_expressions and defaults.buddy_idle_accessories and defaults.buddy_idle_sleep)

    def test_look_round_trips_and_unchecked_boxes_stay_unchecked(self):
        self._save(buddy_character="kitsune", buddy_accessory="shades", buddy_idle_frequency="high", buddy_idle_enabled=False,
                   buddy_idle_expressions=True, buddy_idle_accessories=False, buddy_idle_sleep=True)
        get = self.params.get_param
        self.assertEqual((get("buddy_ia.character"), get("buddy_ia.accessory"), get("buddy_ia.idle_frequency")),
                         ("kitsune", "shades", "high"))
        # Explicit "0"/"1": an unchecked box must not bring the default (checked) back after saving.
        self.assertEqual((get("buddy_ia.idle_enabled"), get("buddy_ia.idle_groups")), ("0", "expressions,sleep"))
        reopened = self.Settings.create({})
        self.assertEqual((reopened.buddy_character, reopened.buddy_accessory, reopened.buddy_idle_frequency),
                         ("kitsune", "shades", "high"))
        self.assertFalse(reopened.buddy_idle_enabled)
        self.assertEqual((reopened.buddy_idle_expressions, reopened.buddy_idle_accessories, reopened.buddy_idle_sleep),
                         (True, False, True))
        self._save(buddy_idle_enabled=True, buddy_idle_expressions=False, buddy_idle_accessories=False, buddy_idle_sleep=False)
        self.assertEqual(self.params.get_param("buddy_ia.idle_groups"), "none")
        self.assertFalse(any((self.Settings.create({}).buddy_idle_expressions, self.Settings.create({}).buddy_idle_sleep)))

    def test_look_values_outside_the_whitelist_are_refused(self):
        for field, value in (("buddy_character", "dragon"), ("buddy_accessory", "<script>"), ("buddy_idle_frequency", "siempre")):
            with self.subTest(field=field):
                # The Selection field refuses it (ValueError) or Buddy's own constraint does (ValidationError).
                try:
                    self.Settings.create({field: value})
                except (ValidationError, ValueError):
                    continue
                self.fail("%s accepted %r" % (field, value))
        # The second wall: an option another module adds to the Selection is still not one of Buddy's ids.
        fake = SimpleNamespace(buddy_character="dragon", buddy_accessory="none", buddy_idle_frequency="normal")
        with self.assertRaises(ValidationError):
            ResConfigSettings._validate_buddy_look([fake])
        ResConfigSettings._validate_buddy_look([SimpleNamespace(buddy_character="mochi", buddy_accessory="party",
                                                                buddy_idle_frequency="low")])

    def test_text_limits_and_color_are_validated(self):
        for field, value in (("buddy_greeting", "x" * 141), ("buddy_tagline", "x" * 61), ("buddy_suggestion_2", "x" * 81),
                             ("buddy_extra_instructions", "x" * 1501), ("buddy_primary_color", "azul"),
                             ("buddy_primary_color", "#12345"), ("buddy_chat_model", "m" * 201)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.Settings.create({field: value})
        self.Settings.create({"buddy_greeting": "x" * 140, "buddy_extra_instructions": "x" * 1500, "buddy_primary_color": "#2F6DF6"})

    def test_numeric_limits_are_validated(self):
        for field, value in (("buddy_top_k", 0), ("buddy_top_k", 11), ("buddy_sync_hours", 0), ("buddy_sync_hours", 169),
                             ("buddy_rate_per_minute", 0), ("buddy_rate_per_day", 0), ("buddy_retention_days", -1)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.Settings.create({field: value})
        self._save(buddy_sync_hours=6)
        self.assertEqual(self.params.get_param("buddy_ia.sync_hours"), "6")

    # --- AI provider and API key ------------------------------------------------------------------
    def test_api_key_is_saved_only_when_not_empty(self):
        self._save(buddy_api_key_input="sk-primera-clave")
        self.assertEqual(self.params.get_param(API_KEY_PARAM), "sk-primera-clave")
        self._save(buddy_api_key_input="")
        self.assertEqual(self.params.get_param(API_KEY_PARAM), "sk-primera-clave", "an empty box keeps the stored key")
        self._save(buddy_chat_model="un-modelo")
        self.assertEqual(self.params.get_param(API_KEY_PARAM), "sk-primera-clave")
        self._save(buddy_api_key_input="   ")
        self.assertEqual(self.params.get_param(API_KEY_PARAM), "sk-primera-clave", "blank spaces are not a key")
        self._save(buddy_api_key_input="sk-segunda-clave")
        self.assertEqual(self.params.get_param(API_KEY_PARAM), "sk-segunda-clave")

    def test_api_key_is_trimmed_but_never_accepts_inner_spaces_or_breaks(self):
        self._save(buddy_api_key_input="  sk-con-bordes \n")
        self.assertEqual(self.params.get_param(API_KEY_PARAM), "sk-con-bordes")
        for bad in ("sk con espacio", "sk-linea1\nsk-linea2", "sk\tcon\ttab", "k" * 501):
            with self.subTest(key=bad[:12]), self.assertRaises(ValidationError):
                self.Settings.create({"buddy_api_key_input": bad})
        self.assertEqual(self.params.get_param(API_KEY_PARAM), "sk-con-bordes")

    def test_api_key_never_comes_back_to_the_form(self):
        self._save(buddy_api_key_input="sk-NO-DEBE-VOLVER")
        settings = self.Settings.create({})
        self.assertTrue(settings.buddy_api_key_loaded)
        self.assertFalse(settings.buddy_api_key_input)
        self.assertNotIn("sk-NO-DEBE-VOLVER", json.dumps(self.Settings.get_values(), default=str))
        self.assertNotIn("sk-NO-DEBE-VOLVER", json.dumps(settings.read(), default=str))
        self.assertNotIn("buddy_api_key_input", self.Settings.default_get(list(self.Settings._fields)))
        for field in self.Settings._fields.values():
            self.assertNotIn(getattr(field, "config_parameter", None), (API_KEY_PARAM, DRIVE_KEY_PARAM), field.name)

    def test_clear_api_key(self):
        self._save(buddy_api_key_input="sk-borrar")
        self.assertTrue(self.Settings.create({}).buddy_api_key_loaded)
        result = self.Settings.create({}).action_buddy_clear_api_key()
        self.assertEqual(result["params"]["next"]["tag"], "soft_reload", "it says what happened, then refreshes the checklist")
        self.assertFalse(self.params.get_param(API_KEY_PARAM))
        self.assertFalse(self.Settings.create({}).buddy_api_key_loaded)

    def test_custom_provider_needs_a_valid_address(self):
        for base in ("", "mi-servidor/v1", "ftp://servidor/v1", "https://user:pw@servidor/v1"):
            with self.subTest(base=base), self.assertRaises(ValidationError):
                self.Settings.create({"buddy_provider": "custom", "buddy_api_base": base})
        self._save(buddy_provider="custom", buddy_api_base="https://mi-servidor.example/v1")
        self.assertEqual(self.params.get_param("buddy_ia.provider"), "custom")
        self.assertEqual(self.params.get_param("buddy_ia.api_base"), "https://mi-servidor.example/v1")
        self._save(buddy_provider="anthropic", buddy_api_base="")
        self.assertEqual(self.params.get_param("buddy_ia.provider"), "anthropic")

    # --- Google Drive key --------------------------------------------------------------------------
    def test_drive_key_is_validated_and_only_the_minimal_json_is_stored(self):
        self._save(buddy_drive_key_input=service_account())
        stored = json.loads(self.params.get_param(DRIVE_KEY_PARAM))
        self.assertEqual(set(stored), {"type", "client_email", "private_key", "token_uri", "project_id"})
        self.assertEqual((stored["client_email"], stored["token_uri"], stored["project_id"]), (EMAIL, TOKEN_URI, "proyecto-demo"))
        self.assertEqual(stored["private_key"], PRIVATE_PEM)
        for dropped in ("private_key_id", "client_id", "auth_uri", "universe_domain", "client_x509_cert_url"):
            self.assertNotIn(dropped, stored)

    def test_drive_key_without_project_id_is_accepted(self):
        self._save(buddy_drive_key_input=service_account(project_id=None))
        self.assertNotIn("project_id", json.loads(self.params.get_param(DRIVE_KEY_PARAM)))

    def test_invalid_drive_keys_are_rejected_without_leaking_the_key(self):
        self._save(buddy_drive_key_input=service_account())
        stored = self.params.get_param(DRIVE_KEY_PARAM)
        bad_keys = {
            "not json": "esto no es json " + KEY_FRAGMENT,
            "list": json.dumps([PRIVATE_PEM]),
            "wrong type": service_account(type="authorized_user"),
            "no email": service_account(client_email=None),
            "bad email": service_account(client_email="sin arroba"),
            "bad token uri": service_account(token_uri="https://evil.example/token"),
            "no private key": service_account(private_key=None),
            "garbage private key": service_account(private_key="-----BEGIN PRIVATE KEY-----\nQUJDREVGRw==\n-----END PRIVATE KEY-----\n"),
        }
        for label, raw in bad_keys.items():
            with self.subTest(case=label), self.assertRaises(ValidationError) as caught:
                self.Settings.create({"buddy_drive_key_input": raw}).execute()
            message = str(caught.exception)
            self.assertNotIn(KEY_FRAGMENT, message, label)
            self.assertNotIn("BEGIN PRIVATE KEY", message, label)
        self.assertEqual(self.params.get_param(DRIVE_KEY_PARAM), stored, "a rejected key leaves the previous one untouched")

    def test_empty_drive_box_keeps_the_stored_key(self):
        self._save(buddy_drive_key_input=service_account())
        stored = self.params.get_param(DRIVE_KEY_PARAM)
        self._save(buddy_drive_key_input="   ")
        self._save(buddy_chat_model="otro")
        self.assertEqual(self.params.get_param(DRIVE_KEY_PARAM), stored)

    def test_drive_email_is_computed_and_the_private_key_never_comes_back(self):
        self.params.set_param(DRIVE_KEY_PARAM, False)
        empty = self.Settings.create({})
        self.assertEqual((empty.buddy_drive_email, empty.buddy_drive_loaded, empty.buddy_ready_drive), ("", False, False))
        self._save(buddy_drive_key_input=service_account())
        settings = self.Settings.create({})
        self.assertEqual(settings.buddy_drive_email, EMAIL)
        self.assertTrue(settings.buddy_drive_loaded)
        self.assertTrue(settings.buddy_ready_drive)
        self.assertFalse(settings.buddy_drive_key_input)
        dumps = (json.dumps(settings.read(), default=str), json.dumps(self.Settings.get_values(), default=str),
                 json.dumps(self.Settings.default_get(list(self.Settings._fields)), default=str))
        for dump in dumps:
            self.assertNotIn(KEY_FRAGMENT, dump)
            self.assertNotIn("PRIVATE KEY", dump)

    def test_corrupt_stored_drive_key_does_not_break_the_screen(self):
        self.params.set_param(DRIVE_KEY_PARAM, "{esto no es json")
        settings = self.Settings.create({})
        self.assertEqual((settings.buddy_drive_email, settings.buddy_drive_loaded), ("", False))

    def test_clear_drive_key(self):
        self._save(buddy_drive_key_input=service_account())
        result = self.Settings.create({}).action_buddy_clear_drive_key()
        self.assertEqual(result["params"]["next"]["tag"], "soft_reload")
        self.assertFalse(self.params.get_param(DRIVE_KEY_PARAM))
        settings = self.Settings.create({})
        self.assertEqual((settings.buddy_drive_email, settings.buddy_ready_drive), ("", False))

    def test_employees_cannot_clear_keys_or_open_the_folder_list(self):
        employee = new_test_user(self.env, login="buddy_settings_emp2", groups="base.group_user")
        self._save(buddy_api_key_input="sk-protegida")
        self._save(buddy_drive_key_input=service_account())
        Settings = self.Settings.with_user(employee).browse()
        for action in (Settings.action_buddy_clear_api_key, Settings.action_buddy_clear_drive_key, Settings.action_buddy_open_sources):
            with self.subTest(action=action.__name__), self.assertRaises(AccessError):
                action()
        self.assertEqual(self.params.get_param(API_KEY_PARAM), "sk-protegida")
        self.assertTrue(self.params.get_param(DRIVE_KEY_PARAM))

    def test_employees_cannot_read_the_stored_secrets(self):
        employee = new_test_user(self.env, login="buddy_settings_emp3", groups="base.group_user")
        self._save(buddy_api_key_input="sk-protegida")
        with self.assertRaises(AccessError):
            self.env[PARAMS].with_user(employee).search([("key", "=", API_KEY_PARAM)]).read(["value"])

    # --- checklist -----------------------------------------------------------------------------------
    def test_checklist_reflects_the_real_state(self):
        self.params.set_param("buddy_ia.last_test", "")
        settings = self.Settings.create({})
        self.assertFalse(settings.buddy_ready_test)
        self.assertTrue("Todavía no" in settings.buddy_last_test_text or "not tested" in settings.buddy_last_test_text)
        self.params.set_param("buddy_ia.last_test", "ok|2026-10-04 12:00:00")
        settings = self.Settings.create({})
        self.assertTrue(settings.buddy_ready_test)
        self.assertIn("2026-10-04 12:00:00", settings.buddy_last_test_text)
        self.params.set_param("buddy_ia.last_test", "fail|2026-10-04 12:00:00")
        self.assertFalse(self.Settings.create({}).buddy_ready_test)

    def test_ai_step_needs_both_key_and_model(self):
        self.params.set_param("buddy_ia.provider", "openai")  # a provider without a default model
        self.params.set_param(API_KEY_PARAM, False)
        self.params.set_param("buddy_ia.chat_model", False)
        self.assertFalse(self.Settings.create({}).buddy_ready_ai)
        self.params.set_param(API_KEY_PARAM, "sk-x")
        self.assertFalse(self.Settings.create({}).buddy_ready_ai, "a key without a model is not enough")
        self.params.set_param("buddy_ia.chat_model", "un-modelo")
        self.assertTrue(self.Settings.create({}).buddy_ready_ai)
        self.params.set_param(API_KEY_PARAM, False)
        self.assertFalse(self.Settings.create({}).buddy_ready_ai, "a model without a key is not enough")

    def test_ai_step_counts_the_providers_default_model(self):
        self.params.set_param("buddy_ia.chat_model", False)
        self.params.set_param("buddy_ia.api_base", "https://mi-servidor.example/v1")  # "custom" needs an address to be saved
        self.params.set_param(API_KEY_PARAM, "sk-x")
        for provider, ready in (("gemini", True), ("anthropic", True), ("openai", False), ("custom", False)):
            self.params.set_param("buddy_ia.provider", provider)
            self.assertEqual(self.Settings.create({}).buddy_ready_ai, ready, provider)
        self.params.set_param("buddy_ia.provider", False)
        self.assertTrue(self.Settings.create({}).buddy_ready_ai, "no provider chosen is Claude, which has a default")
        self.params.set_param(API_KEY_PARAM, False)
        self.assertFalse(self.Settings.create({}).buddy_ready_ai, "the default model does not replace the key")

    def test_the_model_box_says_what_an_empty_value_means_and_recommends_gemini_flash(self):
        hint = {provider: self.Settings.create({"buddy_provider": provider, "buddy_api_base": "https://mi-servidor.example/v1"}).buddy_model_hint
                for provider in ("gemini", "anthropic", "openai", "custom")}
        self.assertIn("gemini-2.5-flash", hint["gemini"])
        self.assertIn("claude-haiku-4-5-20251001", hint["anthropic"])
        self.assertEqual((hint["openai"], hint["custom"]), ("", ""), "no default: the box must be filled in")
        arch = self.Settings.get_views([(False, "form")])["views"]["form"]["arch"]
        self.assertIn("buddy_model_hint", arch)
        self.assertIn("gemini-2.5-flash", arch)
        self.assertIn("lite", arch)
        # Saving with an empty box keeps it empty: the default is applied by the engine, never written over the setting.
        self._save(buddy_provider="gemini", buddy_chat_model="")
        self.assertFalse(self.params.get_param("buddy_ia.chat_model"))
        self._save(buddy_provider="gemini", buddy_chat_model="gemini-2.5-flash-lite")
        self.assertEqual(self.params.get_param("buddy_ia.chat_model"), "gemini-2.5-flash-lite", "what was written stays")

    def test_folder_and_document_steps_follow_the_data(self):
        self.env["buddy.source"].with_context(active_test=False).search([]).unlink()
        settings = self.Settings.create({})
        self.assertEqual((settings.buddy_ready_source, settings.buddy_ready_documents), (False, False))
        source = new_source(self.env, "Checklist", [new_group(self.env, "Chk")])
        settings = self.Settings.create({})
        self.assertEqual((settings.buddy_ready_source, settings.buddy_ready_documents), (True, False), "a folder with no documents yet")
        new_document(self.env, source, "Doc", "contenido de prueba")
        settings = self.Settings.create({})
        self.assertEqual((settings.buddy_ready_source, settings.buddy_ready_documents), (True, True))
        detach_groups(self.env, source)
        settings = self.Settings.create({})
        self.assertEqual((settings.buddy_ready_source, settings.buddy_ready_documents), (False, False), "no group means nobody can use it")
        source.write({"group_ids": [(6, 0, new_group(self.env, "Chk2").ids)], "active": False})
        self.assertFalse(self.Settings.create({}).buddy_ready_source, "an archived folder does not count")

    def test_test_connection_records_result_and_rejects_employees(self):
        cls = self.env.registry["buddy.engine"]
        with patch.object(cls, "_self_test", return_value=(True, ["Proveedor de IA OK"])):
            action = self.Settings.create({}).action_buddy_test_connection()
        self.assertEqual(action["params"]["type"], "success")
        self.assertTrue(self.params.get_param("buddy_ia.last_test").startswith("ok|"))
        with patch.object(cls, "_self_test", return_value=(False, ["Falta la clave de Google Drive."])):
            action = self.Settings.create({}).action_buddy_test_connection()
        self.assertEqual(action["params"]["type"], "danger")
        self.assertIn("Google Drive", action["params"]["message"])
        self.assertTrue(self.params.get_param("buddy_ia.last_test").startswith("fail|"))
        employee = new_test_user(self.env, login="buddy_settings_emp", groups="base.group_user")
        with self.assertRaises(AccessError):
            self.Settings.with_user(employee).browse().action_buddy_test_connection()

    def test_checklist_button_opens_the_folder_list(self):
        action = self.Settings.create({}).action_buddy_open_sources()
        self.assertEqual(action["res_model"], "buddy.source")

    def test_settings_view_has_checklist_secret_boxes_and_advanced_block(self):
        arch = self.Settings.get_views([(False, "form")])["views"]["form"]["arch"]
        for expected in ("buddy_start", "buddy_advanced", "buddy_show_advanced", "buddy_ready_ai", "buddy_ready_drive",
                         "buddy_ready_source", "buddy_ready_documents", "buddy_ready_test", "action_buddy_test_connection",
                         "buddy_provider", "buddy_api_key_input", "buddy_chat_model", "buddy_drive_key_input", "buddy_drive_email",
                         "action_buddy_clear_api_key", "action_buddy_clear_drive_key", "action_buddy_open_sources",
                         "buddy_sync_hours", "buddy_greeting", "buddy_primary_color", "buddy_extra_instructions", "buddy_hidden"):
            self.assertIn(expected, arch)
        self.assertIn("btn-primary", arch)
        self.assertNotIn("llm", arch.lower().replace("buddy", ""), "no leftover of the old LLM modules")

    def test_only_administrators_get_the_folder_menu(self):
        menu = self.env.ref("buddy_ia.buddy_menu_sources")
        self.assertEqual(menu.groups_id, self.env.ref("base.group_system"))

    # --- tickets and contacts ----------------------------------------------------------------------------
    def test_ticket_search_view_and_help(self):
        views = self.env["buddy.ticket"].get_views([(False, "search"), (False, "list")])["views"]
        for name in ("my_tickets", "open_tickets", "group_state"):
            self.assertIn(name, views["search"]["arch"])
        action = self.env.ref("buddy_ia.buddy_ticket_action")
        self.assertIn("Todavía no hay tickets", action.help)
        self.assertEqual(action.search_view_id, self.env.ref("buddy_ia.buddy_ticket_view_search"))

    def test_my_tickets_filter_only_matches_own(self):
        ana = new_test_user(self.env, login="buddy_flt_ana", groups="base.group_user")
        leo = new_test_user(self.env, login="buddy_flt_leo", groups="base.group_user")
        Ticket = self.env["buddy.ticket"]
        mine = Ticket.with_user(ana).create({"name": "De Ana"})
        Ticket.with_user(leo).create({"name": "De Leo"})
        self.assertEqual(Ticket.search([("user_id", "=", ana.id)]), mine)

    def test_contact_code_is_normalized_unique_and_validated(self):
        Contact = self.env["buddy.contact"]
        contact = Contact.create({"code": "  Soporte ", "label": "Soporte", "name": "Equipo", "email": "s@ejemplo.test"})
        self.assertEqual(contact.code, "soporte")
        contact.write({"code": " SOPORTE-2 "})
        self.assertEqual(contact.code, "soporte-2")
        with self.assertRaises(ValidationError):
            Contact.create({"code": "SOPORTE-2", "label": "Dup", "name": "Dup", "email": "d@ejemplo.test"})
        for bad in ("con espacio", "ñandú", "a;b", "x" * 33):
            with self.subTest(code=bad), self.assertRaises(ValidationError):
                Contact.create({"code": bad, "label": "Mal", "name": "Mal", "email": "m@ejemplo.test"})
        help_text = self.env.ref("buddy_ia.buddy_contact_action").help.lower()
        self.assertTrue("código" in help_text or "<strong>code</strong>" in help_text, "the action explains the 'code' field")

    def test_engine_finds_a_contact_saved_with_capitals(self):
        self.env["buddy.contact"].create({"code": "Finanzas", "label": "Finanzas", "name": "Equipo", "email": "f@ejemplo.test"})
        self.assertEqual(self.env["buddy.engine"]._contact_for("FINANZAS")["role"], "finanzas")


@tagged("post_install", "-at_install")
class TestBuddyConfigRoute(HttpCase):
    def _config(self, login):
        self.authenticate(login, login)
        payload = json.dumps({"jsonrpc": "2.0", "method": "call", "params": {}})
        response = self.url_open("/buddy/config", data=payload, headers={"Content-Type": "application/json"})
        return response.json()["result"]

    def test_config_route_returns_appearance_and_never_secrets(self):
        new_test_user(self.env, login="buddy_cfg_route", groups="base.group_user")
        params = self.env[PARAMS].sudo()
        for key, value in {"greeting": "Buenas, {nombre}", "tagline": "Tu compañera", "suggestion_1": "¿Dónde está el wifi?",
                           "primary_color": "#112233", "extra_instructions": "SECRETO-DE-LA-EMPRESA",
                           "api_key": "sk-SECRETA", "drive_key": service_account()}.items():
            params.set_param("buddy_ia." + key, value)
        params.search([("key", "=", "buddy_ia.hidden")]).unlink()
        cls = self.env.registry["buddy.engine"]
        with patch.object(cls, "_session_generation", return_value=5):
            result = self._config("buddy_cfg_route")
        self.assertEqual((result["greeting"], result["tagline"], result["primary_color"]),
                         ("Buenas, {nombre}", "Tu compañera", "#112233"))
        self.assertEqual(result["suggestions"], ["¿Dónde está el wifi?"])
        self.assertTrue(result["enabled"])
        self.assertEqual(result["session_generation"], 5)
        dump = json.dumps(result)
        for secret in ("SECRETO-DE-LA-EMPRESA", "sk-SECRETA", "PRIVATE KEY", EMAIL):
            self.assertNotIn(secret, dump)
        params.set_param("buddy_ia.hidden", "True")
        with patch.object(cls, "_session_generation", return_value=5):
            self.assertFalse(self._config("buddy_cfg_route")["enabled"])

    def test_config_route_sends_only_whitelisted_look_values(self):
        new_test_user(self.env, login="buddy_cfg_look", groups="base.group_user")
        params = self.env[PARAMS].sudo()
        cls = self.env.registry["buddy.engine"]
        for key in ("character", "accessory", "idle_enabled", "idle_frequency", "idle_groups"):
            params.search([("key", "=", "buddy_ia." + key)]).unlink()
        with patch.object(cls, "_session_generation", return_value=5):
            fresh = self._config("buddy_cfg_look")
        self.assertEqual((fresh["character"], fresh["accessory"]), ("mochi", "none"))
        self.assertEqual(fresh["idle"], {"enabled": True, "frequency": "normal", "groups": ["expressions", "accessories", "sleep"]})
        for key, value in {"character": "mochi", "accessory": "party", "idle_frequency": "low", "idle_groups": "sleep",
                           "idle_enabled": "0"}.items():
            params.set_param("buddy_ia." + key, value)
        with patch.object(cls, "_session_generation", return_value=5):
            chosen = self._config("buddy_cfg_look")
        self.assertEqual((chosen["character"], chosen["accessory"]), ("mochi", "party"))
        self.assertEqual(chosen["idle"], {"enabled": False, "frequency": "low", "groups": ["sleep"]})
        # Whatever is stored by hand (another module, a script) is never forwarded: it falls back to the defaults.
        for key, value in {"character": "<img src=x>", "accessory": "x; color:red", "idle_frequency": "<b>",
                           "idle_groups": "<i>,sleep"}.items():
            params.set_param("buddy_ia." + key, value)
        with patch.object(cls, "_session_generation", return_value=5):
            hostile = self._config("buddy_cfg_look")
        self.assertEqual((hostile["character"], hostile["accessory"], hostile["idle"]["frequency"]), ("mochi", "none", "normal"))
        self.assertEqual(hostile["idle"]["groups"], ["sleep"])
        self.assertNotIn("<", json.dumps(hostile))
