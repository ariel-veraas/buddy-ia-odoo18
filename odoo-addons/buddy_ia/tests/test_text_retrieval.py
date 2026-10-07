from odoo.tests.common import TransactionCase, tagged

from ..models.buddy_engine import text_terms
from .common import new_document, new_group, new_source, new_user

CFG = {"top_k": 4}


@tagged("post_install", "-at_install")
class TestTextRetrieval(TransactionCase):
    """Full-text retrieval: finds the right chunks, ignores accents and never goes past the user's permissions."""

    def setUp(self):
        super().setUp()
        self.env["buddy.source"].with_context(active_test=False).search([]).unlink()
        self.g_sales = new_group(self.env, "T Text Sales")
        self.g_hr = new_group(self.env, "T Text HR")
        self.ana = new_user(self.env, "t_text_ana", extra_groups=[self.g_sales])
        self.leo = new_user(self.env, "t_text_leo")
        self.sales_source = new_source(self.env, "Ventas", [self.g_sales])
        self.hr_source = new_source(self.env, "RRHH", [self.g_hr])
        self.sales = new_document(self.env, self.sales_source, "Cómo crear una cotización",
                                  "Para crear una cotización abrí la aplicación Ventas y elegí Nuevo.")
        self.hr = new_document(self.env, self.hr_source, "Escala salarial",
                               "La escala salarial del gerente es confidencial y solo la ve RRHH.")

    def _retrieve(self, user, message, **cfg):
        return self.env["buddy.engine"].with_user(user)._retrieve(message, dict(CFG, **cfg))

    def test_finds_chunk_with_and_without_accents(self):
        for question in ("¿Cómo creo una cotización?", "como creo una cotizacion", "COTIZACIÓN"):
            hits = self._retrieve(self.ana, question)
            self.assertEqual([hit["title"] for hit in hits], ["Cómo crear una cotización"], question)
            self.assertEqual(hits[0]["owner"], "Ventas")
            self.assertTrue(0 < hits[0]["score"] < 1)

    def test_hit_carries_the_citation_data(self):
        hit = self._retrieve(self.ana, "cotización")[0]
        self.assertEqual(hit["doc_id"], self.sales.id)
        self.assertEqual(hit["link"], self.sales.web_view_link)
        self.assertEqual(hit["updated"], "2026-01-01")
        self.assertIn("aplicación Ventas", hit["text"])

    def test_weak_matches_are_dropped_when_a_much_better_one_exists(self):
        # Shares one stem with the question ("crear" ~ "creo") but is about something else.
        new_document(self.env, self.sales_source, "Alta de usuarios", "Pedí a IT que te ayude a crear tu usuario.")
        hits = self._retrieve(self.ana, "como creo una cotizacion")
        self.assertEqual([hit["title"] for hit in hits], ["Cómo crear una cotización"])

    def test_best_chunks_come_first_and_top_k_is_respected(self):
        for number in range(6):
            new_document(self.env, self.sales_source, "Copia %d" % number, "Para crear una cotización abrí Ventas, copia %d." % number)
        hits = self._retrieve(self.ana, "crear cotización", top_k=3)
        self.assertEqual(len(hits), 3)
        scores = [hit["score"] for hit in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_user_never_gets_chunks_outside_own_groups(self):
        self.assertEqual(self._retrieve(self.leo, "cotización de ventas"), [])
        self.assertEqual(self._retrieve(self.ana, "escala salarial del gerente"), [])
        hr_user = new_user(self.env, "t_text_hr", extra_groups=[self.g_hr])
        self.assertEqual([h["title"] for h in self._retrieve(hr_user, "escala salarial")], ["Escala salarial"])

    def test_archived_folders_and_error_documents_are_not_returned(self):
        self.hr_source.active = False
        admin = self.env.ref("base.user_admin")
        self.assertEqual(self._retrieve(admin, "escala salarial"), [])
        self.sales.chunk_ids.unlink()
        self.sales.state = "error"
        self.assertEqual(self._retrieve(self.ana, "cotización"), [])

    def test_question_without_useful_words_returns_nothing(self):
        self.assertEqual(self._retrieve(self.ana, "¿?"), [])
        self.assertEqual(self._retrieve(self.ana, "a la de"), [])

    def test_no_folders_at_all_returns_nothing(self):
        self.env["buddy.source"].search([]).unlink()
        self.assertEqual(self._retrieve(self.ana, "cotización"), [])

    def test_tsquery_syntax_in_the_question_is_inert(self):
        self.assertEqual(self._retrieve(self.ana, "cotización & ! ( | ) :* <-> 'x"), self._retrieve(self.ana, "cotización x"))
        self.assertEqual(self._retrieve(self.ana, "'; DROP TABLE buddy_chunk; --"), [])
        self.assertTrue(self.env["buddy.chunk"].search([]), "the table is still there")

    def test_text_terms_fold_accents_and_drop_symbols(self):
        self.assertEqual(text_terms("¿Cómo está la cotización?!"), ["como", "esta", "cotizacion"])

    def test_the_search_index_column_exists(self):
        self.env.cr.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'buddy_chunk' AND column_name = 'buddy_fts'")
        self.assertTrue(self.env.cr.fetchone())
        self.env.cr.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'buddy_chunk_fts_idx'")
        self.assertIn("gin", self.env.cr.fetchone()[0].lower())

    def test_new_chunks_are_searchable_immediately_and_accents_are_folded_in_storage(self):
        new_document(self.env, self.sales_source, "Política", "El reembolso de gastos de movilidad se pide por Gastos.")
        for question in ("reembolso movilidad", "REEMBOLSO MOVILIDAD", "reembolsó movilidád"):
            self.assertEqual([h["title"] for h in self._retrieve(self.ana, question)], ["Política"], question)
