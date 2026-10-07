import logging

from odoo import api, fields, models
from .buddy_chunker import strip_toc
from .buddy_text import search_context

_logger = logging.getLogger(__name__)

# Accents are folded with translate() (built-in and immutable) instead of the unaccent extension, which may need
# a superuser to install. The same folding is applied to the user's question in buddy.engine, so "cotizacion"
# finds "cotización".
ACCENTS = "áéíóúüñÁÉÍÓÚÜÑ"
FOLDED = "aeiouunAEIOUUN"
# The search vector: the document's context (title, sub-folders, folder) weighs more than the body, so asking about
# "HelpDesk" finds the chunks of the document called "PR 46 HelpDesk" even when the chunk never says the word.
# A = context, B = body. buddy.engine ranks with explicit weights for these two letters.
FTS_EXPRESSION = (
    "setweight(to_tsvector('spanish'::regconfig, translate(coalesce(context_text, ''), '%(a)s', '%(f)s')), 'A') || "
    "setweight(to_tsvector('spanish'::regconfig, translate(coalesce(content, ''), '%(a)s', '%(f)s')), 'B')"
) % {"a": ACCENTS, "f": FOLDED}
# A chunk that is only the start of a document (where a table of contents lives) is checked on upgrade.
TOC_CHECKED_SEQUENCES = 2
# A chunk left with less text than this after removing its table of contents carried nothing else.
MIN_CONTENT_AFTER_TOC = 30


class BuddyChunk(models.Model):
    _name = "buddy.chunk"
    _description = "Fragmento de un documento de Drive"
    _order = "document_id, sequence"

    document_id = fields.Many2one("buddy.document", required=True, ondelete="cascade", index=True)
    # Stored copy of the folder: record rules and the search filter use it without joining through the document.
    source_id = fields.Many2one("buddy.source", related="document_id.source_id", store=True, index=True)
    sequence = fields.Integer(default=0)
    content = fields.Text(required=True)
    # Document title + sub-folders + folder, indexed with more weight than the body. Computed, so it follows renames.
    context_text = fields.Char(string="Contexto", compute="_compute_context_text", store=True)

    @api.depends("document_id.name", "document_id.path", "document_id.source_id.name")
    def _compute_context_text(self):
        for chunk in self:
            document = chunk.document_id
            chunk.context_text = search_context(document.name, document.path, document.source_id.name)

    def init(self):
        super().init()
        self._ensure_search_index()

    @api.model
    def _ensure_search_index(self):
        """Create (or recreate, once) the generated search column and its GIN index. Safe to run on every update."""
        cr = self.env.cr
        cr.execute("""SELECT generation_expression FROM information_schema.columns
                      WHERE table_schema = current_schema() AND table_name = 'buddy_chunk' AND column_name = 'buddy_fts'""")
        row = cr.fetchone()
        if row and not ("context_text" in (row[0] or "") and "setweight" in (row[0] or "")):
            # Column from a version that indexed the body only: replace it. Nothing is lost, it is derived data.
            cr.execute("DROP INDEX IF EXISTS buddy_chunk_fts_idx")
            cr.execute("ALTER TABLE buddy_chunk DROP COLUMN buddy_fts")
            row = None
        if not row:
            cr.execute("ALTER TABLE buddy_chunk ADD COLUMN buddy_fts tsvector GENERATED ALWAYS AS (%s) STORED" % FTS_EXPRESSION)
        cr.execute("CREATE INDEX IF NOT EXISTS buddy_chunk_fts_idx ON buddy_chunk USING gin (buddy_fts)")

    @api.model
    def _rebuild_search_data(self):
        """Upgrade step: fill the context of existing chunks and drop tables of contents. Idempotent, loses no text."""
        self._ensure_search_index()
        cr = self.env.cr
        cr.execute("""SELECT d.id, d.name, d.path, s.name FROM buddy_document d JOIN buddy_source s ON s.id = d.source_id
                      WHERE EXISTS (SELECT 1 FROM buddy_chunk c WHERE c.document_id = d.id)""")
        for document_id, name, path, folder in cr.fetchall():
            context = search_context(name, path, folder)
            cr.execute("UPDATE buddy_chunk SET context_text = %s WHERE document_id = %s AND context_text IS DISTINCT FROM %s",
                       (context, document_id, context))
        self.env.invalidate_all()
        chunks = self.sudo().search([("sequence", "<", TOC_CHECKED_SEQUENCES)])
        removed = cleaned = 0
        for chunk in chunks:
            content = strip_toc(chunk.content)
            if content == chunk.content:
                continue
            if len(content) < MIN_CONTENT_AFTER_TOC:
                chunk.unlink()
                removed += 1
            else:
                chunk.content = content
                cleaned += 1
        _logger.info("Buddy: search context rebuilt; tables of contents removed from %s chunks, %s dropped", cleaned, removed)
