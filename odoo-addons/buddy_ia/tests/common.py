"""Shared fixtures for the native tests: a fake Drive client, a fake HTTP response and small builders."""
import json
import uuid
from contextlib import contextmanager

from ..models.drive_client import DriveFile

DRIVE_URL = "https://drive.google.com/drive/folders/"
DOC_LINK = "https://docs.google.com/document/d/%s/edit"


def drive_file(file_id, name=None, modified="2026-01-01T10:00:00.000Z", md5=None, link=None, path="", mime="text/plain"):
    return DriveFile(id=file_id, name=name or file_id, mime_type=mime, modified_time=modified, md5=md5, path=path,
                     web_view_link=DOC_LINK % file_id if link is None else link)


class FakeClient:
    """Same interface as DriveClient (list_folder / fetch_text / truncated) with scripted answers."""

    def __init__(self, files=(), texts=None, errors=None):
        self.files = list(files)
        self.texts = dict(texts or {})  # file id -> text
        self.errors = dict(errors or {})  # file id -> exception to raise from fetch_text
        self.list_error = None
        self.truncated = False
        self.listed, self.fetched = [], []

    def list_folder(self, folder_id):
        self.listed.append(folder_id)
        if self.list_error:
            raise self.list_error
        return list(self.files)

    def fetch_text(self, drive_file_):
        self.fetched.append(drive_file_.id)
        if drive_file_.id in self.errors:
            raise self.errors[drive_file_.id]
        return self.texts.get(drive_file_.id, "")


class FakeResponse:
    """The slice of requests.Response that buddy.provider uses."""

    def __init__(self, status=200, payload=None, body=None):
        self.status_code = status
        data = body if body is not None else json.dumps(payload if payload is not None else {}).encode()
        self.body = data
        self.raw = self
        self.closed = False
        self.position = 0  # the body is consumed piece by piece, like a socket
        self.reads = []  # the `amount` of every read

    @property
    def consumed(self):
        return self.position

    def read(self, amount=None, decode_content=False):
        self.reads.append(amount)
        end = len(self.body) if amount is None else min(len(self.body), self.position + amount)
        piece, self.position = self.body[self.position:end], end
        return piece

    def close(self):
        self.closed = True


def new_group(env, name):
    return env["res.groups"].create({"name": name + " " + uuid.uuid4().hex[:6]})


def new_user(env, name, groups=("base.group_user",), extra_groups=()):
    ids = [env.ref(xmlid).id for xmlid in groups] + [group.id for group in extra_groups]
    return env["res.users"].with_context(no_reset_password=True).create({
        "name": name, "login": "buddy_%s_%s" % (name, uuid.uuid4().hex[:8]), "groups_id": [(6, 0, ids)]})


def new_admin(env, name="admin"):
    return new_user(env, name, groups=("base.group_user", "base.group_system"))


def new_source(env, name, groups, folder="Carpeta%s", **values):
    """A synchronized-looking folder owned by the given groups (created with full rights)."""
    vals = {"name": name, "folder_url": DRIVE_URL + (folder % uuid.uuid4().hex[:12]), "group_ids": [(6, 0, [g.id for g in groups])]}
    vals.update(values)
    return env["buddy.source"].create(vals)


def new_document(env, source, name, text, link=None, drive_id=None, **values):
    """A ready document with one chunk."""
    drive_id = drive_id or uuid.uuid4().hex
    vals = {"source_id": source.id, "drive_id": drive_id, "name": name, "state": "ready",
            "web_view_link": DOC_LINK % drive_id if link is None else link, "modified_time": "2026-01-01T10:00:00.000Z"}
    vals.update(values)
    document = env["buddy.document"].create(vals)
    env["buddy.chunk"].create({"document_id": document.id, "sequence": 0, "content": text})
    return document


def detach_groups(env, source):
    """Leave a folder without groups (the constraint forbids it through the ORM, a legacy row could still exist)."""
    env.cr.execute("DELETE FROM buddy_source_group_rel WHERE source_id = %s", (source.id,))
    source.invalidate_recordset()
    env["res.users"].invalidate_model()


def touch(env, record):
    """Move write_date forward. A TransactionCase runs in one database transaction, where now() never advances;
    in production every edit is its own transaction and write_date changes by itself."""
    env.cr.execute("UPDATE %s SET write_date = write_date + interval '1 second' WHERE id = %%s" % record._table, (record.id,))
    record.invalidate_recordset(["write_date"])


@contextmanager
def local_test_transaction(engine, lock=False):
    """Stand-in for buddy.engine._fresh_engine: fixtures live in the (uncommitted) test transaction, so the
    independent cursor of production would not see them. Locking and isolation are not covered by these tests."""
    yield engine
