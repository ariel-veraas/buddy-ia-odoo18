import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from .buddy_text import is_placeholder_email  # noqa: F401 (also imported from here by the tests)

CODE_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
def normalize_code(value):
    """The engine looks contacts up in lower case, so store them that way (and compare them that way)."""
    return value.strip().lower() if isinstance(value, str) else value


def validate_code(contacts, code, own_ids=()):
    """Friendly validation (format and case-insensitive uniqueness) that runs BEFORE the SQL constraint.

    `contacts` is any recordset of buddy.contact; `own_ids` are the records allowed to hold `code`.
    """
    if not code or not CODE_PATTERN.fullmatch(code):
        raise ValidationError(_("El código «%(code)s» no es válido: usá letras minúsculas, números, guion o guion bajo, sin espacios (hasta 32 caracteres).",
                                code=code or ""))
    # The table is tiny (a handful of contacts): compare in Python so "_" is never a SQL wildcard
    # and legacy rows saved with capitals are still caught.
    duplicate = contacts.sudo().search([("id", "not in", list(own_ids))]).filtered(
        lambda other: (other.code or "").strip().lower() == code)
    if duplicate:
        raise ValidationError(_("Ya existe un contacto con el código «%(code)s» (%(label)s).",
                                code=code, label=duplicate[0].label))


class BuddyContact(models.Model):
    _name = "buddy.contact"
    _description = "Persona o equipo al que el buddy deriva"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    code = fields.Char(string="Código", required=True,
                       help="Identificador corto que usa el buddy para elegir el contacto: it, rrhh, ventas, general… "
                            "Es único y se guarda en minúsculas. El código «general» se usa cuando ningún otro encaja.")
    label = fields.Char(string="Área", required=True)
    name = fields.Char(string="Nombre o equipo", required=True)
    email = fields.Char(string="Correo", required=True)
    # Shown as a warning in the list: the address is a sample («…@ejemplo.test») and nobody could write to it.
    is_placeholder = fields.Boolean(string="Correo de ejemplo", compute="_compute_is_placeholder")

    _sql_constraints = [("code_unique", "unique(code)", "Ya existe un contacto con ese código.")]

    @api.depends("email")
    def _compute_is_placeholder(self):
        for contact in self:
            contact.is_placeholder = is_placeholder_email(contact.email)

    @api.model_create_multi
    def create(self, vals_list):
        cleaned, seen = [], set()
        for vals in vals_list:
            if "code" in vals:
                vals = dict(vals, code=normalize_code(vals["code"]))
                validate_code(self, vals["code"])
                if vals["code"] in seen:
                    raise ValidationError(_("Ya existe un contacto con el código «%(code)s» (%(label)s).",
                                            code=vals["code"], label=vals.get("label") or ""))
                seen.add(vals["code"])
            cleaned.append(vals)
        return super().create(cleaned)

    def write(self, vals):
        if "code" in vals:
            vals = dict(vals, code=normalize_code(vals["code"]))
            if len(self) > 1:
                raise ValidationError(_("No se puede asignar el mismo código a varios contactos."))
            validate_code(self, vals["code"], self.ids)
        return super().write(vals)

    @api.constrains("code")
    def _check_code(self):
        for contact in self:
            validate_code(self, contact.code, contact.ids)
