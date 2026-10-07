"""E2, the search by meaning: embeddings from the provider, vectors in plain PostgreSQL, hybrid retrieval, incremental
indexing, permissions and privacy. The provider is always replaced (no socket); the vectors of the fake come from a small
table of «concepts», so two texts that use different words for the same idea get close vectors, like a real model's."""
import re
import time
import zlib
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from ..models import buddy_embedding, buddy_provider, buddy_vectors
from ..models.buddy_embedding import DIMS_PARAM, MODEL_PARAM, SEMANTIC_PARAM, STATE_PARAM
from ..models.buddy_provider import ProviderError
from ..models.buddy_text import fold
from .common import FakeClient, FakeResponse, drive_file, new_admin, new_document, new_group, new_source, new_user
from .test_engine import VALID, EngineCase

DIMS = 24
CONCEPTS = [
    {"ticket", "pedido", "solicitud", "incidente", "caso"},
    {"urgente", "grave", "critico", "critica", "importante"},
    {"tarda", "demora", "demoran", "tiempo", "resolucion", "arreglar", "resolver", "solucionar", "plazo"},
    {"horas", "hora", "ocho"},
    {"backup", "backups", "respaldo", "respaldos", "copia", "copias"},
    {"vacaciones", "licencia", "descanso"},
    {"salario", "sueldo", "escala", "salarial", "haberes"},
    {"servidor", "servidores", "maquinas", "virtuales"},
]
URGENT_DOC = "Tiempo de resolución urgente"
URGENT_TEXT = "El tiempo de resolución de un incidente urgente es de 8 horas."
URGENT_QUESTION = "¿Cuánto demoran en arreglar algo grave?"  # not one word of it is in the document


def fake_vector(text, dims=DIMS):
    values = [0.01] * dims
    words = re.findall(r"\w+", fold(text.lower()))
    for word in words:
        for index, group in enumerate(CONCEPTS):
            if word in group:
                values[index] += 1.0
        values[len(CONCEPTS) + zlib.crc32(word.encode()) % (dims - len(CONCEPTS))] += 0.05
    return values


def fake_embed(texts, model, timeout, dims=0, settings=None):
    return [buddy_vectors.normalize(fake_vector(text, dims or DIMS)) for text in texts]


class SemanticCase(EngineCase):
    """The configured custom provider of EngineCase, the search by meaning on, and a fake embeddings endpoint."""

    def setUp(self):
        super().setUp()
        self.P.set_param(SEMANTIC_PARAM, "True")
        self.P.set_param(MODEL_PARAM, "qa-embed")
        self.P.set_param(DIMS_PARAM, str(DIMS))
        self.env["buddy.source"].with_context(active_test=False).search([]).unlink()
        buddy_embedding._QUERIES.clear()
        buddy_embedding._REJECTED.clear()
        buddy_embedding._TOO_BIG_LOGGED.clear()
        for key in list(buddy_embedding._BREAKER._state):
            buddy_embedding._BREAKER.success(key)
        patcher = patch.object(self.env.registry["buddy.provider"], "_embed", MagicMock(side_effect=fake_embed))
        self.embed = patcher.start()
        self.addCleanup(patcher.stop)
        self.Embedding = self.env["buddy.embedding"]
        self.Chunk = self.env["buddy.chunk"]

    def cfg(self):
        return self.Embedding._semantic_cfg()

    def index(self, seconds=60, **kwargs):
        return self.Embedding._index_pending(time.monotonic() + seconds, **kwargs)

    def rows(self):
        self.env.cr.execute("SELECT chunk_id, embedding_model, dims, content_hash FROM buddy_embedding ORDER BY chunk_id")
        return self.env.cr.fetchall()

    def retrieve(self, user, message, context=None, **cfg):
        values = {"top_k": 4, "semantic": self.cfg(), "semantic_info": {}}
        values.update(cfg)
        self.last_cfg = values
        return self.env["buddy.engine"].with_user(user)._retrieve(message, values, context)

    @staticmethod
    def titles(hits):
        return [hit["title"] for hit in hits if not hit["neighbor"]]


# ----------------------------------------------------------------------------------------------------------- provider
@tagged("post_install", "-at_install")
class TestEmbeddingsProvider(TransactionCase):
    def setUp(self):
        super().setUp()
        self.params = self.env["ir.config_parameter"].sudo()
        self.provider = self.env["buddy.provider"]
        sleeper = patch.object(buddy_provider.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.configure("openai")

    def configure(self, provider, key="sk-SECRETA-EMBED", base=None):
        self.params.set_param("buddy_ia.provider", provider)
        self.params.set_param("buddy_ia.api_key", key)
        self.params.set_param("buddy_ia.api_base", base or False)

    def mock(self, *responses):
        queue = list(responses)

        def fake(method, url, **kwargs):
            fake.calls.append((method, url, kwargs))
            return queue.pop(0) if len(queue) > 1 else queue[0]

        fake.calls = []
        patcher = patch.object(buddy_provider._SESSION, "request", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    @staticmethod
    def answer(count=2, dims=12):
        return {"object": "list", "data": [{"object": "embedding", "index": number, "embedding": [float(number + 1)] + [0.5] * (dims - 1)}
                                           for number in range(count)], "model": "m"}

    def test_the_request_has_the_openai_shape(self):
        fake = self.mock(FakeResponse(payload=self.answer(2)))
        vectors = self.provider._embed(["uno", "dos"], "text-embedding-3-small", 10, dims=512)
        method, url, kwargs = fake.calls[0]
        self.assertEqual((method, url), ("POST", "https://api.openai.com/v1/embeddings"))
        self.assertEqual(kwargs["json"], {"model": "text-embedding-3-small", "input": ["uno", "dos"], "dimensions": 512})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-SECRETA-EMBED")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(len(vectors), 2)
        for vector in vectors:
            self.assertAlmostEqual(buddy_vectors.dot(vector, vector), 1.0, places=5)

    def test_dimensions_are_only_sent_when_asked_for(self):
        fake = self.mock(FakeResponse(payload=self.answer(1)))
        self.provider._embed(["uno"], "modelo", 10)
        self.assertNotIn("dimensions", fake.calls[0][2]["json"])

    def test_gemini_goes_through_its_openai_compatible_endpoint(self):
        self.configure("gemini")
        fake = self.mock(FakeResponse(payload=self.answer(1)))
        self.provider._embed(["uno"], "gemini-embedding-2", 10, dims=768)
        self.assertEqual(fake.calls[0][1], "https://generativelanguage.googleapis.com/v1beta/openai/embeddings")

    def test_claude_has_no_embeddings_and_nothing_is_sent(self):
        self.configure("anthropic")
        fake = self.mock(FakeResponse(payload=self.answer(1)))
        with self.assertRaises(ProviderError) as caught:
            self.provider._embed(["uno"], "modelo", 10)
        self.assertEqual(caught.exception.code, "embeddings_unsupported")
        self.assertEqual(fake.calls, [])

    def test_failures_have_stable_codes_and_never_the_key(self):
        for status, code in ((401, "invalid_key"), (403, "invalid_key"), (404, "not_found"), (429, "rate_limited"), (500, "unavailable")):
            with self.subTest(status=status):
                self.mock(FakeResponse(status=status, payload={"error": {"message": "sk-SECRETA-EMBED en el cuerpo"}}))
                with self.assertRaises(ProviderError) as caught:
                    self.provider._embed(["uno"], "modelo", 10)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("sk-SECRETA", str(caught.exception))

    def test_an_answer_that_is_not_a_list_of_vectors_is_refused(self):
        for payload in ({"data": []}, {"data": [{"embedding": [1.0] * 12}]}, {"data": [{"embedding": "x"}, {"embedding": "y"}]},
                        {"error": "algo"}, []):
            with self.subTest(payload=str(payload)[:40]):
                self.mock(FakeResponse(payload=payload))
                with self.assertRaises(ValueError):
                    self.provider._embed(["uno", "dos"], "modelo", 10)
        self.mock(FakeResponse(body=b"<html>no es json</html>"))
        with self.assertRaises(ValueError):
            self.provider._embed(["uno"], "modelo", 10)

    def test_bad_arguments_never_reach_the_network(self):
        fake = self.mock(FakeResponse(payload=self.answer(1)))
        for texts, model, dims in (([], "m", 0), (["a"] * 65, "m", 0), ([5], "m", 0), (["  "], "m", 0), ("texto", "m", 0),
                                   (["a"], "", 0), (["a"], "m" * 201, 0), (["a"], "m", 3), (["a"], "m", 99999), (["a"], "m", "512")):
            with self.subTest(texts=str(texts)[:20], model=model[:5], dims=dims), self.assertRaises(ValueError):
                self.provider._embed(texts, model, 10, dims=dims)
        self.assertEqual(fake.calls, [])

    def test_a_503_is_retried_once_when_the_time_allows(self):
        fake = self.mock(FakeResponse(status=503, payload={}), FakeResponse(payload=self.answer(1)))
        self.assertEqual(len(self.provider._embed(["uno"], "modelo", 20)), 1)
        self.assertEqual(len(fake.calls), 2)

    def test_a_short_budget_is_not_retried(self):
        fake = self.mock(FakeResponse(status=503, payload={}), FakeResponse(payload=self.answer(1)))
        with self.assertRaises(ProviderError):
            self.provider._embed(["uno"], "modelo", 3)
        self.assertEqual(len(fake.calls), 1)

    def test_the_shared_connection_is_only_for_embeddings_and_keeps_no_cookies(self):
        fake = self.mock(FakeResponse(payload=self.answer(1)))
        self.provider._embed(["uno"], "modelo", 10)
        self.assertEqual(len(fake.calls), 1, "went through the pooled session")
        request = MagicMock()
        request.get_full_url.return_value = "https://api.openai.com/v1/embeddings"
        cookie = MagicMock()
        policy = buddy_provider._SESSION.cookies._policy
        self.assertFalse(policy.set_ok(cookie, request))
        self.assertFalse(policy.return_ok(cookie, request))
        chat = self.mock(FakeResponse(payload={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}))
        with patch.object(buddy_provider.requests, "request", chat):
            self.provider._chat("m", [{"role": "user", "content": "hola"}], 10)
        self.assertEqual(len(chat.calls), 1, "chat keeps using plain requests")

    def test_a_stale_kept_alive_connection_is_retried_once_on_a_new_one(self):
        def stale(method, url, **kwargs):
            stale.calls += 1
            raise buddy_provider.requests.ConnectionError("Connection aborted")

        stale.calls = 0
        fresh = self.mock(FakeResponse(payload=self.answer(1)))
        with patch.object(buddy_provider._SESSION, "request", stale), patch.object(buddy_provider.requests, "request", fresh):
            self.assertEqual(len(self.provider._embed(["uno"], "modelo", 10)), 1)
        self.assertEqual((stale.calls, len(fresh.calls)), (1, 1))

    def test_only_a_network_error_with_time_left_is_retried(self):
        def stale(method, url, **kwargs):
            stale.calls += 1
            raise buddy_provider.requests.ConnectionError("Connection aborted")

        stale.calls = 0
        fresh = self.mock(FakeResponse(payload=self.answer(1)))
        with patch.object(buddy_provider._SESSION, "request", stale), patch.object(buddy_provider.requests, "request", fresh):
            with self.assertRaises(ProviderError):
                self.provider._embed(["uno"], "modelo", 1.0)  # no time for a second try
        self.assertEqual((stale.calls, len(fresh.calls)), (1, 0))
        failing = self.mock(FakeResponse(status=500, payload={}))
        with patch.object(buddy_provider.requests, "request", fresh):
            with self.assertRaises(ProviderError):
                self.provider._embed(["uno"], "modelo", 10)
        self.assertEqual(len(failing.calls), 1, "a 500 is not a stale connection")
        self.assertEqual(len(fresh.calls), 0)

    def test_defaults_by_provider(self):
        self.assertEqual(buddy_provider.default_embedding_model("gemini"), "gemini-embedding-2")
        self.assertEqual(buddy_provider.default_embedding_model("openai"), "text-embedding-3-small")
        self.assertEqual((buddy_provider.default_embedding_model("anthropic"), buddy_provider.default_embedding_model("custom")), ("", ""))
        self.assertEqual((buddy_provider.default_embedding_dims("gemini"), buddy_provider.default_embedding_dims("openai"),
                          buddy_provider.default_embedding_dims("custom")), (768, 512, 0))
        self.assertEqual([buddy_provider.supports_embeddings(name) for name in ("gemini", "openai", "custom", "anthropic", "otro")],
                         [True, True, True, False, False])


# ---------------------------------------------------------------------------------------------------- configuration
@tagged("post_install", "-at_install")
class TestSemanticConfiguration(TransactionCase):
    def setUp(self):
        super().setUp()
        self.P = self.env["ir.config_parameter"].sudo()
        self.Embedding = self.env["buddy.embedding"]
        for key in (SEMANTIC_PARAM, MODEL_PARAM, DIMS_PARAM):
            self.P.set_param(key, False)
        self.P.set_param("buddy_ia.api_key", "no-es-una-clave")

    def test_it_is_off_unless_an_administrator_turned_it_on(self):
        for provider in ("gemini", "openai", "custom", "anthropic"):
            self.P.set_param("buddy_ia.provider", provider)
            self.assertIsNone(self.Embedding._semantic_cfg(), provider)
        self.assertFalse(self.Embedding._semantic_requested())

    def test_the_defaults_follow_the_provider(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        self.P.set_param("buddy_ia.provider", "gemini")
        self.assertEqual(self.Embedding._semantic_cfg(), {"model": "gemini-embedding-2", "dims": 768, "provider": "gemini", "min_z": 1.5})
        self.P.set_param("buddy_ia.provider", "openai")
        self.assertEqual(self.Embedding._semantic_cfg(), {"model": "text-embedding-3-small", "dims": 512, "provider": "openai", "min_z": 1.5})

    def test_what_the_administrator_wrote_wins(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        self.P.set_param("buddy_ia.provider", "gemini")
        self.P.set_param(MODEL_PARAM, "gemini-embedding-001")
        self.P.set_param(DIMS_PARAM, "256")
        self.assertEqual(self.Embedding._semantic_cfg()["model"], "gemini-embedding-001")
        self.assertEqual(self.Embedding._semantic_cfg()["dims"], 256)

    def test_claude_and_a_custom_server_without_a_model_degrade_to_text(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        self.P.set_param("buddy_ia.provider", "anthropic")
        self.assertIsNone(self.Embedding._semantic_cfg())
        self.assertTrue(self.Embedding._semantic_requested())
        self.P.set_param("buddy_ia.provider", "custom")
        self.assertIsNone(self.Embedding._semantic_cfg())
        self.P.set_param(MODEL_PARAM, "mi-modelo")
        self.assertEqual(self.Embedding._semantic_cfg(), {"model": "mi-modelo", "dims": 0, "provider": "custom", "min_z": 1.5})

    def test_no_key_means_no_semantic_search(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        self.P.set_param("buddy_ia.provider", "gemini")
        self.P.set_param("buddy_ia.api_key", False)
        self.assertIsNone(self.Embedding._semantic_cfg())

    def test_the_strictness_is_a_technical_parameter_with_a_safe_range(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        self.P.set_param("buddy_ia.provider", "openai")
        self.assertEqual(self.Embedding._semantic_cfg()["min_z"], 1.5)
        self.P.set_param("buddy_ia.semantic_min_z", "2.5")
        self.assertEqual(self.Embedding._semantic_cfg()["min_z"], 2.5)
        for value in ("abc", "-1", "7", "nan", "inf"):
            self.P.set_param("buddy_ia.semantic_min_z", value)
            self.assertEqual(self.Embedding._semantic_cfg()["min_z"], 1.5, value)

    def test_a_garbage_value_is_never_an_error(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        self.P.set_param("buddy_ia.provider", "openai")
        for value in ("abc", "-5", "3", "99999", "1e3"):
            self.P.set_param(DIMS_PARAM, value)
            self.assertEqual(self.Embedding._semantic_cfg()["dims"], 512, value)


# ------------------------------------------------------------------------------------------------------- the indexer
@tagged("post_install", "-at_install")
class TestSemanticIndexing(SemanticCase):
    def setUp(self):
        super().setUp()
        self.group = new_group(self.env, "T Sem Idx")
        self.source = new_source(self.env, "Procesos", [self.group])
        self.docs = [new_document(self.env, self.source, "Documento %d" % number, "Texto del documento %d sobre backups." % number)
                     for number in range(3)]

    def test_the_backlog_is_indexed_and_the_database_agrees_with_the_hash(self):
        result = self.index()
        self.assertEqual((result["done"], result["left"], result["state"]), (3, 0, "ok"))
        rows = self.rows()
        self.assertEqual({row[1:3] for row in rows}, {("qa-embed", DIMS)})
        self.assertEqual(len(rows), 3)
        # `_pending_count` computes the hash in SQL: zero left proves it equals the Python one (accents, newline, cut).
        self.assertEqual(self.Embedding._pending_count(self.cfg()), 0)
        self.assertEqual(self.Embedding._state()[0], "ok")

    def test_a_second_pass_sends_nothing(self):
        self.index()
        calls = self.embed.call_count
        self.assertEqual(self.index()["done"], 0)
        self.assertEqual(self.embed.call_count, calls)

    def test_only_the_changed_fragment_is_sent_again(self):
        self.index()
        chunk = self.Chunk.search([("document_id", "=", self.docs[1].id)])
        chunk.write({"content": "Texto nuevo sobre vacaciones."})
        self.embed.reset_mock()
        result = self.index()
        self.assertEqual(result["done"], 1)
        texts = self.embed.call_args[0][0]
        self.assertEqual(len(texts), 1)
        self.assertIn("Texto nuevo sobre vacaciones.", texts[0])

    def test_renaming_a_document_refreshes_its_vectors_only(self):
        self.index()
        self.docs[0].write({"name": "Otro nombre"})
        self.embed.reset_mock()
        self.assertEqual(self.index()["done"], 1)
        self.assertIn("Otro nombre", self.embed.call_args[0][0][0])

    def test_the_text_sent_is_context_newline_content_within_the_cut(self):
        self.index()
        sent = [text for call in self.embed.call_args_list for text in call[0][0]]
        chunk = self.Chunk.search([("document_id", "=", self.docs[0].id)])
        self.assertIn(buddy_vectors.embedding_text(chunk.context_text, chunk.content), sent)

    def test_a_new_model_replaces_every_vector_and_nothing_is_mixed(self):
        self.index()
        self.P.set_param(MODEL_PARAM, "otro-modelo")
        self.assertEqual(self.Embedding._pending_count(self.cfg()), 3)
        result = self.index()
        self.assertEqual((result["done"], result["left"]), (3, 0))
        self.assertEqual({row[1] for row in self.rows()}, {"otro-modelo"})
        self.assertEqual(len(self.rows()), 3)

    def test_a_new_size_replaces_every_vector(self):
        self.index()
        self.P.set_param(DIMS_PARAM, "16")
        self.assertEqual(self.index()["done"], 3)
        self.assertEqual({row[2] for row in self.rows()}, {16})

    def test_a_pass_cut_short_is_resumed_by_the_next_one(self):
        for number in range(40):
            new_document(self.env, self.source, "Extra %d" % number, "Fragmento extra %d sobre servidores." % number)
        answers = [fake_embed, ProviderError("rate_limited", "cuota")]
        calls = {"n": 0}

        def flaky(texts, model, timeout, dims=0, settings=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise answers[1]
            return fake_embed(texts, model, timeout, dims, settings)

        self.embed.side_effect = flaky
        first = self.index()
        self.assertEqual(first["state"], "error:rate_limited")
        self.assertEqual(first["done"], buddy_vectors.BATCH_ITEMS)
        self.assertEqual(self.Embedding._state()[0], "error:rate_limited")
        self.assertEqual(calls["n"], 2, "it stopped at the first failure: no retry loop")
        self.embed.side_effect = fake_embed
        second = self.index()
        self.assertEqual((second["state"], second["left"]), ("ok", 0))
        self.assertEqual(first["done"] + second["done"], 43)
        self.assertEqual(len(self.rows()), 43)

    def test_a_deadline_already_gone_does_nothing(self):
        result = self.Embedding._index_pending(time.monotonic() - 1)
        self.assertEqual((result["done"], result["left"]), (0, 3))
        self.embed.assert_not_called()

    def test_a_refused_key_stops_the_pass_without_retrying(self):
        self.embed.side_effect = ProviderError("invalid_key", "clave")
        result = self.index()
        self.assertEqual((result["done"], result["state"]), (0, "error:invalid_key"))
        self.assertEqual(self.embed.call_count, 1)

    def test_a_timeout_stops_the_pass(self):
        self.embed.side_effect = TimeoutError("tarda")
        self.assertEqual(self.index()["state"], "error:provider_timeout")
        self.assertEqual(self.embed.call_count, 1)

    def test_a_refused_fragment_is_not_offered_again_until_its_text_changes(self):
        bad = new_document(self.env, self.source, "Malo", "TEXTO-RECHAZADO por el proveedor")

        def picky(texts, model, timeout, dims=0, settings=None):
            if any("TEXTO-RECHAZADO" in text for text in texts):
                raise ProviderError("unavailable", "rechazado", status=400)
            return fake_embed(texts, model, timeout, dims, settings)

        self.embed.side_effect = picky
        self.index()
        self.embed.reset_mock()
        result = self.index()
        self.assertEqual(result["done"], 0)
        self.embed.assert_not_called()
        self.assertEqual(result["left"], 1, "still pending, only not retried now")
        self.Chunk.search([("document_id", "=", bad.id)]).write({"content": "Texto corregido sobre servidores."})
        self.assertEqual(self.index()["done"], 1, "changed text: tried again")

    def test_a_fragment_deleted_while_indexing_is_not_an_error(self):
        chunk = self.Chunk.search([("document_id", "=", self.docs[0].id)])
        real = self.Embedding._embed_group

        def deleting(group, cfg, settings, deadline):
            vectors = real(group, cfg, settings, deadline)
            self.env.cr.execute("DELETE FROM buddy_chunk WHERE id = %s", (chunk.id,))
            return vectors

        with patch.object(self.env.registry["buddy.embedding"], "_embed_group", side_effect=deleting):
            result = self.index()
        self.assertEqual(result["state"], "ok")
        self.assertEqual(len(self.rows()), 2)

    def test_the_state_is_not_rewritten_on_every_pass(self):
        self.index()
        written = self.Embedding._state()
        with patch.object(type(self.P), "set_param") as set_param:
            self.index()
        set_param.assert_not_called()
        self.assertEqual(self.Embedding._state(), written)
        self.P.set_param(STATE_PARAM, "ok|2020-01-01 00:00:00")
        self.index()
        self.assertNotEqual(self.Embedding._state()[1], "2020-01-01 00:00:00")

    def test_the_coverage_counts_only_active_folders(self):
        self.index()
        self.assertEqual(sum(item[1] for item in self.Embedding._coverage().values()), 3)
        self.source.active = False
        self.assertEqual(self.Embedding._coverage(), {})

    def test_one_bad_text_does_not_hold_back_the_rest(self):
        new_document(self.env, self.source, "Malo", "TEXTO-RECHAZADO por el proveedor")

        def picky(texts, model, timeout, dims=0, settings=None):
            if any("TEXTO-RECHAZADO" in text for text in texts):
                raise ProviderError("unavailable", "rechazado", status=400)
            return fake_embed(texts, model, timeout, dims, settings)

        self.embed.side_effect = picky
        result = self.index()
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["done"], 3)
        self.assertEqual(result["left"], 1, "the refused fragment waits for the next pass")
        self.assertEqual(len(self.rows()), 3)

    def test_too_many_refused_texts_stop_the_pass(self):
        for number in range(8):
            new_document(self.env, self.source, "Malo %d" % number, "TEXTO-RECHAZADO %d" % number)
        self.embed.side_effect = ProviderError("unavailable", "rechazado", status=400)
        result = self.index()
        self.assertEqual(result["state"], "error:invalid_request")
        self.assertLessEqual(self.embed.call_count, 1 + buddy_vectors.BATCH_ITEMS + 2)

    def test_a_provider_that_ignores_the_requested_size_stores_nothing(self):
        self.embed.side_effect = lambda texts, model, timeout, dims=0, settings=None: fake_embed(texts, model, timeout, 16, settings)
        result = self.index()
        self.assertEqual((result["done"], result["state"]), (0, "error:dimensions"))
        self.assertEqual(self.rows(), [])

    def test_an_answer_that_is_not_vectors_is_skipped_not_looped(self):
        self.embed.side_effect = ValueError("respuesta inválida")
        result = self.index()
        self.assertEqual(result["done"], 0)
        self.assertLessEqual(self.embed.call_count, 1 + 3 + 3)

    def test_above_the_scan_limit_it_pauses_and_says_so(self):
        with patch.object(buddy_vectors, "MAX_SCAN_FLOATS", DIMS * 2):
            result = self.index()
        self.assertEqual((result["done"], result["state"]), (0, "limit"))
        self.embed.assert_not_called()
        self.assertEqual(self.Embedding._state()[0], "limit")

    def test_archived_folders_are_not_indexed(self):
        self.source.active = False
        self.assertEqual(self.index()["done"], 0)
        self.embed.assert_not_called()

    def test_off_means_off(self):
        self.P.set_param(SEMANTIC_PARAM, False)
        self.assertEqual(self.index(), {"done": 0, "left": 0, "state": "off"})
        self.embed.assert_not_called()

    def test_deleting_a_document_deletes_its_vectors(self):
        self.index()
        self.docs[0].unlink()
        self.assertEqual(len(self.rows()), 2)

    def test_each_request_is_saved_when_it_commits(self):
        with patch.object(self.env.cr, "commit") as commit:
            self.index(commit=True)
        self.assertGreaterEqual(commit.call_count, 2)

    def test_the_cron_indexes_after_the_folders_even_without_a_drive_key(self):
        self.P.set_param("buddy_ia.drive_key", False)
        with patch.object(self.env.cr, "commit"):
            self.env["buddy.source"]._cron_sync()
        self.assertEqual(len(self.rows()), 3)

    def test_the_cron_calls_itself_again_while_work_is_left(self):
        for number in range(40):
            new_document(self.env, self.source, "Extra %d" % number, "Fragmento extra %d sobre servidores." % number)
        with patch.object(self.env.cr, "commit"), patch.object(
                self.env.registry["buddy.embedding"], "_cron_index", return_value={"done": 5, "left": 30, "state": "ok"}), patch.object(
                self.env.registry["buddy.source"], "_wake_sync_cron") as wake:
            self.env["buddy.source"]._cron_sync()
        wake.assert_called_once()

    def test_the_cron_does_not_spin_after_an_error(self):
        with patch.object(self.env.cr, "commit"), patch.object(
                self.env.registry["buddy.embedding"], "_cron_index", return_value={"done": 0, "left": 30, "state": "error:rate_limited"}), patch.object(
                self.env.registry["buddy.source"], "_wake_sync_cron") as wake:
            self.env["buddy.source"]._cron_sync()
        wake.assert_not_called()

    def test_the_summary_of_a_folder_counts_fragments_with_meaning(self):
        self.source.invalidate_recordset()
        self.assertEqual(self.source.semantic_summary, "0 de 3 fragmentos con significado")
        self.index()
        self.source.invalidate_recordset()
        self.assertEqual(self.source.semantic_summary, "3 de 3 fragmentos con significado")
        self.P.set_param(SEMANTIC_PARAM, False)
        self.source.invalidate_recordset()
        self.assertFalse(self.source.semantic_summary)

    def test_purging_erases_every_vector_and_the_state(self):
        self.index()
        self.assertEqual(self.Embedding._purge_all(), 3)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.Embedding._state(), ("", ""))
        self.assertEqual(self.Chunk.search_count([]), 3, "the fragments are untouched")


@tagged("post_install", "-at_install")
class TestSemanticReuse(SemanticCase):
    """A document that is read again only pays for the pieces of text that changed."""

    LONG = "\n\n".join("Párrafo %d sobre procedimientos internos. " % n + "Detalle del paso número %d. " % n * 14 for n in range(12))

    def setUp(self):
        super().setUp()
        self.group = new_group(self.env, "T Sem Reuse")
        self.source = new_source(self.env, "Docs", [self.group])

    def sync(self, text, md5, modified):
        client = FakeClient(files=[drive_file("fileR", "Guía R", modified=modified, md5=md5)], texts={"fileR": text})
        self.source._sync(client=client)

    def chunks(self):
        return self.Chunk.search([("source_id", "=", self.source.id)], order="sequence")

    def test_unchanged_pieces_keep_their_vectors_when_the_document_changes(self):
        self.sync(self.LONG, "m1", "2026-01-01T10:00:00.000Z")
        total = len(self.chunks())
        self.assertGreater(total, 3)
        self.assertEqual(self.index()["done"], total)
        before = {chunk.content: chunk.id for chunk in self.chunks()}
        old_ids = set(before.values())
        # The last paragraph changes; the first fragments are word for word the same.
        edited = self.LONG.rsplit("\n\n", 1)[0] + "\n\nPárrafo nuevo sobre teletrabajo, escrito para esta prueba."
        self.sync(edited, "m2", "2026-02-01T10:00:00.000Z")
        self.assertFalse(old_ids & set(self.chunks().ids), "the fragments were replaced")
        self.env.cr.execute("SELECT count(*) FROM buddy_embedding")
        reused = self.env.cr.fetchone()[0]
        self.assertGreater(reused, 0, "the unchanged pieces came with their vectors")
        self.assertLess(reused, len(self.chunks()))
        self.embed.reset_mock()
        result = self.index()
        self.assertEqual(result["done"], len(self.chunks()) - reused)
        self.assertEqual(sum(len(call[0][0]) for call in self.embed.call_args_list), len(self.chunks()) - reused)
        self.assertEqual(self.Embedding._pending_count(self.cfg()), 0)

    def test_nothing_is_reused_when_the_text_is_all_new(self):
        self.sync(self.LONG, "m1", "2026-01-01T10:00:00.000Z")
        self.index()
        self.sync("Un documento completamente distinto sobre otro tema.", "m2", "2026-02-01T10:00:00.000Z")
        self.env.cr.execute("SELECT count(*) FROM buddy_embedding")
        self.assertEqual(self.env.cr.fetchone()[0], 0)


# ---------------------------------------------------------------------------------------------------------- retrieval
@tagged("post_install", "-at_install")
class TestHybridRetrieval(SemanticCase):
    def setUp(self):
        super().setUp()
        self.g_ops, self.g_hr = new_group(self.env, "T Sem Ops"), new_group(self.env, "T Sem HR")
        self.ana = new_user(self.env, "t_sem_ana", extra_groups=[self.g_ops])
        self.leo = new_user(self.env, "t_sem_leo")
        ops = new_source(self.env, "Operaciones", [self.g_ops])
        hr = new_source(self.env, "RRHH", [self.g_hr])
        self.urgent = new_document(self.env, ops, URGENT_DOC, URGENT_TEXT)
        self.backups = new_document(self.env, ops, "Resguardo", "Los backups de Odoo se guardan en odoo.sh.")
        self.vacations = new_document(self.env, ops, "Vacaciones", "Las vacaciones se piden con tres semanas de aviso.")
        self.salary = new_document(self.env, hr, "Escala salarial", "La escala salarial del gerente es confidencial. Tiempo de resolución urgente.")
        self.index()

    def test_the_search_by_meaning_finds_what_the_words_cannot(self):
        self.assertEqual(self.titles(self.retrieve(self.ana, URGENT_QUESTION, semantic=None)), [], "words alone find nothing")
        hits = self.retrieve(self.ana, URGENT_QUESTION)
        self.assertEqual(self.titles(hits)[0], URGENT_DOC)
        self.assertEqual(self.last_cfg["semantic_info"]["state"], "used")

    def test_fusion_keeps_what_the_text_search_found_and_adds_the_semantic_ones(self):
        hits = self.retrieve(self.ana, "¿Dónde están los respaldos de odoo? ¿y cuánto demoran en arreglar algo grave?")
        self.assertEqual(set(self.titles(hits)), {"Resguardo", URGENT_DOC})

    def test_a_fragment_both_searches_like_comes_first(self):
        hits = self.retrieve(self.ana, "¿Cuánto tarda en resolverse un incidente urgente?")
        self.assertEqual(self.titles(hits)[0], URGENT_DOC)

    def test_the_budget_the_cap_and_the_neighbours_still_apply(self):
        ops = self.env["buddy.source"].search([("name", "=", "Operaciones")])
        document = new_document(self.env, ops, "Largo", "x")
        self.Chunk.search([("document_id", "=", document.id)]).unlink()
        for sequence in range(8):
            self.Chunk.create({"document_id": document.id, "sequence": sequence, "content": "Plazo de resolución urgente parte %d." % sequence})
        self.index()
        hits = self.retrieve(self.ana, "Plazo de resolución urgente parte", top_k=6)
        own = [hit for hit in hits if hit["doc_id"] == document.id and not hit["neighbor"]]
        self.assertTrue(own)
        self.assertLessEqual(len(own), 3, "no more than three fragments of one document")
        self.assertLessEqual(sum(len(hit["text"]) for hit in hits), 7000)
        self.assertTrue(all("neighbor" in hit and "sequence" in hit for hit in hits))

    def test_permissions_apply_before_scoring(self):
        seen = []
        real = buddy_embedding.vec.score_rows

        def spy(query, rows, dims):
            rows = list(rows)
            seen.append({row[0] for row in rows})
            return real(query, rows, dims)

        with patch.object(buddy_embedding.vec, "score_rows", spy):
            hits = self.retrieve(self.ana, URGENT_QUESTION)
        self.assertNotIn("Escala salarial", self.titles(hits), "an identical-looking fragment of a folder she cannot read")
        visible = set(self.Chunk.with_user(self.ana).search([]).ids)
        hidden = set(self.Chunk.sudo().search([("document_id", "=", self.salary.id)]).ids)
        self.assertTrue(seen and seen[0])
        self.assertLessEqual(seen[0], visible)
        self.assertFalse(seen[0] & hidden)
        self.assertEqual(self.retrieve(self.leo, URGENT_QUESTION), [], "a person with no folder gets nothing")

    def test_each_person_gets_only_their_folders(self):
        admin = new_admin(self.env)
        titles = self.titles(self.retrieve(admin, "¿escala salarial del gerente, sueldo?"))
        self.assertIn("Escala salarial", titles)
        self.assertNotIn("Escala salarial", self.titles(self.retrieve(self.ana, "¿escala salarial del gerente, sueldo?")))

    def test_nobody_reads_the_vectors(self):
        admin = new_admin(self.env)
        for user in (self.ana, admin):
            with self.assertRaises(AccessError):
                self.env["buddy.embedding"].with_user(user).search([])
            with self.assertRaises(AccessError):
                self.env["buddy.embedding"].with_user(user).browse(1).read(["vector"])

    def test_vectors_of_another_model_are_never_compared(self):
        self.P.set_param(MODEL_PARAM, "otro-modelo")  # nothing is indexed with it yet
        self.assertEqual(self.titles(self.retrieve(self.ana, URGENT_QUESTION)), [])
        self.assertEqual(self.last_cfg["semantic_info"]["state"], "used")

    def test_a_provider_that_fails_leaves_the_text_search_working(self):
        self.embed.side_effect = ProviderError("unavailable", "caído", status=503)
        hits = self.retrieve(self.ana, "¿Dónde están los backups de odoo?")
        self.assertEqual(self.titles(hits), ["Resguardo"])
        self.assertEqual(self.last_cfg["semantic_info"]["state"], "failed")

    def test_a_timeout_and_a_garbage_answer_also_degrade(self):
        for error in (TimeoutError("tarda"), ValueError("basura"), OSError("red")):
            buddy_embedding._QUERIES.clear()
            self.embed.side_effect = error
            hits = self.retrieve(self.ana, "¿Dónde están los backups de odoo?")
            self.assertEqual(self.titles(hits), ["Resguardo"], type(error).__name__)
            self.assertEqual(self.last_cfg["semantic_info"]["state"], "failed")
            for key in list(buddy_embedding._BREAKER._state):
                buddy_embedding._BREAKER.success(key)

    def test_a_question_the_provider_refuses_is_not_an_outage(self):
        self.embed.side_effect = ProviderError("unavailable", "rechazada", status=400)
        self.embed.reset_mock()
        for number in range(5):
            self.retrieve(self.ana, "¿Dónde están los backups de odoo? intento %d" % number)
        self.assertEqual(self.last_cfg["semantic_info"]["state"], "failed", "never «paused»")
        self.assertEqual(self.embed.call_count, 5)

    def test_an_answer_from_the_cache_does_not_reopen_a_closed_provider(self):
        self.retrieve(self.ana, URGENT_QUESTION)  # now in the cache
        key = self.Embedding._breaker_key(self.cfg())
        buddy_embedding._BREAKER.failure(key)
        buddy_embedding._BREAKER.failure(key)
        self.embed.reset_mock()
        self.retrieve(self.ana, URGENT_QUESTION)  # answered from the cache: the provider was not asked
        self.embed.assert_not_called()
        buddy_embedding._BREAKER.failure(key)  # the third in a row: the earlier two were not forgotten
        self.assertFalse(buddy_embedding._BREAKER.allowed(key))

    def test_a_question_already_asked_is_answered_while_the_provider_is_paused(self):
        self.retrieve(self.ana, URGENT_QUESTION)  # now in the cache
        key = self.Embedding._breaker_key(self.cfg())
        for unused in range(3):
            buddy_embedding._BREAKER.failure(key)
        self.embed.reset_mock()
        hits = self.retrieve(self.ana, URGENT_QUESTION)
        self.assertEqual(self.titles(hits)[0], URGENT_DOC)
        self.assertEqual(self.last_cfg["semantic_info"]["state"], "used")
        self.embed.assert_not_called()
        self.retrieve(self.ana, "Otra pregunta que nadie hizo todavía")
        self.assertEqual(self.last_cfg["semantic_info"]["state"], "paused")

    def test_the_cache_and_the_pause_belong_to_one_destination(self):
        first = self.Embedding._breaker_key(self.cfg())
        self.P.set_param("buddy_ia.api_base", "https://otro.test/v1")
        self.assertNotEqual(first, self.Embedding._breaker_key(self.cfg()))
        self.retrieve(self.ana, URGENT_QUESTION)
        calls = self.embed.call_count
        self.P.set_param("buddy_ia.api_base", "https://llm.test/v1")
        self.retrieve(self.ana, URGENT_QUESTION)
        self.assertGreater(self.embed.call_count, calls, "a vector made for another address is not reused")

    def test_the_planner_setting_is_put_back_after_a_search(self):
        self.env.cr.execute("SHOW enable_nestloop")
        before = self.env.cr.fetchone()[0]
        self.retrieve(self.ana, URGENT_QUESTION)
        self.env.cr.execute("SHOW enable_nestloop")
        self.assertEqual(self.env.cr.fetchone()[0], before)
        with patch.object(buddy_vectors, "MAX_SCAN_FLOATS", DIMS * 2):  # also on the early exit
            self.retrieve(self.ana, URGENT_QUESTION + " otra")
        self.env.cr.execute("SHOW enable_nestloop")
        self.assertEqual(self.env.cr.fetchone()[0], before)

    def test_the_cache_never_keeps_the_text_of_a_question(self):
        self.retrieve(self.ana, "¿Cuánto demoran en arreglar algo grave y secreto?")
        keys = list(buddy_embedding._QUERIES._items)
        self.assertTrue(keys)
        self.assertNotIn("secreto", str(keys))

    def test_the_scan_goes_in_batches_and_finds_the_same_things(self):
        with patch.object(buddy_embedding, "SCAN_BATCH", 1):
            hits = self.retrieve(self.ana, URGENT_QUESTION)
        self.assertEqual(self.titles(hits)[0], URGENT_DOC)
        self.assertEqual(self.last_cfg["semantic_info"]["state"], "used")

    def test_a_scan_that_takes_too_long_gives_up(self):
        with patch.object(buddy_embedding, "SCAN_BATCH", 1), patch.object(buddy_embedding, "SCAN_SECONDS", -1):
            hits = self.retrieve(self.ana, URGENT_QUESTION)
        self.assertEqual(hits, [])
        self.assertEqual(self.last_cfg["semantic_info"]["state"], "no_time")

    def test_it_is_never_scored_under_the_superuser(self):
        self.embed.reset_mock()
        found, info = self.env["buddy.embedding"].sudo()._semantic_rows(
            URGENT_QUESTION, self.env["buddy.source"].sudo().search([]), 6, self.cfg())
        self.assertEqual((found, info["state"]), ([], "failed"))
        self.embed.assert_not_called()

    def test_the_too_many_fragments_notice_is_logged_once(self):
        with patch.object(buddy_vectors, "MAX_SCAN_FLOATS", DIMS * 2), self.assertLogs("odoo.addons.buddy_ia.models.buddy_embedding", "WARNING") as logs:
            for number in range(4):
                self.retrieve(self.ana, URGENT_QUESTION + " %d" % number)
        self.assertEqual(len([line for line in logs.output if "demasiados fragmentos" in line]), 1)

    def test_a_provider_that_keeps_failing_is_not_asked_again_for_a_while(self):
        self.embed.side_effect = ProviderError("unavailable", "caído", status=503)
        self.embed.reset_mock()
        for number in range(3):
            self.retrieve(self.ana, "¿Dónde están los backups de odoo? versión %d" % number)
        self.assertEqual(self.embed.call_count, 3)
        self.retrieve(self.ana, "¿Dónde están los backups de odoo? otra vez")
        self.assertEqual(self.embed.call_count, 3, "paused")
        self.assertEqual(self.last_cfg["semantic_info"]["state"], "paused")
        key = self.Embedding._breaker_key(self.cfg())
        buddy_embedding._BREAKER.success(key)
        self.embed.side_effect = fake_embed
        self.retrieve(self.ana, "¿Dónde están los backups de odoo? y ahora sí")
        self.assertEqual(self.last_cfg["semantic_info"]["state"], "used")

    def test_greetings_thanks_and_tiny_messages_cost_no_request(self):
        self.embed.reset_mock()
        for message in ("Hola", "¡Hola, buen día!", "gracias", "ok", "sí", "¿sobre qué me podés guiar?", "   "):
            self.retrieve(self.ana, message)
        self.embed.assert_not_called()

    def test_the_same_question_is_embedded_once(self):
        self.embed.reset_mock()
        self.retrieve(self.ana, URGENT_QUESTION)
        self.retrieve(self.ana, "  ¿CUÁNTO demoran en arreglar   algo grave? ")
        self.assertEqual(self.embed.call_count, 1)

    def test_the_question_request_is_small_and_bounded_in_time(self):
        self.embed.reset_mock()
        self.retrieve(self.ana, URGENT_QUESTION * 40)
        texts, model, timeout = self.embed.call_args[0][:3]
        self.assertEqual(len(texts), 1)
        self.assertLessEqual(len(texts[0]), buddy_vectors.QUERY_TEXT_CHARS)
        self.assertEqual(model, "qa-embed")
        self.assertLessEqual(timeout, buddy_embedding.QUERY_TIMEOUT)

    def test_a_query_without_time_left_does_not_wait_for_the_provider(self):
        self.embed.reset_mock()
        self.retrieve(self.ana, URGENT_QUESTION, deadline=time.monotonic() + 20.3)
        self.embed.assert_not_called()
        self.assertEqual(self.last_cfg["semantic_info"]["state"], "no_time")

    def test_the_wait_never_eats_the_time_of_the_answer(self):
        self.retrieve(self.ana, URGENT_QUESTION + " bis", deadline=time.monotonic() + 22.5)
        self.assertLessEqual(self.embed.call_args[0][2], 2.6)

    def test_a_follow_up_is_embedded_with_the_previous_question(self):
        self.embed.reset_mock()
        context = {"questions": ["¿Cuánto demoran en arreglar algo grave?"], "titles": []}
        hits = self.retrieve(self.ana, "¿y si es de prioridad baja?", context)
        self.assertIn("arreglar algo grave", self.embed.call_args[0][0][0])
        self.assertIn("prioridad baja", self.embed.call_args[0][0][0])
        self.assertIn(URGENT_DOC, self.titles(hits))

    def test_more_vectors_than_the_scan_limit_means_text_only(self):
        with patch.object(buddy_vectors, "MAX_SCAN_FLOATS", DIMS * 2):
            hits = self.retrieve(self.ana, URGENT_QUESTION)
        self.assertEqual(self.titles(hits), [])
        self.assertEqual(self.last_cfg["semantic_info"]["state"], "too_big")

    def test_off_means_the_provider_is_never_asked(self):
        self.P.set_param(SEMANTIC_PARAM, False)
        self.embed.reset_mock()
        self.assertEqual(self.titles(self.retrieve(self.ana, URGENT_QUESTION, semantic=self.cfg())), [])
        self.embed.assert_not_called()

    def test_nothing_that_stands_out_adds_nothing(self):
        hits = self.retrieve(self.ana, "¿Cuál es el mejor restaurante de la ciudad?")
        self.assertEqual(hits, [])

    def test_the_statistics_row_says_what_the_search_did(self):
        engine = self.env["buddy.engine"].with_user(self.ana)
        with patch.object(self.cls, "_authorization_hash", return_value="A"), patch.object(
                self.cls, "_ask_model", return_value=dict(VALID, answer="Son 8 horas.")):
            engine.chat(URGENT_QUESTION)
        row = self.env["buddy.insight"].sudo().search([], order="id desc", limit=1)
        self.assertEqual(row.semantic_state, "used")
        self.assertGreaterEqual(row.semantic_ms, 0)
        self.assertGreater(row.retrieved_count, 0)

    def test_a_chat_still_answers_when_the_embeddings_are_down(self):
        self.embed.side_effect = ProviderError("unavailable", "caído", status=503)
        engine = self.env["buddy.engine"].with_user(self.ana)
        with patch.object(self.cls, "_authorization_hash", return_value="A"), patch.object(
                self.cls, "_ask_model", return_value=dict(VALID, answer="Los backups están en odoo.sh.")):
            result = engine.chat("¿Dónde están los backups de odoo?")
        self.assertEqual(result["answer"], "Los backups están en odoo.sh.")
        self.assertEqual([source["title"] for source in result["sources"]], ["Resguardo"])
        self.assertEqual(self.env["buddy.insight"].sudo().search([], order="id desc", limit=1).semantic_state, "failed")

    def test_the_embeddings_of_the_question_are_a_single_extra_call_per_query(self):
        engine = self.env["buddy.engine"].with_user(self.ana)
        self.embed.reset_mock()
        with patch.object(self.cls, "_authorization_hash", return_value="A"), patch.object(
                self.cls, "_ask_model", return_value=dict(VALID)) as ask:
            engine.chat(URGENT_QUESTION)
        self.assertEqual(self.embed.call_count, 1)
        self.assertEqual(ask.call_count, 1)


# --------------------------------------------------------------------------------------------- settings and privacy
@tagged("post_install", "-at_install")
class TestSemanticSettings(TransactionCase):
    def setUp(self):
        super().setUp()
        self.P = self.env["ir.config_parameter"].sudo()
        self.admin = new_admin(self.env)
        self.employee = new_user(self.env, "t_sem_emp")
        self.P.set_param("buddy_ia.provider", "gemini")
        self.P.set_param("buddy_ia.api_key", "no-es-una-clave")
        for key in (SEMANTIC_PARAM, MODEL_PARAM, DIMS_PARAM, STATE_PARAM):
            self.P.set_param(key, False)

    def settings(self, **values):
        return self.env["res.config.settings"].with_user(self.admin).create(values)

    def test_a_fresh_form_has_it_off(self):
        self.assertFalse(self.settings().buddy_semantic_enabled)

    def test_turning_it_on_shows_the_explicit_warning(self):
        record = self.settings(buddy_semantic_enabled=True)
        warning = record._onchange_buddy_semantic_enabled()["warning"]
        self.assertIn("TEXTO de todos los documentos", warning["message"])
        self.assertIn("restringido", warning["message"])
        record.buddy_semantic_enabled = False
        self.assertIsNone(record._onchange_buddy_semantic_enabled())

    def test_saving_it_on_is_logged_and_starts_the_indexing(self):
        record = self.settings(buddy_semantic_enabled=True)
        with patch.object(self.env.registry["buddy.source"], "_wake_sync_cron") as wake, self.assertLogs(
                "odoo.addons.buddy_ia.models.res_config_settings", "WARNING") as logs:
            record.set_values()
        self.assertTrue(self.semantic_on())
        wake.assert_called()
        self.assertTrue(any("activó la búsqueda por significado" in line for line in logs.output))

    def semantic_on(self):
        return self.env["buddy.embedding"]._semantic_requested()

    def test_turning_it_off_is_logged_and_keeps_the_vectors_until_purged(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        record = self.settings(buddy_semantic_enabled=False)
        with self.assertLogs("odoo.addons.buddy_ia.models.res_config_settings", "WARNING") as logs:
            record.set_values()
        self.assertFalse(self.semantic_on())
        self.assertTrue(any("apagó" in line for line in logs.output))

    def test_changing_the_provider_with_it_on_warns_and_is_logged(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        record = self.settings(buddy_provider="openai")
        warning = record._onchange_buddy_semantic_enabled()["warning"]
        self.assertIn("Cambiaste de proveedor", warning["title"])
        self.assertIn("TEXTO de todos los documentos", warning["message"])
        with patch.object(self.env.registry["buddy.source"], "_wake_sync_cron"), self.assertLogs(
                "odoo.addons.buddy_ia.models.res_config_settings", "WARNING") as logs:
            record.set_values()
        self.assertTrue(any("cambió el proveedor de IA" in line and "openai" in line for line in logs.output))
        self.assertFalse(any("api_key" in line or "no-es-una-clave" in line for line in logs.output))

    def test_turning_it_on_with_claude_says_nothing_is_sent(self):
        record = self.settings(buddy_provider="anthropic", buddy_semantic_enabled=True)
        with patch.object(self.env.registry["buddy.source"], "_wake_sync_cron"), self.assertLogs(
                "odoo.addons.buddy_ia.models.res_config_settings", "WARNING") as logs:
            record.set_values()
        self.assertTrue(any("no se envía nada" in line for line in logs.output))
        self.assertFalse(any("se envía al proveedor" in line for line in logs.output))

    def test_the_status_is_for_administrators_only(self):
        self.assertEqual(self.env["res.config.settings"]._fields["buddy_semantic_status"].groups, "base.group_system")

    def test_the_neutralization_removes_the_keys_and_the_switch(self):
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / "data" / "neutralize.sql").read_text(encoding="utf-8")
        for key in ("buddy_ia.api_key", "buddy_ia.drive_key", "buddy_ia.semantic_search"):
            self.assertIn(key, text)
        self.assertIn("DELETE FROM ir_config_parameter", text)

    def test_the_same_provider_with_it_on_does_not_warn(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        record = self.settings()
        self.assertIsNone(record._onchange_buddy_semantic_enabled())

    def test_uninstalling_erases_the_switch_too(self):
        from .. import uninstall_hook
        self.P.set_param(SEMANTIC_PARAM, "True")
        self.P.set_param(STATE_PARAM, "ok|2026-01-01 00:00:00")
        uninstall_hook(self.env)
        self.assertFalse(self.P.get_param(SEMANTIC_PARAM))
        self.assertFalse(self.P.get_param(STATE_PARAM))
        self.assertFalse(self.P.get_param("buddy_ia.api_key"))

    def test_claude_gets_a_clear_warning_instead_of_a_silent_failure(self):
        record = self.settings(buddy_provider="anthropic", buddy_semantic_enabled=True)
        warning = record._onchange_buddy_semantic_enabled()["warning"]
        self.assertIn("Claude", warning["message"])
        self.assertIn("solo por palabras", warning["message"])
        self.assertNotIn("TEXTO de todos", warning["message"], "nothing would be sent: no privacy warning needed")

    def test_the_form_still_opens_and_saves_when_the_provider_changed_under_it(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        record = self.settings(buddy_provider="anthropic")
        record.set_values()
        self.assertIsNone(self.env["buddy.embedding"]._semantic_cfg())

    def test_the_model_and_size_are_validated(self):
        for values in ({"buddy_embedding_model": "dos palabras"}, {"buddy_embedding_model": "m" * 201},
                       {"buddy_embedding_dims": 3}, {"buddy_embedding_dims": 99999}, {"buddy_embedding_dims": -1}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.settings(**values)
        self.settings(buddy_embedding_model="gemini-embedding-001", buddy_embedding_dims=256)
        self.settings(buddy_embedding_dims=0)

    def test_changing_the_model_restarts_the_indexing(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        record = self.settings(buddy_semantic_enabled=True, buddy_embedding_model="otro-modelo")
        with patch.object(self.env.registry["buddy.source"], "_wake_sync_cron") as wake:
            record.set_values()
        wake.assert_called()

    def test_saving_something_else_does_not_touch_the_indexing(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        record = self.settings(buddy_semantic_enabled=True, buddy_name="Otro")
        with patch.object(self.env.registry["buddy.source"], "_wake_sync_cron") as wake:
            record.set_values()
        wake.assert_not_called()

    def test_the_status_explains_each_situation(self):
        self.assertFalse(self.settings().buddy_semantic_status)
        self.P.set_param(SEMANTIC_PARAM, "True")
        self.assertIn("fragmentos con significado", self.settings().buddy_semantic_status)
        self.P.set_param("buddy_ia.provider", "anthropic")
        self.assertIn("no ofrece búsqueda por significado", self.settings().buddy_semantic_status)
        self.P.set_param("buddy_ia.provider", "custom")
        self.P.set_param("buddy_ia.api_base", "https://llm.test/v1")
        self.assertIn("Falta el nombre del modelo", self.settings().buddy_semantic_status)
        self.P.set_param("buddy_ia.provider", "gemini")
        self.P.set_param(STATE_PARAM, "error:rate_limited|2026-10-06 10:00:00")
        self.assertIn("limitó los pedidos", self.settings().buddy_semantic_status)
        self.P.set_param(STATE_PARAM, "error:algo_raro|2026-10-06 10:00:00")
        self.assertIn("algo_raro", self.settings().buddy_semantic_status)

    def test_the_status_warns_when_the_corpus_is_above_the_limit(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        group = new_group(self.env, "T Sem Big")
        source = new_source(self.env, "Grande", [group])
        new_document(self.env, source, "Uno", "Texto uno")
        with patch.object(buddy_vectors, "MAX_SCAN_FLOATS", 100):
            self.assertIn("supera el tope", self.settings().buddy_semantic_status)

    def test_only_an_administrator_can_purge(self):
        self.P.set_param(SEMANTIC_PARAM, "True")
        with self.assertRaises(AccessError):
            self.env["res.config.settings"].with_user(self.employee).create({}).action_buddy_purge_semantic()
        result = self.settings().action_buddy_purge_semantic()
        self.assertEqual(result["tag"], "display_notification")

    def test_the_connection_test_checks_the_embeddings(self):
        embeddings = self.env["buddy.embedding"]
        self.P.set_param(SEMANTIC_PARAM, "True")
        with patch.object(self.env.registry["buddy.provider"], "_embed", MagicMock(side_effect=fake_embed)):
            ok, line = embeddings._self_test_semantic(10)
        self.assertTrue(ok)
        self.assertIn("gemini-embedding-2", line)
        self.assertIn("768", line)
        with patch.object(self.env.registry["buddy.provider"], "_embed", MagicMock(side_effect=ProviderError("not_found", "no existe el modelo"))):
            ok, line = embeddings._self_test_semantic(10)
        self.assertFalse(ok)
        self.assertIn("no existe el modelo", line)
        with patch.object(self.env.registry["buddy.provider"], "_embed", MagicMock(side_effect=TimeoutError("x"))):
            self.assertFalse(embeddings._self_test_semantic(10)[0])
        self.P.set_param("buddy_ia.provider", "anthropic")
        self.assertFalse(embeddings._self_test_semantic(10)[0])

    def test_privacy_is_documented_where_the_administrator_reads_it(self):
        field = self.env["res.config.settings"]._fields["buddy_semantic_enabled"]
        self.assertIn("TODOS los documentos de TODAS las carpetas", field.help)
        view = self.env.ref("buddy_ia.res_config_settings_view_form_buddy")
        self.assertIn("restringido", view.arch_db)
