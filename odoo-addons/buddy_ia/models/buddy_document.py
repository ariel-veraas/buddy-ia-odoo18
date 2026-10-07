from odoo import api, fields, models


class BuddyDocument(models.Model):
    _name = "buddy.document"
    _description = "Documento de Drive"
    _order = "source_id, path, name"

    source_id = fields.Many2one("buddy.source", string="Carpeta", required=True, ondelete="cascade", index=True)
    # Technical and diagnostic fields: only administrators read them (the record rules already hide the whole
    # document from people without access to its folder; these stay private even for people who can read it).
    drive_id = fields.Char(string="ID en Drive", required=True, index=True, groups="base.group_system")
    name = fields.Char(string="Nombre", required=True)
    path = fields.Char(string="Ubicación", help="Subcarpetas dentro de la carpeta conectada.")
    web_view_link = fields.Char(string="Enlace")
    mime_type = fields.Char(string="Tipo")
    modified_time = fields.Char(string="Modificado en Drive")
    md5 = fields.Char(string="Huella", groups="base.group_system")
    state = fields.Selection([
        ("ready", "Listo"), ("empty", "Sin texto o tipo no compatible"), ("error", "Con error")],
        string="Estado", default="ready", required=True)
    error = fields.Char(string="Detalle", groups="base.group_system",
                        help="Por qué falló, o un aviso (por ejemplo, que el documento se leyó solo hasta cierto punto).")
    # Set when a previous pass died while processing this file (corrupt or too heavy): it is not retried until the
    # file changes in Drive, so one bad file cannot keep the folder from ever finishing.
    interrupted = fields.Boolean(string="Procesamiento interrumpido", default=False, groups="base.group_system")
    chunk_ids = fields.One2many("buddy.chunk", "document_id", string="Fragmentos")
    chunk_count = fields.Integer(string="Cantidad de fragmentos", compute="_compute_chunk_count")

    _sql_constraints = [("unique_drive_file", "unique(source_id, drive_id)", "Ese archivo ya está en la carpeta.")]

    @api.depends("name")
    @api.depends_context("uid")
    def _compute_display_name(self):
        """The name of a document only for people who can read it. Odoo shows the name of the record a many2one points to with
        the rights of the system, so without this a step of an onboarding journey (which anybody who has the journey can
        read) would tell the title of a document of a folder the person cannot see."""
        visible = set(self.with_user(self.env.uid).search([("id", "in", self.ids)]).ids) if self.ids else set()
        for record in self:
            record.display_name = record.name if record.id in visible else False

    @api.depends("chunk_ids")
    def _compute_chunk_count(self):
        counts = dict(self.env["buddy.chunk"]._read_group([("document_id", "in", self.ids)], ["document_id"], ["__count"]))
        for record in self:
            record.chunk_count = counts.get(record, 0)
