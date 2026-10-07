"""18.0.4.1.0: search context in the index, tables of contents out, 6 fragments by default.

The models already created the `context_text` column and rebuilt the generated `buddy_fts` column while loading
(`buddy.chunk.init`). This step fills the context of the chunks that existed before, removes the table of contents
from the first chunks of each document and moves the old default of fragments per question (4) to the new one (6).
It is safe to run again: nothing is lost that is not derived data or a table of contents.
"""
from odoo import SUPERUSER_ID, api

OLD_TOP_K, NEW_TOP_K = "4", "6"


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["buddy.chunk"]._rebuild_search_data()
    parameters = env["ir.config_parameter"].sudo()
    # Only the old default moves; a value somebody chose on purpose stays as it is.
    if parameters.get_param("buddy_ia.top_k") == OLD_TOP_K:
        parameters.set_param("buddy_ia.top_k", NEW_TOP_K)
