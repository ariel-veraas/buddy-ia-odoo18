"""buddy.provider: the Anthropic and OpenAI-compatible protocols, failures and limits. `requests.request` is
always mocked: these tests never open a socket."""
import json
from unittest.mock import patch

import requests

from odoo.tests.common import TransactionCase, tagged

from ..models import buddy_provider
from ..models.buddy_provider import MAX_RESPONSE_BYTES, ProviderError, base_url, valid_base_url
from .common import FakeResponse

SECRET = "sk-ant-SECRETA-123"
USER = [{"role": "user", "content": "hola"}]
ANTHROPIC_OK = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Hola "}, {"type": "text", "text": "mundo"}]}
OPENAI_OK = {"choices": [{"finish_reason": "stop", "message": {"content": "Hola mundo"}}]}


@tagged("post_install", "-at_install")
class TestBuddyProvider(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.provider = self.env["buddy.provider"]
        # The retry pause must not make the tests slow.
        sleeper = patch.object(buddy_provider.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.configure("anthropic")

    def configure(self, provider, key=SECRET, base=None):
        self.params.set_param("buddy_ia.provider", provider)
        self.params.set_param("buddy_ia.api_key", key)
        self.params.set_param("buddy_ia.api_base", base or False)

    def mock(self, *responses, error=None):
        """Patch requests.request; each call consumes the next response (the last one repeats)."""
        queue = list(responses)

        def fake(method, url, **kwargs):
            fake.calls.append((method, url, kwargs))
            if error:
                raise error
            return queue.pop(0) if len(queue) > 1 else queue[0]

        fake.calls = []
        patcher = patch.object(buddy_provider.requests, "request", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def chat(self, messages=USER, timeout=10):
        return self.provider._chat("modelo-x", messages, timeout)

    # --- Anthropic ----------------------------------------------------------------------------------
    def test_anthropic_request_shape_and_text_blocks_are_joined(self):
        fake = self.mock(FakeResponse(payload=ANTHROPIC_OK))
        self.assertEqual(self.chat([{"role": "system", "content": "Sé breve."}, {"role": "user", "content": "hola"}]), "Hola mundo")
        method, url, kwargs = fake.calls[0]
        self.assertEqual((method, url), ("POST", "https://api.anthropic.com/v1/messages"))
        self.assertEqual(kwargs["headers"]["x-api-key"], SECRET)
        self.assertEqual(kwargs["headers"]["anthropic-version"], buddy_provider.ANTHROPIC_VERSION)
        self.assertNotIn("Authorization", kwargs["headers"])
        body = kwargs["json"]
        self.assertEqual((body["model"], body["system"], body["messages"]), ("modelo-x", "Sé breve.", [{"role": "user", "content": "hola"}]))
        self.assertLessEqual(body["temperature"], 1.0)
        self.assertGreater(body["max_tokens"], 0)
        self.assertNotIn("response_format", body)

    def test_anthropic_merges_consecutive_messages_of_the_same_role(self):
        fake = self.mock(FakeResponse(payload=ANTHROPIC_OK))
        self.chat([{"role": "system", "content": "A"}, {"role": "system", "content": "B"},
                   {"role": "user", "content": "uno"}, {"role": "user", "content": "dos"},
                   {"role": "assistant", "content": "respuesta"}, {"role": "user", "content": "tres"}])
        body = fake.calls[0][2]["json"]
        self.assertEqual(body["system"], "A\n\nB")
        self.assertEqual(body["messages"], [{"role": "user", "content": "uno\n\ndos"}, {"role": "assistant", "content": "respuesta"},
                                            {"role": "user", "content": "tres"}])

    def test_anthropic_incomplete_or_empty_answers_are_errors(self):
        for payload in ({"stop_reason": "max_tokens", "content": [{"type": "text", "text": "cortado"}]},
                        {"stop_reason": "refusal", "content": []}, {"stop_reason": None, "content": [{"type": "text", "text": "x"}]},
                        {"stop_reason": "end_turn", "content": []}, {"stop_reason": "end_turn", "content": [{"type": "tool_use"}]},
                        ["no", "es", "un", "objeto"]):
            self.mock(FakeResponse(payload=payload))
            with self.subTest(payload=str(payload)[:40]), self.assertRaises(ValueError):
                self.chat()

    def test_anthropic_conversation_must_start_with_the_user(self):
        fake = self.mock(FakeResponse(payload=ANTHROPIC_OK))
        for messages in ([{"role": "assistant", "content": "hola"}], [{"role": "system", "content": "solo sistema"}]):
            with self.assertRaises(ValueError):
                self.chat(messages)
        self.assertEqual(fake.calls, [], "a malformed conversation never leaves the server")

    # --- OpenAI-compatible ------------------------------------------------------------------------------
    def test_openai_request_shape(self):
        self.configure("openai")
        fake = self.mock(FakeResponse(payload=OPENAI_OK))
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        self.assertEqual(self.chat(messages), "Hola mundo")
        method, url, kwargs = fake.calls[0]
        self.assertEqual((method, url), ("POST", "https://api.openai.com/v1/chat/completions"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + SECRET)
        self.assertNotIn("x-api-key", kwargs["headers"])
        self.assertEqual(kwargs["json"]["messages"], messages)
        self.assertEqual(kwargs["json"]["response_format"], {"type": "json_object"})

    def test_gemini_and_custom_use_the_openai_protocol_with_their_own_address(self):
        self.configure("gemini")
        fake = self.mock(FakeResponse(payload=OPENAI_OK))
        self.chat()
        self.assertEqual(fake.calls[0][1], "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
        self.configure("custom", base="http://ollama.local:11434/v1/")
        fake = self.mock(FakeResponse(payload=OPENAI_OK))
        self.chat()
        self.assertEqual(fake.calls[0][1], "http://ollama.local:11434/v1/chat/completions")
        self.assertEqual(fake.calls[0][2]["headers"]["Authorization"], "Bearer " + SECRET)

    def test_openai_incomplete_answers_are_errors(self):
        self.configure("openai")
        for payload in ({"choices": [{"finish_reason": "length", "message": {"content": "cortado"}}]},
                        {"choices": [{"finish_reason": "content_filter", "message": {"content": "x"}}]}, {"choices": []}, {},
                        {"choices": [{"finish_reason": "stop", "message": {"content": None}}]},
                        {"choices": [{"finish_reason": "stop", "message": {"content": ["lista"]}}]}, ["no objeto"]):
            self.mock(FakeResponse(payload=payload))
            with self.subTest(payload=str(payload)[:40]), self.assertRaises(ValueError):
                self.chat()

    def test_length_with_a_complete_json_object_is_accepted(self):
        self.configure("gemini")
        whole = '{"answer": "Hola", "needs_human": false}'
        self.mock(FakeResponse(payload={"choices": [{"finish_reason": "length", "message": {"content": whole}}]}))
        self.assertEqual(self.chat(), whole)

    def test_gemini_gets_a_large_output_cap_and_short_reasoning(self):
        self.configure("gemini")
        fake = self.mock(FakeResponse(payload=OPENAI_OK))
        self.provider._chat("gemini-2.5-flash", USER, 10)
        body = fake.calls[0][2]["json"]
        self.assertEqual(body["max_tokens"], buddy_provider.GEMINI_OUTPUT_TOKENS)
        self.assertEqual(body["reasoning_effort"], "low")
        fake = self.mock(FakeResponse(payload=OPENAI_OK))
        self.provider._chat("gemini-1.5-flash", USER, 10)  # older models do not know the field: it is not sent
        self.assertNotIn("reasoning_effort", fake.calls[0][2]["json"])

    def test_other_providers_keep_the_default_cap_and_no_reasoning_field(self):
        self.configure("openai")
        fake = self.mock(FakeResponse(payload=OPENAI_OK))
        self.provider._chat("gpt-x", USER, 10)
        body = fake.calls[0][2]["json"]
        self.assertEqual(body["max_tokens"], buddy_provider.DEFAULT_OUTPUT_TOKENS)
        self.assertNotIn("reasoning_effort", body)

    # --- HTTP behaviour ---------------------------------------------------------------------------------------
    def test_requests_never_follow_redirects_and_are_bounded(self):
        fake = self.mock(FakeResponse(payload=ANTHROPIC_OK))
        self.chat(timeout=12)
        kwargs = fake.calls[0][2]
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertIs(kwargs["stream"], True)
        connect, read = kwargs["timeout"]
        self.assertEqual((connect, read), (buddy_provider.CONNECT_TIMEOUT, 12))

    def test_short_budget_shortens_the_connect_timeout_too(self):
        fake = self.mock(FakeResponse(payload=ANTHROPIC_OK))
        self.chat(timeout=2)
        self.assertEqual(fake.calls[0][2]["timeout"], (2, 2))

    def test_redirect_response_is_not_followed_and_is_an_error(self):
        fake = self.mock(FakeResponse(status=302, body=b""))
        with self.assertRaises(ValueError):
            self.chat()
        self.assertEqual(len(fake.calls), 1)

    def test_response_is_read_with_a_size_cap_and_always_closed(self):
        response = FakeResponse(payload=ANTHROPIC_OK)
        self.mock(response)
        self.chat()
        self.assertTrue(response.reads and None not in response.reads, "never an unbounded read")
        self.assertLessEqual(max(response.reads), MAX_RESPONSE_BYTES + 1)
        self.assertTrue(response.closed)

    def test_oversized_response_is_rejected(self):
        huge = FakeResponse(body=b'{"pad": "' + b"x" * (MAX_RESPONSE_BYTES + 10) + b'"}')
        self.mock(huge)
        with self.assertRaises(ValueError):
            self.chat()
        self.assertTrue(huge.closed)
        exact = FakeResponse(body=json.dumps(ANTHROPIC_OK).encode() + b" " * 100)
        self.mock(exact)
        self.assertEqual(self.chat(), "Hola mundo", "padding under the cap is fine")

    def test_non_json_response_is_an_error(self):
        for body in (b"<html>proxy</html>", b"", b"\xff\xfe", b"{truncated"):
            self.mock(FakeResponse(body=body))
            with self.subTest(body=body[:10]), self.assertRaises(ValueError):
                self.chat()

    def test_http_errors_have_stable_codes_and_never_include_the_key(self):
        cases = {401: "invalid_key", 403: "invalid_key", 404: "not_found", 429: "rate_limited", 500: "unavailable",
                 502: "unavailable", 400: "unavailable"}
        for status, code in cases.items():
            response = FakeResponse(status=status, payload={"error": {"message": "Bearer " + SECRET + " x-api-key " + SECRET}})
            self.mock(response)
            with self.subTest(status=status), self.assertRaises(ProviderError) as caught:
                self.chat()
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertNotIn("Bearer", str(caught.exception))
            self.assertTrue(response.closed)

    def test_overloaded_provider_is_retried_once(self):
        fake = self.mock(FakeResponse(status=503, payload={}), FakeResponse(payload=ANTHROPIC_OK))
        self.assertEqual(self.chat(), "Hola mundo")
        self.assertEqual(len(fake.calls), 2)
        self.sleep.assert_called_once()

    def test_a_second_overload_is_reported(self):
        fake = self.mock(FakeResponse(status=503, payload={}))
        with self.assertRaises(ProviderError) as caught:
            self.chat()
        self.assertEqual((caught.exception.code, caught.exception.status), ("unavailable", 503))
        self.assertEqual(len(fake.calls), 2)

    def test_other_errors_are_not_retried(self):
        for status in (400, 401, 404, 429, 500):
            fake = self.mock(FakeResponse(status=status, payload={}))
            with self.subTest(status=status), self.assertRaises(ProviderError):
                self.chat()
            self.assertEqual(len(fake.calls), 1)

    def test_no_retry_when_the_time_budget_is_short(self):
        fake = self.mock(FakeResponse(status=503, payload={}), FakeResponse(payload=ANTHROPIC_OK))
        with self.assertRaises(ProviderError):
            self.chat(timeout=4)
        self.assertEqual(len(fake.calls), 1)

    def test_invalid_key_message_is_actionable(self):
        self.mock(FakeResponse(status=401, payload={}))
        with self.assertRaises(ProviderError) as caught:
            self.chat()
        self.assertEqual(caught.exception.code, "invalid_key")
        self.assertIn("clave", str(caught.exception))

    def test_network_failures_become_a_network_error(self):
        for error in (requests.ConnectionError("dns " + SECRET), requests.exceptions.SSLError("tls"), requests.TooManyRedirects("x")):
            self.mock(FakeResponse(payload={}), error=error)
            with self.subTest(error=type(error).__name__), self.assertRaises(ProviderError) as caught:
                self.chat()
            self.assertEqual(caught.exception.code, "network")
            self.assertNotIn(SECRET, str(caught.exception))

    def test_timeouts_raise_timeout_error(self):
        for error in (requests.ReadTimeout("lento"), requests.ConnectTimeout("lento")):
            self.mock(FakeResponse(payload={}), error=error)
            with self.subTest(error=type(error).__name__), self.assertRaises(TimeoutError):
                self.chat()

    def test_an_exhausted_budget_never_calls_the_network(self):
        fake = self.mock(FakeResponse(payload=ANTHROPIC_OK))
        for budget in (0, -3, float("nan"), float("inf"), None, "5"):
            with self.subTest(budget=budget), self.assertRaises(TimeoutError):
                self.chat(timeout=budget)
        self.assertEqual(fake.calls, [])

    def test_missing_key_or_bad_address_never_calls_the_network(self):
        fake = self.mock(FakeResponse(payload=OPENAI_OK))
        self.configure("anthropic", key="")
        with self.assertRaises(ProviderError) as caught:
            self.chat()
        self.assertEqual(caught.exception.code, "not_configured")
        for base in (False, "", "ftp://x/v1", "javascript:alert(1)", "http://user:pw@host/v1", "sin-esquema.com/v1"):
            self.configure("custom", base=base)
            with self.subTest(base=base), self.assertRaises(ProviderError) as caught:
                self.chat()
            self.assertEqual(caught.exception.code, "not_configured")
        self.configure("no-existe")
        with self.assertRaises(ProviderError):
            self.chat()
        self.assertEqual(fake.calls, [])

    def test_the_custom_address_is_ignored_for_the_fixed_providers(self):
        self.configure("openai", base="https://evil.example/v1")
        fake = self.mock(FakeResponse(payload=OPENAI_OK))
        self.chat()
        self.assertTrue(fake.calls[0][1].startswith("https://api.openai.com/"), "the key never goes to a custom host by accident")

    # --- model list ----------------------------------------------------------------------------------------------
    def test_list_models_openai_format_is_sorted_and_cleaned(self):
        self.configure("openai")
        fake = self.mock(FakeResponse(payload={"data": [{"id": "gpt-b"}, {"id": "gpt-a"}, {"id": ""}, {"nombre": "x"}, "basura", {"id": "gpt-a"}]}))
        names = self.provider._list_models()
        self.assertEqual(names, ["gpt-a", "gpt-a", "gpt-b"])
        method, url, kwargs = fake.calls[0]
        self.assertEqual((method, url), ("GET", "https://api.openai.com/v1/models"))
        self.assertIsNone(kwargs.get("json"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + SECRET)

    def test_list_models_gemini_prefix_is_removed(self):
        self.configure("gemini")
        self.mock(FakeResponse(payload={"data": [{"id": "models/gemini-2-pro"}, {"id": "models/gemini-2-flash"}]}))
        self.assertEqual(self.provider._list_models(), ["gemini-2-flash", "gemini-2-pro"])

    def test_list_models_anthropic_format_uses_the_native_headers(self):
        fake = self.mock(FakeResponse(payload={"data": [{"type": "model", "id": "claude-b", "display_name": "B"},
                                                        {"type": "model", "id": "claude-a", "display_name": "A"}], "has_more": False}))
        self.assertEqual(self.provider._list_models(), ["claude-a", "claude-b"])
        self.assertEqual(fake.calls[0][1], "https://api.anthropic.com/v1/models")
        self.assertEqual(fake.calls[0][2]["headers"]["x-api-key"], SECRET)

    def test_list_models_rejects_unexpected_shapes_and_propagates_errors(self):
        for payload in ({}, {"data": "no es lista"}, ["x"], {"models": []}):
            self.mock(FakeResponse(payload=payload))
            with self.subTest(payload=str(payload)), self.assertRaises(ValueError):
                self.provider._list_models()
        self.mock(FakeResponse(status=401, payload={}))
        with self.assertRaises(ProviderError) as caught:
            self.provider._list_models()
        self.assertEqual(caught.exception.code, "invalid_key")

    # --- helpers -------------------------------------------------------------------------------------------------------
    def test_base_url_and_validation_helpers(self):
        self.assertEqual(base_url("anthropic"), "https://api.anthropic.com/v1")
        self.assertEqual(base_url("custom", "  https://x.test/v1/  "), "https://x.test/v1")
        self.assertEqual(base_url("openai", "https://ignorada"), "https://api.openai.com/v1")
        self.assertEqual(base_url("desconocido"), "")
        self.assertTrue(valid_base_url("https://x.test/v1"))
        self.assertTrue(valid_base_url("http://localhost:11434/v1"))
        for bad in ("", None, "x.test", "ftp://x.test", "https://", "https://u:p@x.test", "file:///etc/passwd"):
            self.assertFalse(valid_base_url(bad), bad)
