"""Minimal Google Drive (read-only) client for Buddy IA.

Pure Python module: it must NOT import odoo, so it can be tested standalone.

Design notes
- Authentication is the OAuth2 JWT-bearer flow of a Google service account,
  implemented with PyJWT (RS256). No google-auth dependency.
- Only the official token endpoint is accepted (SSRF defense); the Drive API
  base URL is fixed.
- Neither the private key nor access tokens are ever logged or placed in
  exception messages. File names coming from Drive are untrusted data and are
  never used as filesystem paths.
- User-facing error messages are in Argentine Spanish and meant for a
  non-technical admin.
"""
import html
import io
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from xml.parsers import expat
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

_logger = logging.getLogger(__name__)

TOKEN_URI = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/drive/v3"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
API_LIBRARY_URL = "https://console.cloud.google.com/apis/library/drive.googleapis.com"

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_CHARS = 1_000_000
MAX_DOCX_XML_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_PDF_SECONDS = 60  # extraction budget of one PDF: a page that takes forever must not take the worker with it
MAX_RETRIES = 3
TOKEN_SKEW_SECONDS = 60

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GDOC_MIME = "application/vnd.google-apps.document"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
GSLIDES_MIME = "application/vnd.google-apps.presentation"
PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_MIMES = {"text/plain", "text/markdown", "text/x-markdown", "text/csv"}
HTML_MIMES = {"text/html", "application/xhtml+xml"}
TEXT_EXTENSIONS = (".txt", ".md", ".csv")
HTML_EXTENSIONS = (".html", ".htm")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FOLDER_URL_RE = re.compile(r"/folders/([A-Za-z0-9_-]+)")
_FILE_URL_RE = re.compile(r"/(?:file|document|spreadsheets|presentation)/d/")
_MIN_ID_LEN = 10

_RATE_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "sharingratelimitexceeded"}
_DISABLED_REASONS = {"accessnotconfigured", "service_disabled"}


class DriveError(Exception):
    """Drive failure. ``code`` is machine readable, ``str(exc)`` is for the admin."""

    CODES = (
        "invalid_key", "auth_failed", "not_found", "forbidden", "rate_limited",
        "network", "too_large", "unsupported", "api_disabled",
    )

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: str
    md5: "str | None" = None
    size: "int | None" = None
    path: str = ""
    web_view_link: "str | None" = None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_folder_id(url_or_id):
    """Return the Drive folder ID from a folder URL or a bare ID."""
    bad = ValueError(
        "No pude sacar el ID de la carpeta. Pegá el link de la carpeta de Google Drive "
        "(se ve como https://drive.google.com/drive/folders/...) o directamente su ID."
    )
    if not isinstance(url_or_id, str):
        raise bad
    value = url_or_id.strip()
    if not value:
        raise bad
    if "/" in value or "://" in value or "?" in value:
        if _FILE_URL_RE.search(value):
            raise ValueError(
                "Ese link es de un archivo, no de una carpeta. "
                "Abrí la carpeta en Google Drive y copiá el link desde ahí."
            )
        match = _FOLDER_URL_RE.search(urlparse(value).path or value)
        candidate = match.group(1) if match else None
        if candidate is None:
            query_ids = parse_qs(urlparse(value).query).get("id")
            candidate = query_ids[0] if query_ids else None
        if candidate and _ID_RE.match(candidate) and len(candidate) >= _MIN_ID_LEN:
            return candidate
        raise bad
    if _ID_RE.match(value) and len(value) >= _MIN_ID_LEN:
        return value
    raise bad


def parse_service_account(raw_json):
    """Validate the service account key JSON and return the relevant fields.

    Error messages never contain any part of the key.
    """
    invalid = "La clave JSON no es válida."
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        raise DriveError(
            "invalid_key",
            invalid + " Tiene que ser el contenido completo del archivo .json que descargaste "
            "de Google Cloud (empieza con { y termina con }).",
        ) from None
    if not isinstance(data, dict):
        raise DriveError("invalid_key", invalid + " Pegá el contenido completo del archivo .json.")
    if data.get("type") != "service_account":
        raise DriveError(
            "invalid_key",
            invalid + " No es una clave de cuenta de servicio (falta type: service_account). "
            "Creá una clave de tipo JSON desde Google Cloud > IAM > Cuentas de servicio.",
        )
    email = data.get("client_email")
    if not isinstance(email, str) or "@" not in email or any(c.isspace() for c in email):
        raise DriveError("invalid_key", invalid + " Falta el campo client_email o está mal.")
    key = data.get("private_key")
    if not isinstance(key, str) or "-----BEGIN" not in key or "PRIVATE KEY-----" not in key:
        raise DriveError("invalid_key", invalid + " Falta el campo private_key o está mal.")
    token_uri = data.get("token_uri")
    if token_uri != TOKEN_URI:
        raise DriveError(
            "invalid_key",
            invalid + " El campo token_uri no es el oficial de Google (%s)." % TOKEN_URI,
        )
    try:
        from cryptography.hazmat.primitives import serialization
        serialization.load_pem_private_key(key.encode("utf-8"), password=None)
    except Exception:  # noqa: BLE001 - never leak details about the key
        raise DriveError(
            "invalid_key",
            invalid + " La private_key no se pudo leer. Descargá una clave nueva y pegala "
            "completa, sin modificar nada.",
        ) from None
    result = {
        "type": "service_account",
        "client_email": email,
        "private_key": key,
        "token_uri": token_uri,
    }
    if isinstance(data.get("project_id"), str):
        result["project_id"] = data["project_id"]
    return result


def _check_id(value, what="ID"):
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise DriveError("not_found", "El %s de Drive no es válido." % what)
    return value


def _error_reasons(resp):
    """Collect lowercase 'reason' strings from a Google error body (best effort)."""
    reasons = set()
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return reasons
    if not isinstance(body, dict):
        return reasons
    err = body.get("error")
    if isinstance(err, dict):
        for item in err.get("errors") or []:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reasons.add(item["reason"].lower())
        for item in err.get("details") or []:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reasons.add(item["reason"].lower())
        if isinstance(err.get("status"), str):
            reasons.add(err["status"].lower())
    elif isinstance(err, str):
        reasons.add(err.lower())
    return reasons


# ---------------------------------------------------------------------------
# Text extraction helpers
# ---------------------------------------------------------------------------

# html.parser from the standard library is quadratic on malformed input (40,000 unclosed "<!--" take 14 seconds), and
# a Drive file is untrusted. This reader is linear: comments are removed with plain searches, tags are only what fits
# between "<" and ">" in a short, bounded stretch, and everything else is text.
_HTML_SKIP_TAGS = frozenset(("script", "style", "head", "template"))
_HTML_BLOCK_TAGS = frozenset(("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
                              "section", "article", "table", "ul", "ol", "pre", "blockquote", "hr"))
MAX_HTML_TAG_CHARS = 2000
_HTML_MARKUP = re.compile("<([^<>]{1,%d})>" % MAX_HTML_TAG_CHARS)
_HTML_TAG_NAME = re.compile("(/?)([A-Za-z][A-Za-z0-9]{0,30})")


def _drop_html_comments(raw):
    """The text without `<!-- ... -->`; an unclosed comment swallows the rest, as it does in a browser."""
    parts, position = [], 0
    while True:
        start = raw.find("<!--", position)
        if start < 0:
            parts.append(raw[position:])
            return "".join(parts)
        parts.append(raw[position:start])
        end = raw.find("-->", start + 4)
        if end < 0:
            return "".join(parts)
        position = end + 3


def _html_to_text(raw):
    raw = _drop_html_comments(raw)
    parts, skipping, position = [], 0, 0
    for match in _HTML_MARKUP.finditer(raw):
        inner = match.group(1)
        named = _HTML_TAG_NAME.match(inner)
        if not named and inner[0] not in "!?":
            continue  # not markup ("1 <3 2 > 0"): it stays text
        if not skipping:
            parts.append(html.unescape(raw[position:match.start()]))
        position = match.end()
        if not named:
            continue  # "<!DOCTYPE ...>", "<?xml ...?>": no text
        closing, name = named.group(1), named.group(2).lower()
        if name in _HTML_SKIP_TAGS:
            if closing:
                skipping = max(0, skipping - 1)
            elif not match.group(1).endswith("/"):
                skipping += 1
        elif name in _HTML_BLOCK_TAGS:
            parts.append("\n")
    if not skipping:
        parts.append(html.unescape(raw[position:]))
    text = "".join(parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _decode(data):
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_to_text(data):
    """Extract paragraph text from a .docx; returns None if it is not readable."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise DriveError(
                    "too_large",
                    "El documento Word es demasiado grande para procesarlo.",
                )
            with archive.open(info) as handle:
                xml_bytes = handle.read(MAX_DOCX_XML_BYTES + 1)
    except DriveError:
        raise
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError, ValueError):
        return None
    if len(xml_bytes) > MAX_DOCX_XML_BYTES:
        raise DriveError("too_large", "El documento Word es demasiado grande para procesarlo.")
    head = xml_bytes[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in xml_bytes[:65536].upper():
        return None  # no DTDs/entities in a legitimate docx
    if b"\x00" in xml_bytes and not xml_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None  # a NUL byte in an 8-bit document: not XML (UTF-16 legitimately has them, and is checked below)
    scanner = expat.ParserCreate()

    def refuse_doctype(*unused):
        # The byte sniffing above cannot see a DOCTYPE written in UTF-16 (or hidden by an encoding declaration): the
        # XML parser itself can, whatever the encoding. A legitimate document.xml never has one.
        raise ValueError("DOCTYPE no permitido")

    scanner.StartDoctypeDeclHandler = refuse_doctype
    try:
        scanner.Parse(xml_bytes, True)
        root = ET.fromstring(xml_bytes)
    except (expat.ExpatError, ValueError, ET.ParseError):
        return None
    lines = []
    for para in root.iter(_W_NS + "p"):
        chunks = []
        for node in para.iter():
            if node.tag == _W_NS + "t":
                chunks.append(node.text or "")
            elif node.tag == _W_NS + "tab":
                chunks.append("\t")
            elif node.tag in (_W_NS + "br", _W_NS + "cr"):
                chunks.append("\n")
        lines.append("".join(chunks))
    return "\n".join(lines).strip()


def _pdf_to_text(data, info=None):
    """Extract text from a PDF with pypdf (BSD); None if scanned, encrypted or unreadable.

    Only the first MAX_PDF_PAGES pages are read, and no more than MAX_PDF_SECONDS are spent; when either limit cuts the
    text, `info["truncated"]` is set so the caller can tell the administrator.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        raise DriveError(
            "unsupported",
            "Falta la librería pypdf en el servidor, así que no puedo leer PDFs. "
            "Avisale al administrador del sistema.",
        ) from None
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return None
        parts = []
        total = 0
        started = time.monotonic()
        for number, page in enumerate(reader.pages):
            if number >= MAX_PDF_PAGES or time.monotonic() - started > MAX_PDF_SECONDS:
                if info is not None:
                    info["truncated"] = True
                break
            text = page.extract_text() or ""
            parts.append(text)
            total += len(text)
            if total > MAX_OUTPUT_CHARS:
                break
    except Exception:  # noqa: BLE001
        return None
    text = "\n".join(parts).strip()
    return text or None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class DriveClient:
    def __init__(self, service_account_info, session=None, timeout=20, max_files=2000,
                 max_depth=8, sleep=None, clock=None):
        if not isinstance(service_account_info, dict):
            raise DriveError("invalid_key", "La clave JSON no es válida.")
        email = service_account_info.get("client_email")
        key = service_account_info.get("private_key")
        if not isinstance(email, str) or not isinstance(key, str):
            raise DriveError("invalid_key", "La clave JSON no es válida: faltan datos.")
        token_uri = service_account_info.get("token_uri", TOKEN_URI)
        if token_uri != TOKEN_URI:
            raise DriveError(
                "invalid_key",
                "La clave JSON tiene un token_uri que no es el oficial de Google; "
                "por seguridad no lo uso.",
            )
        self._email = email
        self._private_key = key
        self._session = session
        self._timeout = timeout
        self._max_files = max(1, int(max_files))
        self._max_depth = max(0, int(max_depth))
        self._sleep = sleep or time.sleep
        self._clock = clock or time.time
        self._token = None
        self._token_expiry = 0.0
        #: True when the last list_folder stopped early because of max_files.
        self.truncated = False
        #: True when the text returned by the last fetch_text was cut (PDF pages or characters).
        self.last_fetch_truncated = False

    def __repr__(self):  # never expose credentials
        return "<DriveClient %s>" % self._email

    @property
    def client_email(self):
        return self._email

    # -- HTTP plumbing ------------------------------------------------------

    def _http(self):
        if self._session is None:
            try:
                import requests
            except ImportError:
                raise DriveError(
                    "network",
                    "Falta la librería requests en el servidor. Avisale al administrador del sistema.",
                ) from None
            self._session = requests.Session()
        return self._session

    def _send(self, method, url, params=None, data=None, headers=None, stream=False):
        """Send a request with bounded retries; returns the final response."""
        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            final = attempt == MAX_RETRIES
            try:
                resp = self._http().request(
                    method, url, params=params, data=data, headers=headers,
                    timeout=self._timeout, stream=stream,
                )
            except DriveError:
                raise
            except OSError as exc:  # requests exceptions derive from IOError
                last_exc = exc
                if final:
                    break
                self._backoff(attempt, None)
                continue
            status = resp.status_code
            retryable = status == 429 or status >= 500
            if status == 403 and (_error_reasons(resp) & _RATE_REASONS):
                retryable = True
            if retryable and not final:
                self._backoff(attempt, resp)
                continue
            return resp
        _logger.warning("Drive network failure: %s", type(last_exc).__name__)
        raise DriveError(
            "network",
            "No pude conectarme con Google Drive. Revisá la conexión a internet del servidor "
            "y probá de nuevo en unos minutos.",
        )

    def _backoff(self, attempt, resp):
        delay = min(0.5 * (2 ** attempt), 8.0)
        if resp is not None:
            try:
                retry_after = float(resp.headers.get("Retry-After", ""))
                delay = max(delay, min(retry_after, 10.0))
            except (TypeError, ValueError, AttributeError):
                pass
        self._sleep(delay)

    # -- Authentication -----------------------------------------------------

    def _auth_failed(self):
        return DriveError(
            "auth_failed",
            "Google no aceptó la clave de la cuenta de servicio (%s). Revisá que la fecha y "
            "hora del servidor estén bien y que la clave no haya sido borrada o reemplazada "
            "en Google Cloud." % self._email,
        )

    def _get_token(self, force=False):
        now = self._clock()
        if not force and self._token and now < self._token_expiry - TOKEN_SKEW_SECONDS:
            return self._token
        try:
            import jwt
            assertion = jwt.encode(
                {
                    "iss": self._email,
                    "scope": SCOPE,
                    "aud": TOKEN_URI,
                    "iat": int(now),
                    "exp": int(now) + 3600,
                },
                self._private_key,
                algorithm="RS256",
            )
        except Exception:  # noqa: BLE001 - never leak key details
            raise DriveError(
                "invalid_key",
                "No pude firmar con la clave de la cuenta de servicio. Descargá una clave "
                "nueva desde Google Cloud y pegala completa.",
            ) from None
        if isinstance(assertion, bytes):
            assertion = assertion.decode("ascii")
        resp = self._send(
            "POST", TOKEN_URI,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                  "assertion": assertion},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        status = resp.status_code
        if status == 429:
            raise DriveError("rate_limited", "Google está limitando los pedidos. Probá de nuevo en unos minutos.")
        if status >= 500:
            raise DriveError("network", "Google no está respondiendo ahora. Probá de nuevo en unos minutos.")
        if status != 200:
            raise self._auth_failed()
        try:
            body = resp.json()
            token = body["access_token"]
            expires_in = float(body.get("expires_in", 3600))
        except Exception:  # noqa: BLE001
            raise self._auth_failed() from None
        if not isinstance(token, str) or not token:
            raise self._auth_failed()
        self._token = token
        self._token_expiry = now + expires_in
        return token

    # -- API calls ----------------------------------------------------------

    def _raise_for(self, resp, kind):
        status = resp.status_code
        if status < 400:
            return
        reasons = _error_reasons(resp)
        email = self._email
        if status == 401:
            raise self._auth_failed()
        if status == 403:
            if reasons & _DISABLED_REASONS:
                raise DriveError(
                    "api_disabled",
                    "Activá la API de Google Drive en tu proyecto de Google Cloud: "
                    "%s (entrá con la cuenta dueña del proyecto y tocá Habilitar)." % API_LIBRARY_URL,
                )
            if reasons & _RATE_REASONS:
                raise DriveError(
                    "rate_limited",
                    "Google está limitando los pedidos. Probá de nuevo en unos minutos.",
                )
            if "exportsizelimitexceeded" in reasons:
                raise DriveError("too_large", "El archivo es demasiado grande para que Google lo exporte.")
            raise DriveError(
                "forbidden",
                "La cuenta %s no tiene permiso para leer %s. Compartila con rol Lector."
                % (email, "esa carpeta" if kind == "folder" else "ese archivo"),
            )
        if status == 404:
            raise DriveError(
                "not_found",
                "No encuentro %s. ¿La compartiste con %s? Compartila con esa dirección "
                "(rol Lector) y revisá que el link sea el correcto."
                % ("la carpeta" if kind == "folder" else "el archivo", email),
            )
        if status == 429:
            raise DriveError("rate_limited", "Google está limitando los pedidos. Probá de nuevo en unos minutos.")
        if status >= 500:
            raise DriveError("network", "Google Drive no está respondiendo bien ahora. Probá de nuevo en unos minutos.")
        raise DriveError("forbidden", "Google Drive rechazó el pedido (código %s)." % status)

    def _request(self, path, params, kind="file", stream=False):
        url = API_BASE + path
        for attempt in range(2):
            token = self._get_token(force=attempt == 1)
            resp = self._send(
                "GET", url, params=params,
                headers={"Authorization": "Bearer " + token}, stream=stream,
            )
            if resp.status_code == 401 and attempt == 0:
                self._token = None
                continue
            break
        self._raise_for(resp, kind)
        return resp

    def _json(self, path, params, kind="file"):
        resp = self._request(path, params, kind)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            raise DriveError("network", "Google Drive devolvió una respuesta que no entiendo. Probá de nuevo.") from None
        if not isinstance(body, dict):
            raise DriveError("network", "Google Drive devolvió una respuesta que no entiendo. Probá de nuevo.")
        return body

    def _bytes(self, path, params):
        resp = self._request(path, params, "file", stream=True)
        try:
            declared = resp.headers.get("Content-Length")
            if declared and str(declared).isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                raise self._too_large()
            if hasattr(resp, "iter_content"):
                buf = bytearray()
                for chunk in resp.iter_content(65536):
                    buf.extend(chunk)
                    if len(buf) > MAX_DOWNLOAD_BYTES:
                        raise self._too_large()
                return bytes(buf)
            data = resp.content
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise self._too_large()
            return data
        except OSError:
            raise DriveError("network", "Se cortó la descarga desde Google Drive. Probá de nuevo.") from None
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()

    @staticmethod
    def _too_large():
        return DriveError(
            "too_large",
            "El archivo pesa más de %d MB y lo salteo." % (MAX_DOWNLOAD_BYTES // (1024 * 1024)),
        )

    # -- Public API ---------------------------------------------------------

    def folder_info(self, folder_id):
        """Return {'id', 'name'} for a folder; validates access."""
        folder_id = _check_id(folder_id, "ID de carpeta")
        body = self._json(
            "/files/" + folder_id,
            {"fields": "id,name,mimeType", "supportsAllDrives": "true"},
            kind="folder",
        )
        if body.get("mimeType") != FOLDER_MIME:
            raise DriveError(
                "not_found",
                "Ese ID no es de una carpeta sino de un archivo. Pegá el link de la carpeta.",
            )
        return {"id": str(body.get("id", folder_id)), "name": str(body.get("name", ""))}

    def list_folder(self, folder_id):
        """Recursively list files (not folders) below ``folder_id``."""
        root = _check_id(folder_id, "ID de carpeta")
        self.truncated = False
        files = {}
        seen_folders = {root}
        queue = [(root, "", 0)]
        while queue:
            current, path, depth = queue.pop(0)
            page_token = None
            while True:
                params = {
                    "q": "'%s' in parents and trashed = false" % current.replace("\\", "\\\\").replace("'", "\\'"),
                    "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,md5Checksum,size,webViewLink)",
                    "pageSize": "1000",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                }
                if page_token:
                    params["pageToken"] = page_token
                body = self._json("/files", params, kind="folder")
                for item in body.get("files") or []:
                    if not isinstance(item, dict):
                        continue
                    fid = item.get("id")
                    if not isinstance(fid, str) or not _ID_RE.match(fid):
                        continue
                    mime = item.get("mimeType") or ""
                    name = str(item.get("name") or "")
                    if mime == SHORTCUT_MIME:
                        continue
                    if mime == FOLDER_MIME:
                        if depth < self._max_depth and fid not in seen_folders:
                            seen_folders.add(fid)
                            safe = name.replace("/", "-") or "(sin nombre)"
                            queue.append((fid, (path + "/" + safe) if path else safe, depth + 1))
                        continue
                    if fid in files:
                        continue
                    if len(files) >= self._max_files:
                        self.truncated = True
                        return list(files.values())
                    size = item.get("size")
                    try:
                        size = int(size) if size is not None else None
                    except (TypeError, ValueError):
                        size = None
                    files[fid] = DriveFile(
                        id=fid, name=name, mime_type=mime,
                        modified_time=str(item.get("modifiedTime") or ""),
                        md5=item.get("md5Checksum"), size=size, path=path,
                        web_view_link=item.get("webViewLink"),
                    )
                page_token = body.get("nextPageToken")
                if not page_token:
                    break
        return list(files.values())

    def fetch_text(self, f):
        """Return the plain text of ``f`` or None when the type is unsupported/empty.

        `last_fetch_truncated` tells whether the text of the LAST call was cut by a limit (PDF pages or characters).
        """
        self.last_fetch_truncated = False
        fid = _check_id(f.id, "ID de archivo")
        mime = (f.mime_type or "").lower()
        lname = (f.name or "").lower()
        if mime in (GDOC_MIME, GSLIDES_MIME, GSHEET_MIME):
            export = "text/csv" if mime == GSHEET_MIME else "text/plain"
            data = self._bytes("/files/%s/export" % fid, {"mimeType": export})
            return self._finish(_decode(data))
        kind = None
        if mime == PDF_MIME or lname.endswith(".pdf"):
            kind = "pdf"
        elif mime == DOCX_MIME or lname.endswith(".docx"):
            kind = "docx"
        elif mime in HTML_MIMES or lname.endswith(HTML_EXTENSIONS):
            kind = "html"
        elif mime in TEXT_MIMES or lname.endswith(TEXT_EXTENSIONS):
            kind = "text"
        if kind is None or mime.startswith("application/vnd.google-apps."):
            return None
        if f.size is not None and f.size > MAX_DOWNLOAD_BYTES:
            raise self._too_large()
        data = self._bytes("/files/" + fid, {"alt": "media", "supportsAllDrives": "true"})
        if kind == "pdf":
            info = {}
            text = _pdf_to_text(data, info)
            self.last_fetch_truncated = bool(info.get("truncated"))
        elif kind == "docx":
            text = _docx_to_text(data)
        elif kind == "html":
            text = _html_to_text(_decode(data))
        else:
            text = _decode(data)
        if text is None:
            return None
        return self._finish(text)

    def _finish(self, text):
        text = text.lstrip("﻿")
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS]
            self.last_fetch_truncated = True
        return text
