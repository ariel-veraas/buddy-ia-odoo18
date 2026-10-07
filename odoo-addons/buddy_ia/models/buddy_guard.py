"""Fields that only administrators may read must not leak through a search or a group-by either.

`groups="base.group_system"` on a field refuses to READ it, but Odoo still lets any user who can read the model put that
field in a search domain (a yes/no oracle: «are there votes that cite document 7?») or group by it (and grouping by a
many2many returns the NAMES of the records, read with superuser rights: the title of a document in a folder the person
cannot see). `buddy.insight` and `buddy.feedback` keep a many2many to the documents a question cited for administrators
only, so they refuse any use of it by everybody else, in every way the ORM (and so the generic RPC) offers: `search`,
`search_count`, `search_read`, `read_group`, `web_read_group` and `_read_group`.
"""
from odoo import _, api, models
from odoo.exceptions import AccessError

from .buddy_privacy import mentions_field


class BuddyGuard(models.AbstractModel):
    _name = "buddy.guard"
    _description = "Campos de solo administradores: tampoco en búsquedas ni agrupaciones"

    # Names of the fields that only administrators may use anywhere (set by the models that inherit this one).
    _admin_only_fields = ()
    # Who may use them (administrators; a model can name more groups, like the onboarding managers of buddy.journey).
    _guard_groups = ("base.group_system",)

    def _guard_admin_fields(self, domain=None, specs=(), order=None):
        if self.env.su or not self._admin_only_fields or any(self.env.user.has_group(group) for group in self._guard_groups):
            return
        if mentions_field(domain, self._admin_only_fields, specs, order):
            raise AccessError(_("Solo un administrador puede buscar o agrupar por ese campo."))

    @api.model
    def _search(self, domain, offset=0, limit=None, order=None):
        self._guard_admin_fields(domain=domain, order=order)
        return super()._search(domain, offset=offset, limit=limit, order=order)

    @api.model
    def _read_group(self, domain, groupby=(), aggregates=(), having=(), offset=0, limit=None, order=None):
        self._guard_admin_fields(domain=list(domain or []) + list(having or []), specs=list(groupby or ()) + list(aggregates or ()), order=order)
        return super()._read_group(domain, groupby=groupby, aggregates=aggregates, having=having, offset=offset, limit=limit, order=order)
