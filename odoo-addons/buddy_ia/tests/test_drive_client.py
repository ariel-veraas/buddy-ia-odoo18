"""Standalone tests for drive_client (no Odoo, no network).

Run from the repo root:
    .venv\\Scripts\\python.exe -m unittest odoo-addons/buddy_ia/tests/test_drive_client.py
"""
import importlib.util
import io
import json
import sys
import time
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

MODULE_PATH = Path(__file__).resolve().parents[1] / "models" / "drive_client.py"
_spec = importlib.util.spec_from_file_location("buddy_drive_client_under_test", MODULE_PATH)
dc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dc
_spec.loader.exec_module(dc)

EMAIL = "buddy@proyecto.iam.gserviceaccount.com"
ROOT_ID = "ROOTFOLDER_1234567890"
_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _PRIVATE.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_PEM = _PRIVATE.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode()
KEY_BODY = "".join(PRIVATE_PEM.splitlines()[1:-1])[:60]


def key_json(**over):
    data = {
        "type": "service_account", "project_id": "proyecto", "client_email": EMAIL,
        "private_key": PRIVATE_PEM, "token_uri": dc.TOKEN_URI,
    }
    data.update(over)
    return json.dumps({k: v for k, v in data.items() if v is not None})


class FakeResponse:
    def __init__(self, status=200, body=None, content=None, headers=None):
        self.status_code = status
        self._body = body
        self.content = content if content is not None else b""
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_content(self, size):
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]

    def close(self):
        self.closed = True


def api_error(status, reason=None):
    body = {"error": {"code": status, "errors": [{"reason": reason}] if reason else []}}
    return FakeResponse(status, body)


TOKEN_OK = {"access_token": "ya29.SECRET_TOKEN_VALUE", "expires_in": 3600}


class FakeSession:
    """Programmable session: handler(call) -> FakeResponse | Exception | None (default)."""

    def __init__(self, handler=None, token_handler=None):
        self.calls = []
        self.handler = handler
        self.token_handler = token_handler
        self.token_calls = 0

    def request(self, method, url, params=None, data=None, headers=None, timeout=None, stream=False):
        call = {"method": method, "url": url, "params": params or {}, "data": data,
                "headers": headers or {}, "timeout": timeout}
        self.calls.append(call)
        if url == dc.TOKEN_URI:
            self.token_calls += 1
            if self.token_handler:
                out = self.token_handler(call)
                if out is not None:
                    return self._unwrap(out)
            return FakeResponse(200, dict(TOKEN_OK))
        out = self.handler(call) if self.handler else None
        if out is None:
            return FakeResponse(404, {"error": {}})
        return self._unwrap(out)

    @staticmethod
    def _unwrap(out):
        if isinstance(out, Exception):
            raise out
        return out

    def api_calls(self):
        return [c for c in self.calls if c["url"] != dc.TOKEN_URI]


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_client(handler=None, **kw):
    session = FakeSession(handler, kw.pop("token_handler", None))
    sleeps = []
    clock = kw.pop("clock", None) or Clock()
    client = dc.DriveClient(
        dc.parse_service_account(key_json()), session=session,
        sleep=sleeps.append, clock=clock, **kw,
    )
    return client, session, sleeps


def file_item(fid, name, mime="text/plain", **extra):
    item = {"id": fid, "name": name, "mimeType": mime, "modifiedTime": "2026-01-02T03:04:05.000Z"}
    item.update(extra)
    return item


def folder_item(fid, name):
    return file_item(fid, name, dc.FOLDER_MIME)


def make_pdf(text):
    """A minimal one-page PDF (no dependencies). Empty text gives a page without any text, like a scanned PDF."""
    content = ("BT /F1 12 Tf 72 720 Td (%s) Tj ET" % text) if text else ""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        "<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += ("%d 0 obj\n%s\nendobj\n" % (number, body)).encode("latin-1")
    xref = len(out)
    out += ("xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)).encode()
    for offset in offsets:
        out += ("%010d 00000 n \n" % offset).encode()
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)).encode()
    return out


def make_docx(paragraphs, doctype=""):
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % p for p in paragraphs)
    xml = '<?xml version="1.0"?>%s<w:document xmlns:w="%s"><w:body>%s</w:body></w:document>' % (
        doctype, ns, body)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def dfile(fid="FILEID_1234567890", name="a.txt", mime="text/plain", **kw):
    return dc.DriveFile(id=fid, name=name, mime_type=mime, modified_time="2026-01-01T00:00:00Z",
                        md5=kw.get("md5"), size=kw.get("size"), path=kw.get("path", ""),
                        web_view_link=None)


class ParseFolderIdTests(unittest.TestCase):
    def test_urls(self):
        fid = "1AbCdEfGhIjKlMnOpQrStUvWxYz_-12"
        for url in (
            "https://drive.google.com/drive/folders/%s" % fid,
            "https://drive.google.com/drive/folders/%s?usp=sharing" % fid,
            "https://drive.google.com/drive/u/0/folders/%s" % fid,
            "  https://drive.google.com/drive/u/2/folders/%s?usp=drive_link&resourcekey=x  " % fid,
            "drive.google.com/drive/folders/%s" % fid,
            "https://drive.google.com/open?id=%s" % fid,
            fid,
            " %s " % fid,
        ):
            self.assertEqual(dc.parse_folder_id(url), fid, url)

    def test_invalid(self):
        for bad in ("", "   ", None, 123, "abc", "https://example.com/", "hola mundo grande",
                    "https://drive.google.com/drive/folders/",
                    "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view",
                    "../../etc/passwd", "id'; drop"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                dc.parse_folder_id(bad)

    def test_file_link_message(self):
        with self.assertRaises(ValueError) as ctx:
            dc.parse_folder_id("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view")
        self.assertIn("archivo", str(ctx.exception))


class ParseServiceAccountTests(unittest.TestCase):
    def test_valid(self):
        info = dc.parse_service_account(key_json())
        self.assertEqual(info["client_email"], EMAIL)
        self.assertEqual(info["token_uri"], dc.TOKEN_URI)
        self.assertIn("BEGIN PRIVATE KEY", info["private_key"])
        self.assertEqual(dc.DriveClient(info, session=FakeSession()).client_email, EMAIL)

    def test_invalid_cases(self):
        cases = [
            "", "no es json", "[]", "null", None,
            key_json(type="authorized_user"),
            key_json(type=None),
            key_json(client_email=None),
            key_json(client_email="sin-arroba"),
            key_json(private_key=None),
            key_json(private_key="no es pem"),
            key_json(private_key="-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"),
            key_json(token_uri=None),
            key_json(token_uri="https://evil.example.com/token"),
            key_json(token_uri="http://oauth2.googleapis.com/token"),
        ]
        for raw in cases:
            with self.assertRaises(dc.DriveError, msg=str(raw)[:40]) as ctx:
                dc.parse_service_account(raw)
            self.assertEqual(ctx.exception.code, "invalid_key")

    def test_error_never_leaks_key(self):
        broken = key_json(private_key=PRIVATE_PEM[:-40] + "garbage")
        for raw in (broken, key_json(type="x"), key_json(token_uri="https://evil/x"),
                    key_json(client_email="bad")):
            with self.assertRaises(dc.DriveError) as ctx:
                dc.parse_service_account(raw)
            msg = str(ctx.exception)
            self.assertNotIn(KEY_BODY, msg)
            self.assertNotIn("PRIVATE KEY", msg)

    def test_bad_json_error_does_not_echo_content(self):
        with self.assertRaises(dc.DriveError) as ctx:
            dc.parse_service_account('{"private_key": "TOPSECRETVALUE" oops')
        self.assertNotIn("TOPSECRETVALUE", str(ctx.exception))

    def test_client_rejects_foreign_token_uri(self):
        info = dc.parse_service_account(key_json())
        info["token_uri"] = "https://evil.example.com/token"
        with self.assertRaises(dc.DriveError) as ctx:
            dc.DriveClient(info, session=FakeSession())
        self.assertEqual(ctx.exception.code, "invalid_key")

    def test_repr_hides_key(self):
        client, _, _ = make_client()
        self.assertNotIn(KEY_BODY, repr(client))


class AuthTests(unittest.TestCase):
    def folder_handler(self, call):
        if call["url"].endswith("/files/" + ROOT_ID):
            return FakeResponse(200, {"id": ROOT_ID, "name": "Docs", "mimeType": dc.FOLDER_MIME})

    def test_jwt_is_signed_and_verifiable(self):
        client, session, _ = make_client(self.folder_handler)
        client.folder_info(ROOT_ID)
        token_call = [c for c in session.calls if c["url"] == dc.TOKEN_URI][0]
        self.assertEqual(token_call["method"], "POST")
        self.assertEqual(token_call["data"]["grant_type"], "urn:ietf:params:oauth:grant-type:jwt-bearer")
        claims = jwt.decode(token_call["data"]["assertion"], PUBLIC_PEM, algorithms=["RS256"],
                            audience=dc.TOKEN_URI, options={"verify_exp": False, "verify_iat": False})
        self.assertEqual(claims["iss"], EMAIL)
        self.assertEqual(claims["scope"], "https://www.googleapis.com/auth/drive.readonly")
        self.assertEqual(claims["iat"], 1_000_000)
        self.assertEqual(claims["exp"] - claims["iat"], 3600)
        api = session.api_calls()[0]
        self.assertEqual(api["headers"]["Authorization"], "Bearer " + TOKEN_OK["access_token"])
        self.assertTrue(api["url"].startswith("https://www.googleapis.com/drive/v3/files/"))

    def test_token_cached_and_refreshed(self):
        clock = Clock()
        client, session, _ = make_client(self.folder_handler, clock=clock)
        client.folder_info(ROOT_ID)
        client.folder_info(ROOT_ID)
        self.assertEqual(session.token_calls, 1)
        clock.now += 3600 - 61
        client.folder_info(ROOT_ID)
        self.assertEqual(session.token_calls, 1)
        clock.now += 2  # now inside the 60 s safety window
        client.folder_info(ROOT_ID)
        self.assertEqual(session.token_calls, 2)

    def test_token_rejected_by_google(self):
        client, _, _ = make_client(
            token_handler=lambda c: FakeResponse(400, {"error": "invalid_grant", "error_description": "Invalid JWT"}))
        with self.assertRaises(dc.DriveError) as ctx:
            client.folder_info(ROOT_ID)
        self.assertEqual(ctx.exception.code, "auth_failed")
        self.assertIn("hora", str(ctx.exception))

    def test_401_on_api_refreshes_once_then_fails(self):
        client, session, _ = make_client(lambda c: FakeResponse(401, {"error": {}}))
        with self.assertRaises(dc.DriveError) as ctx:
            client.folder_info(ROOT_ID)
        self.assertEqual(ctx.exception.code, "auth_failed")
        self.assertEqual(session.token_calls, 2)

    def test_only_official_token_endpoint_is_called(self):
        client, session, _ = make_client(self.folder_handler)
        client.folder_info(ROOT_ID)
        token_urls = {c["url"] for c in session.calls if "oauth" in c["url"] or "token" in c["url"]}
        self.assertEqual(token_urls, {dc.TOKEN_URI})


class FolderInfoTests(unittest.TestCase):
    def test_ok_with_shared_drives(self):
        def handler(call):
            return FakeResponse(200, {"id": ROOT_ID, "name": "Políticas", "mimeType": dc.FOLDER_MIME})
        client, session, _ = make_client(handler)
        self.assertEqual(client.folder_info(ROOT_ID), {"id": ROOT_ID, "name": "Políticas"})
        self.assertEqual(session.api_calls()[0]["params"]["supportsAllDrives"], "true")

    def test_not_a_folder(self):
        client, _, _ = make_client(lambda c: FakeResponse(200, {"id": "X", "name": "f", "mimeType": "text/plain"}))
        with self.assertRaises(dc.DriveError) as ctx:
            client.folder_info(ROOT_ID)
        self.assertEqual(ctx.exception.code, "not_found")

    def test_invalid_id_never_hits_network(self):
        client, session, _ = make_client()
        for bad in ("a/b", "x' or '1'='1", "", "../x", None):
            with self.assertRaises(dc.DriveError):
                client.folder_info(bad)
        self.assertEqual(session.calls, [])


class ListFolderTests(unittest.TestCase):
    def tree_handler(self, pages=None):
        pages = pages or {}

        def handler(call):
            if "/files" not in call["url"] or call["url"].endswith("/files") is False:
                return None
            q = call["params"]["q"]
            token = call["params"].get("pageToken")
            return FakeResponse(200, pages.get((q, token), {"files": []}))
        return handler

    def q(self, fid):
        return "'%s' in parents and trashed = false" % fid

    def test_recursive_paginated_paths_dedupe_shortcuts(self):
        pages = {
            (self.q(ROOT_ID), None): {
                "nextPageToken": "P2",
                "files": [file_item("F_ROOT_0001", "raiz.txt", size="12", md5Checksum="abc",
                                    webViewLink="https://x/1"),
                          folder_item("FOLD_RRHH_01", "RRHH"),
                          file_item("SC_000000001", "atajo", dc.SHORTCUT_MIME)],
            },
            (self.q(ROOT_ID), "P2"): {
                "files": [file_item("F_ROOT_0001", "raiz.txt"),  # duplicate
                          folder_item("FOLD_LOOP_01", "Ventas/Q1")],
            },
            (self.q("FOLD_RRHH_01"), None): {
                "files": [folder_item("FOLD_POL_001", "Políticas"),
                          file_item("F_RRHH_0001", "vacaciones.pdf", dc.PDF_MIME)],
            },
            (self.q("FOLD_POL_001"), None): {
                "files": [file_item("F_POL_00001", "codigo.docx", dc.DOCX_MIME),
                          folder_item(ROOT_ID, "ciclo")],  # cycle back to root
            },
            (self.q("FOLD_LOOP_01"), None): {"files": [file_item("F_Q1_000001", "plan.txt")]},
        }
        client, session, _ = make_client(self.tree_handler(pages))
        files = client.list_folder(ROOT_ID)
        by_id = {f.id: f for f in files}
        self.assertEqual(len(files), len(by_id))
        self.assertEqual(set(by_id), {"F_ROOT_0001", "F_RRHH_0001", "F_POL_00001", "F_Q1_000001"})
        self.assertEqual(by_id["F_ROOT_0001"].path, "")
        self.assertEqual(by_id["F_ROOT_0001"].size, 12)
        self.assertEqual(by_id["F_ROOT_0001"].md5, "abc")
        self.assertEqual(by_id["F_ROOT_0001"].web_view_link, "https://x/1")
        self.assertEqual(by_id["F_RRHH_0001"].path, "RRHH")
        self.assertEqual(by_id["F_POL_00001"].path, "RRHH/Políticas")
        self.assertEqual(by_id["F_Q1_000001"].path, "Ventas-Q1")
        self.assertFalse(client.truncated)
        for call in session.api_calls():
            self.assertEqual(call["params"]["supportsAllDrives"], "true")
            self.assertEqual(call["params"]["includeItemsFromAllDrives"], "true")
            self.assertIn("trashed = false", call["params"]["q"])
        with self.assertRaises(Exception):
            by_id["F_ROOT_0001"].name = "x"  # frozen

    def test_max_files(self):
        items = [file_item("FILE_%07d" % i, "f%d.txt" % i) for i in range(10)]
        client, _, _ = make_client(self.tree_handler({(self.q(ROOT_ID), None): {"files": items}}), max_files=4)
        files = client.list_folder(ROOT_ID)
        self.assertEqual(len(files), 4)
        self.assertTrue(client.truncated)

    def test_max_depth(self):
        pages = {
            (self.q(ROOT_ID), None): {"files": [folder_item("FOLD_LVL_001", "a")]},
            (self.q("FOLD_LVL_001"), None): {"files": [folder_item("FOLD_LVL_002", "b"),
                                                        file_item("F_LVL1_0001", "x.txt")]},
            (self.q("FOLD_LVL_002"), None): {"files": [file_item("F_LVL2_0001", "y.txt")]},
        }
        client, _, _ = make_client(self.tree_handler(pages), max_depth=1)
        self.assertEqual([f.id for f in client.list_folder(ROOT_ID)], ["F_LVL1_0001"])

    def test_quote_escaping_in_query(self):
        client, session, _ = make_client(self.tree_handler())
        client.list_folder(ROOT_ID)
        self.assertEqual(session.api_calls()[0]["params"]["q"], self.q(ROOT_ID))
        with self.assertRaises(dc.DriveError):
            client.list_folder("x' in parents or 'a")

    def test_unsafe_ids_in_listing_are_skipped(self):
        items = [file_item("../evil", "e.txt"), file_item("GOOD_ID_0001", "ok.txt")]
        client, _, _ = make_client(self.tree_handler({(self.q(ROOT_ID), None): {"files": items}}))
        self.assertEqual([f.id for f in client.list_folder(ROOT_ID)], ["GOOD_ID_0001"])


class FetchTextTests(unittest.TestCase):
    def fetch(self, f, content, status=200):
        client, session, _ = make_client(lambda c: FakeResponse(status, None, content=content))
        return client.fetch_text(f), session

    def test_google_doc_slides_sheet_exports(self):
        for mime, expected in ((dc.GDOC_MIME, "text/plain"), (dc.GSLIDES_MIME, "text/plain"),
                               (dc.GSHEET_MIME, "text/csv")):
            text, session = self.fetch(dfile(mime=mime, name="n"), "﻿hola ñandú".encode("utf-8"))
            self.assertEqual(text, "hola ñandú")
            call = session.api_calls()[0]
            self.assertTrue(call["url"].endswith("/files/FILEID_1234567890/export"))
            self.assertEqual(call["params"]["mimeType"], expected)

    def test_pdf(self):
        text, session = self.fetch(dfile(name="a.pdf", mime=dc.PDF_MIME), make_pdf("Hello vacation policy"))
        self.assertIn("Hello vacation policy", text)
        self.assertEqual(session.api_calls()[0]["params"]["alt"], "media")

    def test_scanned_pdf_returns_none(self):
        text, _ = self.fetch(dfile(name="a.pdf", mime=dc.PDF_MIME), make_pdf(""))
        self.assertIsNone(text)

    def test_corrupt_pdf_returns_none(self):
        text, _ = self.fetch(dfile(name="a.pdf", mime=dc.PDF_MIME), b"not a pdf")
        self.assertIsNone(text)

    def test_docx(self):
        data = make_docx(["Primera línea", "Segunda &amp; última"])
        text, _ = self.fetch(dfile(name="x.docx", mime=dc.DOCX_MIME), data)
        self.assertEqual(text, "Primera línea\nSegunda & última")

    def test_docx_by_extension_and_invalid(self):
        text, _ = self.fetch(dfile(name="x.DOCX", mime="application/octet-stream"), make_docx(["ok"]))
        self.assertEqual(text, "ok")
        text, _ = self.fetch(dfile(name="x.docx", mime=dc.DOCX_MIME), b"not a zip")
        self.assertIsNone(text)

    def test_docx_with_entities_rejected(self):
        evil = make_docx(["x"], doctype='<!DOCTYPE d [<!ENTITY a "aaaa">]>')
        text, _ = self.fetch(dfile(name="x.docx", mime=dc.DOCX_MIME), evil)
        self.assertIsNone(text)

    @staticmethod
    def raw_docx(xml_bytes):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("word/document.xml", xml_bytes)
        return buf.getvalue()

    def test_any_doctype_is_refused_whatever_the_encoding(self):
        """The byte search for «<!DOCTYPE» cannot see one written in UTF-16 (every character takes two bytes): the XML
        parser's own doctype handler can."""
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        body = '<w:document xmlns:w="%s"><w:body><w:p><w:r><w:t>hola</w:t></w:r></w:p></w:body></w:document>' % ns
        for encoding in ("utf-16", "utf-16-le", "utf-16-be"):
            for doctype in ('<!DOCTYPE d [<!ENTITY a "aaaa">]>', "<!doctype d>", "<!DOCTYPE d>"):
                xml = ('<?xml version="1.0" encoding="%s"?>%s%s' % (encoding.upper(), doctype, body)).encode(encoding)
                self.assertNotIn(b"<!DOCTYPE", xml.upper(), "the point: the plain byte search would not have caught it")
                text, _ = self.fetch(dfile(name="x.docx", mime=dc.DOCX_MIME), self.raw_docx(xml))
                self.assertIsNone(text, (encoding, doctype))

    def test_a_clean_utf16_docx_is_still_read(self):
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xml = ('<?xml version="1.0" encoding="UTF-16"?><w:document xmlns:w="%s"><w:body><w:p><w:r><w:t>Árbol</w:t></w:r></w:p></w:body></w:document>' % ns).encode("utf-16")
        text, _ = self.fetch(dfile(name="x.docx", mime=dc.DOCX_MIME), self.raw_docx(xml))
        self.assertEqual(text, "Árbol")

    def test_a_nul_byte_in_an_8_bit_docx_is_refused(self):
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xml = ('<?xml version="1.0"?><w:document xmlns:w="%s"><w:body><w:p><w:r><w:t>a\x00b</w:t></w:r></w:p></w:body></w:document>' % ns).encode()
        text, _ = self.fetch(dfile(name="x.docx", mime=dc.DOCX_MIME), self.raw_docx(xml))
        self.assertIsNone(text)

    def test_plain_text_utf8_and_latin1(self):
        text, _ = self.fetch(dfile(name="a.txt"), "áéí".encode("utf-8"))
        self.assertEqual(text, "áéí")
        text, _ = self.fetch(dfile(name="a.md", mime="text/markdown"), "áéí".encode("latin-1"))
        self.assertEqual(text, "áéí")
        text, _ = self.fetch(dfile(name="a.csv", mime="text/csv"), b"a,b\n1,2")
        self.assertEqual(text, "a,b\n1,2")

    def test_html(self):
        raw = ("<html><head><title>T</title><style>p{}</style></head><body><h1>Título</h1>"
               "<p>Uno &amp; dos</p><script>alert(1)</script><p>Tres</p></body></html>").encode()
        text, _ = self.fetch(dfile(name="a.html", mime="text/html"), raw)
        self.assertEqual(text, "Título\n\nUno & dos\n\nTres")
        self.assertNotIn("alert", text)

    def test_html_keeps_text_that_only_looks_like_markup(self):
        raw = "<!DOCTYPE html><?xml version='1.0'?><p>1 &lt; 2 y <3 > 1</p><!-- nota --><p>Fin <b>negrita</b></p>".encode()
        text, _ = self.fetch(dfile(name="a.htm", mime="text/html"), raw)
        self.assertEqual(text, "1 < 2 y <3 > 1\n\nFin negrita")

    def test_html_unclosed_script_or_head_hides_the_rest_and_comments_swallow_it(self):
        self.assertEqual(dc._html_to_text("<p>antes</p><script>var x = 1;"), "antes")
        self.assertEqual(dc._html_to_text("<p>antes</p><!-- sin cerrar <p>después</p>"), "antes")
        self.assertEqual(dc._html_to_text("a<br/>b<script/>c"), "a\nbc", "a self-closed script hides nothing")

    def test_html_hostile_input_is_linear(self):
        # html.parser needs 14 seconds for 40,000 unclosed comments; this reader must not depend on how nasty the file is.
        for unit in ("<!--", "<!--x>", "<a ", "<![CDATA[", "<", "&", "&#", "<!", "<?", "<a b='", "<script>x", "</", "<>", "<style>"):
            text = unit * (1_000_000 // len(unit))
            started = time.perf_counter()
            dc._html_to_text(text)
            self.assertLess(time.perf_counter() - started, 2.0, unit)
        for text in ("<a " + "x" * 1_000_000, "<" + "x" * 1_000_000, "<script>" + "<b>" * 300_000):
            started = time.perf_counter()
            dc._html_to_text(text)
            self.assertLess(time.perf_counter() - started, 2.0, text[:12])

    def test_docx_xml_limit_is_20_mb(self):
        self.assertEqual(dc.MAX_DOCX_XML_BYTES, 20 * 1024 * 1024)
        xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>%s</w:body></w:document>'
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", xml % ("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % ("x" * dc.MAX_DOCX_XML_BYTES)))
        big = buffer.getvalue()
        self.assertLess(len(big), 1_000_000, "it compresses: only the unpacked size is the problem")
        client, _, _ = make_client(lambda c: FakeResponse(200, None, content=big))
        with self.assertRaises(dc.DriveError) as caught:
            client.fetch_text(dfile(name="x.docx", mime=dc.DOCX_MIME))
        self.assertEqual(caught.exception.code, "too_large")
        text, _ = self.fetch(dfile(name="x.docx", mime=dc.DOCX_MIME), make_docx(["x" * 1_000_000]))
        self.assertEqual(len(text), 1_000_000, "a large but sane document is still read")

    def fake_pdf(self, pages):
        class Page:
            def __init__(self, text):
                self.text = text

            def extract_text(self):
                return self.text

        class Reader:
            is_encrypted = False

            def __init__(self, stream):
                self.pages = [Page(text) for text in pages]

        return Reader

    def test_pdf_is_read_up_to_500_pages_and_the_cut_is_reported(self):
        self.assertEqual(dc.MAX_PDF_PAGES, 500)
        client, _, _ = make_client(lambda c: FakeResponse(200, None, content=b"%PDF-1.4"))
        with patch("pypdf.PdfReader", self.fake_pdf(["página %d" % n for n in range(600)])):
            text = client.fetch_text(dfile(name="grande.pdf", mime=dc.PDF_MIME))
        self.assertEqual(text.count("página"), 500)
        self.assertIn("página 499", text)
        self.assertNotIn("página 500", text)
        self.assertTrue(client.last_fetch_truncated)
        with patch("pypdf.PdfReader", self.fake_pdf(["página %d" % n for n in range(500)])):
            text = client.fetch_text(dfile(name="justo.pdf", mime=dc.PDF_MIME))
        self.assertEqual(text.count("página"), 500)
        self.assertFalse(client.last_fetch_truncated, "exactly 500 pages is complete")

    def test_pdf_extraction_has_a_time_budget(self):
        client, _, _ = make_client(lambda c: FakeResponse(200, None, content=b"%PDF-1.4"))
        ticks = iter(range(0, 10_000, 40))  # every look at the clock is 40 s later
        with patch("pypdf.PdfReader", self.fake_pdf(["p%d" % n for n in range(50)])), \
                patch.object(dc.time, "monotonic", lambda: next(ticks)):
            text = client.fetch_text(dfile(name="lento.pdf", mime=dc.PDF_MIME))
        self.assertEqual(text, "p0")
        self.assertTrue(client.last_fetch_truncated)

    def test_truncation_flag_resets_on_every_fetch_and_covers_the_character_cap(self):
        client, _, _ = make_client(lambda c: FakeResponse(200, None, content=("a" * (dc.MAX_OUTPUT_CHARS + 5)).encode()))
        self.assertEqual(len(client.fetch_text(dfile(name="enorme.txt"))), dc.MAX_OUTPUT_CHARS)
        self.assertTrue(client.last_fetch_truncated)
        client2, _, _ = make_client(lambda c: FakeResponse(200, None, content=b"corto"))
        client2.last_fetch_truncated = True
        self.assertEqual(client2.fetch_text(dfile(name="c.txt")), "corto")
        self.assertFalse(client2.last_fetch_truncated)

    def test_unsupported_returns_none_without_network(self):
        client, session, _ = make_client()
        for mime, name in (("image/png", "a.png"), ("application/zip", "a.zip"),
                           ("application/vnd.google-apps.form", "form"),
                           ("application/vnd.google-apps.drawing", "d")):
            self.assertIsNone(client.fetch_text(dfile(name=name, mime=mime)))
        self.assertEqual(session.calls, [])

    def test_declared_size_limit(self):
        client, session, _ = make_client()
        with self.assertRaises(dc.DriveError) as ctx:
            client.fetch_text(dfile(name="big.pdf", mime=dc.PDF_MIME, size=dc.MAX_DOWNLOAD_BYTES + 1))
        self.assertEqual(ctx.exception.code, "too_large")
        self.assertEqual(session.calls, [])

    def test_streamed_size_limit(self):
        big = b"a" * (dc.MAX_DOWNLOAD_BYTES + 1)
        client, _, _ = make_client(lambda c: FakeResponse(200, None, content=big))
        with self.assertRaises(dc.DriveError) as ctx:
            client.fetch_text(dfile(name="a.txt"))
        self.assertEqual(ctx.exception.code, "too_large")

    def test_content_length_header_limit(self):
        hdr = {"Content-Length": str(dc.MAX_DOWNLOAD_BYTES + 5)}
        client, _, _ = make_client(lambda c: FakeResponse(200, None, content=b"x", headers=hdr))
        with self.assertRaises(dc.DriveError) as ctx:
            client.fetch_text(dfile(name="a.txt"))
        self.assertEqual(ctx.exception.code, "too_large")

    def test_output_char_limit(self):
        data = b"a" * (dc.MAX_OUTPUT_CHARS + 5000)
        text, _ = self.fetch(dfile(name="a.txt"), data)
        self.assertEqual(len(text), dc.MAX_OUTPUT_CHARS)

    def test_export_size_limit_error(self):
        client, _, _ = make_client(lambda c: api_error(403, "exportSizeLimitExceeded"))
        with self.assertRaises(dc.DriveError) as ctx:
            client.fetch_text(dfile(mime=dc.GDOC_MIME, name="n"))
        self.assertEqual(ctx.exception.code, "too_large")

    def test_file_name_never_used_in_url(self):
        client, session, _ = make_client(lambda c: FakeResponse(200, None, content=b"x"))
        client.fetch_text(dfile(name="../../etc/passwd.txt"))
        self.assertNotIn("passwd", session.api_calls()[0]["url"])

    def test_invalid_file_id(self):
        client, session, _ = make_client()
        with self.assertRaises(dc.DriveError):
            client.fetch_text(dfile(fid="../x"))
        self.assertEqual(session.calls, [])


class ErrorMappingTests(unittest.TestCase):
    def code_for(self, response, method="folder_info"):
        client, _, _ = make_client(lambda c: response)
        with self.assertRaises(dc.DriveError) as ctx:
            getattr(client, method)(ROOT_ID)
        return ctx.exception

    def test_mapping(self):
        err = self.code_for(api_error(404))
        self.assertEqual(err.code, "not_found")
        self.assertIn(EMAIL, str(err))
        self.assertEqual(self.code_for(api_error(403, "forbidden")).code, "forbidden")
        self.assertIn(EMAIL, str(self.code_for(api_error(403))))
        for reason in ("accessNotConfigured", "SERVICE_DISABLED"):
            err = self.code_for(api_error(403, reason))
            self.assertEqual(err.code, "api_disabled")
            self.assertIn("https://console.cloud.google.com/apis/library/drive.googleapis.com", str(err))
        details = FakeResponse(403, {"error": {"details": [{"reason": "SERVICE_DISABLED"}]}})
        self.assertEqual(self.code_for(details).code, "api_disabled")
        self.assertEqual(self.code_for(api_error(429)).code, "rate_limited")
        self.assertEqual(self.code_for(api_error(403, "userRateLimitExceeded")).code, "rate_limited")
        self.assertEqual(self.code_for(api_error(503)).code, "network")
        self.assertEqual(self.code_for(api_error(404), "list_folder").code, "not_found")

    def test_network_error(self):
        client, session, sleeps = make_client(lambda c: ConnectionError("boom"))
        with self.assertRaises(dc.DriveError) as ctx:
            client.folder_info(ROOT_ID)
        self.assertEqual(ctx.exception.code, "network")
        self.assertEqual(len(sleeps), dc.MAX_RETRIES)

    def test_network_error_then_ok(self):
        state = {"n": 0}

        def handler(call):
            state["n"] += 1
            if state["n"] == 1:
                return TimeoutError("slow")
            return FakeResponse(200, {"id": ROOT_ID, "name": "D", "mimeType": dc.FOLDER_MIME})
        client, _, sleeps = make_client(handler)
        self.assertEqual(client.folder_info(ROOT_ID)["name"], "D")
        self.assertEqual(len(sleeps), 1)

    def test_retry_429_then_ok(self):
        state = {"n": 0}

        def handler(call):
            state["n"] += 1
            if state["n"] <= 2:
                return FakeResponse(429, {"error": {}}, headers={"Retry-After": "3"})
            return FakeResponse(200, {"id": ROOT_ID, "name": "D", "mimeType": dc.FOLDER_MIME})
        client, session, sleeps = make_client(handler)
        self.assertEqual(client.folder_info(ROOT_ID)["id"], ROOT_ID)
        self.assertEqual(len(session.api_calls()), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(s >= 3 for s in sleeps))

    def test_retry_5xx_then_ok_and_bounded(self):
        state = {"n": 0}

        def handler(call):
            state["n"] += 1
            return FakeResponse(500 if state["n"] == 1 else 200,
                                {"id": ROOT_ID, "name": "D", "mimeType": dc.FOLDER_MIME})
        client, _, sleeps = make_client(handler)
        client.folder_info(ROOT_ID)
        self.assertEqual(len(sleeps), 1)
        client, session, sleeps = make_client(lambda c: api_error(429))
        with self.assertRaises(dc.DriveError) as ctx:
            client.folder_info(ROOT_ID)
        self.assertEqual(ctx.exception.code, "rate_limited")
        self.assertEqual(len(session.api_calls()), dc.MAX_RETRIES + 1)
        self.assertEqual(len(sleeps), dc.MAX_RETRIES)

    def test_token_endpoint_429_and_5xx(self):
        client, _, _ = make_client(token_handler=lambda c: api_error(429))
        with self.assertRaises(dc.DriveError) as ctx:
            client.folder_info(ROOT_ID)
        self.assertEqual(ctx.exception.code, "rate_limited")
        client, _, _ = make_client(token_handler=lambda c: api_error(503))
        with self.assertRaises(dc.DriveError) as ctx:
            client.folder_info(ROOT_ID)
        self.assertEqual(ctx.exception.code, "network")

    def test_no_message_contains_key_or_token(self):
        responses = [api_error(401), api_error(403), api_error(404), api_error(429), api_error(500),
                     api_error(403, "accessNotConfigured"), api_error(403, "exportSizeLimitExceeded"),
                     ConnectionError("x")]
        for resp in responses:
            client, _, _ = make_client(lambda c, r=resp: r)
            for method in ("folder_info", "list_folder"):
                try:
                    getattr(client, method)(ROOT_ID)
                except dc.DriveError as exc:
                    msg = str(exc)
                    self.assertIn(exc.code, dc.DriveError.CODES)
                    self.assertNotIn(KEY_BODY, msg)
                    self.assertNotIn("PRIVATE KEY", msg)
                    self.assertNotIn(TOKEN_OK["access_token"], msg)
                    self.assertNotIn("ya29", msg)
        client, _, _ = make_client(lambda c: FakeResponse(403, {"error": {}}))
        try:
            client.fetch_text(dfile(mime=dc.GDOC_MIME, name="n"))
        except dc.DriveError as exc:
            self.assertNotIn(TOKEN_OK["access_token"], str(exc))

    def test_logs_do_not_contain_secrets(self):
        with self.assertLogs(level="DEBUG") as logs:
            dc._logger.warning("sentinel")
            client, _, _ = make_client(lambda c: ConnectionError("x"))
            with self.assertRaises(dc.DriveError):
                client.folder_info(ROOT_ID)
        joined = "\n".join(logs.output)
        self.assertNotIn(KEY_BODY, joined)
        self.assertNotIn(TOKEN_OK["access_token"], joined)


class RequestsDefaultSessionTests(unittest.TestCase):
    def test_default_session_is_lazy(self):
        info = dc.parse_service_account(key_json())
        client = dc.DriveClient(info)  # must not touch the network or require requests here
        self.assertEqual(client.client_email, EMAIL)


if __name__ == "__main__":
    unittest.main()
