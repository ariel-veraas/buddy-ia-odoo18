"""Bounded calls to the configured AI provider.

Claude (Anthropic) is spoken to through its native Messages API: the OpenAI compatibility layer is documented as
not production-ready and ignores response_format. OpenAI, Gemini and any other OpenAI-compatible server use the
chat completions API. Only `requests` is needed; no provider SDK.
"""
import http.cookiejar
import ipaddress
import json
import math
import re
import socket
import time
from urllib.parse import urlparse

import requests
import urllib3

from odoo import _, api, models
from . import buddy_vectors
from .buddy_stream import (
    AnthropicStream, OpenAIStream, SSEDecoder, StreamError, MAX_STREAM_BYTES, complete_json_object, merge_usage,
    usage_counts)

# Output token cap of one answer. Gemini 2.5 spends "thinking" tokens inside the same cap (measured: finish_reason
# "length" with 60 visible tokens at a 1800 cap), so it gets a much larger one; the answer itself stays short.
DEFAULT_OUTPUT_TOKENS = 2500
GEMINI_OUTPUT_TOKENS = 8192

# key -> (label, base URL, protocol).
PROVIDERS = {
    "anthropic": ("Claude (Anthropic)", "https://api.anthropic.com/v1", "anthropic"),
    "openai": ("OpenAI (ChatGPT)", "https://api.openai.com/v1", "openai"),
    "gemini": ("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "openai"),
    "custom": ("Otro (compatible con OpenAI)", "", "openai"),
}
# Model used when the administrator left the setting empty (it never overwrites what was written). Names change, so
# OpenAI and "Otro" have none: "Probar conexión" lists what the account can really use. For Gemini the cheap "flash-lite"
# invents more than "flash", which is why flash is the default and the one the docs recommend.
DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "anthropic": "claude-haiku-4-5-20251001", "openai": "", "custom": ""}


class _NoCookies(http.cookiejar.CookiePolicy):
    """A cookie policy that accepts and sends nothing: the shared session below must never carry state between people."""

    netscape, rfc2965, hide_cookie2 = True, False, False

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False

    def domain_return_ok(self, domain, request):
        return False

    def path_return_ok(self, path, request):
        return False


# Embeddings are many small requests to the same host: a pooled connection (keep-alive) saves the TLS handshake of each
# one (measured against Gemini: 0.93 s to 0.46 s per question). Only used for them; no cookies, no redirects.
_SESSION = requests.Session()
_SESSION.cookies.set_policy(_NoCookies())


def default_model(provider):
    """The model to use for a provider when none was configured ('' when the provider has no default)."""
    return DEFAULT_MODELS.get(provider or "", "")


# Embeddings (the semantic search) go through the OpenAI-style `/embeddings` endpoint. Names and parameters were checked
# against the providers' documentation in 2026-10 and by calling Gemini's compatibility layer: it accepts `model`, `input`
# (a string or a list) and `dimensions`, and rejects `task_type`. gemini-embedding-2 (text, 8192 tokens) returns unit
# vectors; gemini-embedding-001 (text, 2048 tokens) needs normalizing when `dimensions` is set (done here for every
# model). 768 is one of the sizes Google recommends and 512 is a good size for text-embedding-3-small (default 1536).
# Claude has no embeddings endpoint: with it the semantic search is simply not available. "Otro" has no default (names
# vary by server): the administrator writes it.
DEFAULT_EMBEDDING_MODELS = {"gemini": "gemini-embedding-2", "openai": "text-embedding-3-small", "anthropic": "", "custom": ""}
DEFAULT_EMBEDDING_DIMS = {"gemini": 768, "openai": 512}
MAX_EMBED_INPUTS = 64


def default_embedding_model(provider):
    """The embeddings model of a provider when none was configured ('' when it has none)."""
    return DEFAULT_EMBEDDING_MODELS.get(provider or "", "")


def default_embedding_dims(provider):
    """The vector size asked of the provider when none was configured (0 = do not ask: the model's own size)."""
    return DEFAULT_EMBEDDING_DIMS.get(provider or "", 0)


def supports_embeddings(provider):
    """Whether the provider can embed text (every OpenAI-compatible one; Claude cannot)."""
    return PROVIDERS.get(provider or "", ("", "", ""))[2] == "openai"


ANTHROPIC_VERSION = "2023-06-01"
MAX_RESPONSE_BYTES = 2_000_000
CONNECT_TIMEOUT = 5
# The provider did not process the request, so one more try is safe and costs nothing extra.
RETRY_STATUS = (502, 503, 504)
RETRY_PAUSE = 1.5
RETRY_MIN_BUDGET = 5
STREAM_MAX_BYTES = MAX_STREAM_BYTES
READ_CHUNK = 64 * 1024
SMALL_READ = 8192  # when no "read what arrived" call exists, read this much at a time so the clock is still looked at
# Addresses an API base must never point to: cloud metadata services hand out credentials to whoever asks from inside
# the server. Link-local ranges (169.254.0.0/16, fe80::/10) are refused as a whole; these are the ones outside them.
BLOCKED_HOSTS = frozenset(("metadata.google.internal", "metadata.goog", "metadata", "instance-data",
                           "instance-data.ec2.internal", "168.63.129.16", "100.100.100.200", "fd00:ec2::254"))


_AUTH_MARKERS = (b"api key", b"api_key", b"apikey", b"invalid_api_key", b"unauthenticated", b"unauthorized",
                 b"authentication", b"permission_denied", b"permission denied")


def _is_auth_failure(body):
    """Whether the body of a 400 says that the key (or the permission) is the problem."""
    return isinstance(body, (bytes, bytearray)) and any(marker in bytes(body[:4096]).lower() for marker in _AUTH_MARKERS)


class ProviderError(Exception):
    """A provider failure with a stable code and a message that is safe to show to an administrator.

    The message never contains the API key, the prompt or the provider's raw response.
    """

    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code = code
        self.status = status


def base_url(provider, custom=""):
    """The API root for a provider choice, or '' when the choice needs a URL that was not given."""
    return (custom if provider == "custom" else PROVIDERS.get(provider, ("", "", ""))[1]).strip().rstrip("/")


def _address(host):
    """The IP address a host name spells out ('169.254.169.254', '2852039166', '0xa9fea9fe', '[::ffff:a9fe:a9fe]'), or None."""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))  # the legacy forms (decimal, hex, octal, short) browsers and libc accept
    except (OSError, ValueError):
        return None


def blocked_address(address):
    """Whether an IP (object or text) is link-local or one of the cloud metadata addresses."""
    if isinstance(address, str):
        address = _address(address)
    if address is None:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_link_local or str(address) in BLOCKED_HOSTS


def blocked_host(host):
    """Whether a host name must never be used as the provider address.

    Judged by what the name says (known metadata names and any spelling of a link-local or metadata IP). A name that
    merely RESOLVES to such an address is not caught: the right control for that is the server's egress policy.
    """
    host = (host or "").strip().lower().rstrip(".")
    return host in BLOCKED_HOSTS or host.endswith(".metadata.google.internal") or blocked_address(host)


def valid_base_url(url):
    parsed = urlparse(url or "")
    try:
        host = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(host) and not parsed.username and not blocked_host(host)


class BuddyProvider(models.AbstractModel):
    _name = "buddy.provider"
    _description = "Proveedor de IA de Buddy"

    @api.model
    def _settings(self):
        get = self.env["ir.config_parameter"].sudo().get_param
        provider = get("buddy_ia.provider") or "anthropic"
        if provider not in PROVIDERS:
            raise ProviderError("not_configured", _("El proveedor elegido no existe."))
        return {"provider": provider, "protocol": PROVIDERS[provider][2],
                "base_url": base_url(provider, get("buddy_ia.api_base") or ""), "api_key": get("buddy_ia.api_key") or ""}

    @api.model
    def _headers(self, settings):
        if settings["protocol"] == "anthropic":
            return {"x-api-key": settings["api_key"], "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json"}
        return {"Authorization": "Bearer " + settings["api_key"], "content-type": "application/json"}

    @api.model
    def _status_error(self, status, body=b""):
        """The ProviderError for an HTTP status (None when it is a success). Never includes the key or the body.

        `body` (the start of the answer) is only LOOKED AT for a 400: Gemini answers a wrong API key with 400 («API key
        not valid»), not 401, and that must not be reported as «the provider is unavailable, try again».
        """
        if status in (401, 403) or (status == 400 and _is_auth_failure(body)):
            return ProviderError("invalid_key", _("El proveedor rechazó la clave de API. Revisá que esté completa y vigente."))
        if status == 404:
            return ProviderError("not_found", _("No encuentro el modelo o la dirección del proveedor. Revisá el nombre del modelo."))
        if status == 429:
            return ProviderError("rate_limited", _("El proveedor limitó las consultas (cuota o saldo). Probá más tarde."))
        if status >= 400:
            return ProviderError("unavailable", _("El proveedor respondió con un error (%(status)s).", status=status), status=status)
        return None

    @staticmethod
    def _error_body(response):
        """The first bytes of an error answer (never more than 4 KB; b"" when it cannot be read)."""
        try:
            return response.raw.read(4096, decode_content=True) or b""
        except Exception:  # noqa: BLE001 - only a hint for the error code
            return b""

    @api.model
    def _check_target(self, settings):
        """The key and address are set, and the address is not an internal metadata service."""
        if not settings["api_key"] or not valid_base_url(settings["base_url"]):
            raise ProviderError("not_configured", _("Falta la clave de API o la dirección del proveedor (o no está permitida)."))

    @staticmethod
    def _read_piece(raw, amount):
        """Up to `amount` bytes of the body, returning as soon as SOME data arrived (b"" at the end).

        A plain `read(amount)` waits until it has all `amount` bytes: a server dripping one byte every few seconds
        keeps it blocked far beyond any deadline. `read1` returns what one network read brought. urllib3 only has it
        from 2.1; before that the underlying http.client response has it (the body is then raw, which is why this
        is used only for identity-encoded bodies). Anything else is read in small pieces.
        """
        encoding = str((getattr(raw, "headers", None) or {}).get("Content-Encoding") or "identity").lower()
        reader = getattr(raw, "read1", None)
        if reader is not None:
            return reader(amount, decode_content=True)
        inner = getattr(getattr(raw, "_fp", None), "read1", None)
        if inner is not None and encoding == "identity" and not getattr(raw, "chunked", False):
            return inner(amount)
        return raw.read(min(amount, SMALL_READ), decode_content=True)

    @staticmethod
    def _read_body(response, deadline):
        """The whole body, at most MAX_RESPONSE_BYTES, within the budget.

        Read in pieces with the clock checked between them (and the socket timeout shrunk to what is left): a server
        that drips one byte at a time cannot hold the worker beyond the deadline, which a single read(n) would allow.
        """
        pieces, size = [], 0
        while size <= MAX_RESPONSE_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("El proveedor excedió el tiempo disponible.")
            BuddyProvider._tighten_read_timeout(response, remaining)
            try:
                piece = BuddyProvider._read_piece(response.raw, min(READ_CHUNK, MAX_RESPONSE_BYTES + 1 - size))
            except (urllib3.exceptions.TimeoutError, socket.timeout) as exc:
                raise TimeoutError("El proveedor excedió el tiempo disponible.") from exc
            if not piece:
                break
            pieces.append(piece)
            size += len(piece)
        if size > MAX_RESPONSE_BYTES:
            raise ValueError("Respuesta demasiado grande.")
        return b"".join(pieces)

    @api.model
    def _call(self, method, path, settings, timeout, body=None, pooled=False):
        """One request, plus a single retry when the provider answers 502/503/504 (overloaded, nothing processed)."""
        started = time.monotonic()
        try:
            return self._call_once(method, path, settings, timeout, body, pooled)
        except ProviderError as exc:
            remaining = timeout - (time.monotonic() - started) - RETRY_PAUSE
            if exc.status not in RETRY_STATUS or remaining < RETRY_MIN_BUDGET:
                raise
        time.sleep(RETRY_PAUSE)
        return self._call_once(method, path, settings, remaining, body, pooled)

    @api.model
    def _call_once(self, method, path, settings, timeout, body=None, pooled=False):
        self._check_target(settings)
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise TimeoutError("El presupuesto de la consulta se agotó.")
        deadline = time.monotonic() + timeout
        try:
            # No redirects: a redirect could send the key to another host.
            send = _SESSION.request if pooled else requests.request
            response = send(method, settings["base_url"] + path, headers=self._headers(settings), json=body,
                            timeout=(min(CONNECT_TIMEOUT, timeout), timeout), allow_redirects=False, stream=True)
        except requests.Timeout as exc:
            raise TimeoutError("El proveedor excedió el tiempo disponible.") from exc
        except requests.RequestException as exc:
            raise ProviderError("network", _("No pude conectarme al proveedor. Revisá la dirección y la conexión del servidor.")) from exc
        try:
            raw = self._read_body(response, deadline)
        finally:
            response.close()
        error = self._status_error(response.status_code, raw)
        if error:
            raise error
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ValueError("El proveedor no devolvió JSON.") from exc

    @staticmethod
    def _output_budget(settings, max_tokens=None):
        """Output token cap of one answer. Gemini 2.5 "thinks" inside the same cap, so it gets a much larger one."""
        if max_tokens:
            return max_tokens
        return GEMINI_OUTPUT_TOKENS if settings.get("provider") == "gemini" else DEFAULT_OUTPUT_TOKENS

    @staticmethod
    def _reasoning_effort(settings, model):
        """Gemini 2.5+ reasoning is kept short (it eats the output cap and adds seconds of silence); None = do not send."""
        if settings.get("provider") == "gemini" and re.match(r"^(models/)?gemini-(2\.5|[3-9])", str(model or "")):
            return "low"
        return None

    @api.model
    def _chat(self, model, messages, timeout, max_tokens=None, temperature=0.3, usage=None):
        """One chat completion as text. `messages` use OpenAI roles ('system', 'user', 'assistant').

        `usage` (optional dict) receives {"in": n, "out": n} when the provider reports its token counts.
        """
        settings = self._settings()
        max_tokens = self._output_budget(settings, max_tokens)
        if settings["protocol"] == "anthropic":
            return self._chat_anthropic(settings, model, messages, timeout, max_tokens, temperature, usage)
        body = self._openai_body(model, messages, max_tokens, temperature, reasoning_effort=self._reasoning_effort(settings, model))
        data = self._call("POST", "/chat/completions", settings, timeout, body)
        self._note_usage(data, usage)
        return self._openai_text(data)

    @staticmethod
    def _note_usage(data, usage):
        """Copy the token counts of a complete (non streamed) answer into the caller's dict."""
        if usage is not None and isinstance(data, dict):
            merge_usage(usage, *usage_counts(data.get("usage")))

    @api.model
    def _openai_body(self, model, messages, max_tokens, temperature, stream=False, include_usage=False, reasoning_effort=None):
        body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"}}
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        if stream:
            body["stream"] = True
            if include_usage:
                # Only asked of OpenAI itself, which documents it; other "compatible" servers may refuse unknown fields.
                body["stream_options"] = {"include_usage": True}
        return body

    @api.model
    def _openai_text(self, data):
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            raise ValueError("Respuesta incompleta o rechazada.")
        content = (choices[0].get("message") or {}).get("content")
        finish = choices[0].get("finish_reason")
        if finish != "stop" and not (finish == "length" and complete_json_object(content)):
            # "length" is fine only when the cap hit after the JSON object was already closed (reasoning ate the rest).
            raise ValueError("Respuesta incompleta o rechazada.")
        if not isinstance(content, str):
            raise ValueError("Respuesta sin texto.")
        return content

    @api.model
    def _anthropic_body(self, model, messages, max_tokens, temperature, stream=False):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = []
        for message in messages:
            if message["role"] == "system":
                continue
            # The Messages API wants strict user/assistant alternation: merge consecutive messages of one role.
            if turns and turns[-1]["role"] == message["role"]:
                turns[-1]["content"] += "\n\n" + message["content"]
            else:
                turns.append({"role": message["role"], "content": message["content"]})
        if not turns or turns[0]["role"] != "user":
            raise ValueError("La conversación debe empezar con un mensaje del usuario.")
        body = {"model": model, "system": system, "messages": turns, "max_tokens": max_tokens,
                "temperature": min(1.0, temperature)}
        if stream:
            body["stream"] = True
        return body

    @api.model
    def _anthropic_text(self, data):
        if not isinstance(data, dict) or data.get("stop_reason") != "end_turn":
            raise ValueError("Respuesta incompleta o rechazada.")
        text = "".join(block.get("text", "") for block in data.get("content") or [] if block.get("type") == "text")
        if not text:
            raise ValueError("Respuesta sin texto.")
        return text

    @api.model
    def _chat_anthropic(self, settings, model, messages, timeout, max_tokens, temperature, usage=None):
        data = self._call("POST", "/messages", settings, timeout, self._anthropic_body(model, messages, max_tokens, temperature))
        self._note_usage(data, usage)
        return self._anthropic_text(data)

    @api.model
    def _embed(self, texts, model, timeout, dims=0, settings=None):
        """Unit vectors (float32 arrays, in the order of `texts`) for a list of texts, from the provider's `/embeddings`.

        One request, bounded by `timeout` (the whole call, no retry beyond the single one `_call` makes for a 502/503/504
        when the budget allows it). `dims` > 0 asks for that vector size. Errors: ProviderError with a stable code
        (`invalid_key`, `not_found`, `rate_limited`, `unavailable`, `network`, `embeddings_unsupported`), TimeoutError, or
        ValueError for an answer that is not a list of vectors; none of them carries the key or the provider's text.
        """
        settings = settings or self._settings()
        if settings["protocol"] == "anthropic":
            raise ProviderError("embeddings_unsupported", _("Claude no ofrece búsqueda por significado. Elegí otro proveedor para activarla."))
        if (not isinstance(texts, list) or not 1 <= len(texts) <= MAX_EMBED_INPUTS
                or any(not isinstance(text, str) or not text.strip() for text in texts)):
            raise ValueError("Textos inválidos.")
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValueError("Modelo de embeddings inválido.")
        body = {"model": model.strip(), "input": texts}
        if dims:
            if type(dims) is not int or not buddy_vectors.MIN_DIMS <= dims <= buddy_vectors.MAX_DIMS:
                raise ValueError("Dimensiones inválidas.")
            body["dimensions"] = dims
        started = time.monotonic()
        try:
            data = self._call("POST", "/embeddings", settings, timeout, body, pooled=True)
        except ProviderError as exc:
            # A kept-alive connection the provider closed while idle shows up as a network error: embeddings are plain
            # computations (safe to repeat), so one more try on a brand new connection when the time allows.
            left = timeout - (time.monotonic() - started)
            if exc.code != "network" or left < 1.0:
                raise
            data = self._call("POST", "/embeddings", settings, left, body)
        return buddy_vectors.parse_embeddings(data, len(texts))

    # ------------------------------------------------------------------------------------------ streaming
    @api.model
    def _chat_stream(self, model, messages, timeout, max_tokens=None, temperature=0.3, settings=None, usage=None):
        """Generator of text fragments of one chat completion, as the provider writes them.

        Only the provider is used (pass the already-read `settings`): it is safe to run after the web request that
        prepared it has ended. `timeout` is the budget of the WHOLE stream: it is cut when it runs out even if the
        provider keeps dripping text. Same error codes as `_chat`. A 502/503/504 is retried once, but only here,
        before a single byte was produced: nothing is ever retried after text was emitted. If the consumer abandons
        the generator the connection is closed (the provider stops generating). The stream must end with the
        provider's own "finished" marker, otherwise the answer is incomplete and ValueError is raised.
        `usage` (optional dict) receives {"in": n, "out": n} when the provider reports its token counts (it is filled
        as they arrive, so it holds what is known even if the stream ends badly).
        """
        settings = settings or self._settings()
        anthropic = settings["protocol"] == "anthropic"
        max_tokens = self._output_budget(settings, max_tokens)
        if anthropic:
            path, body = "/messages", self._anthropic_body(model, messages, max_tokens, temperature, stream=True)
        else:
            path, body = "/chat/completions", self._openai_body(
                model, messages, max_tokens, temperature, stream=True, include_usage=settings.get("provider") == "openai",
                reasoning_effort=self._reasoning_effort(settings, model))
        deadline = self._stream_deadline(timeout)
        response = self._open_stream(settings, path, body, deadline)
        try:
            ctype = str((getattr(response, "headers", None) or {}).get("Content-Type") or "").lower()
            if "json" in ctype:
                # A server that ignores stream=true: one complete answer, delivered as a single fragment.
                raw = self._read_body(response, deadline)
                try:
                    data = json.loads(raw.decode("utf-8"))
                except ValueError as exc:
                    raise ValueError("El proveedor no devolvió JSON.") from exc
                self._note_usage(data, usage)
                yield self._anthropic_text(data) if anthropic else self._openai_text(data)
                return
            parser = AnthropicStream() if anthropic else OpenAIStream()
            decoder = SSEDecoder(STREAM_MAX_BYTES)
            try:
                for chunk in response.iter_content(chunk_size=None):
                    if not chunk:
                        continue
                    if time.monotonic() >= deadline:
                        raise TimeoutError("El proveedor excedió el tiempo disponible.")
                    for event in decoder.feed(chunk):
                        text = parser.feed(event)
                        merge_usage(usage, parser.usage.get("in"), parser.usage.get("out"))
                        if text:
                            yield text
                        if parser.done:
                            break
                    if parser.done:
                        break
                    self._tighten_read_timeout(response, deadline - time.monotonic())
            except StreamError as exc:
                raise ProviderError(exc.code, str(exc)) from exc
            except requests.Timeout as exc:
                raise TimeoutError("El proveedor excedió el tiempo disponible.") from exc
            except requests.RequestException as exc:
                if time.monotonic() >= deadline - 0.05:
                    raise TimeoutError("El proveedor excedió el tiempo disponible.") from exc
                raise ProviderError("network", _("Se cortó la conexión con el proveedor.")) from exc
            decoder.close()
            parser.complete()
        finally:
            response.close()

    @api.model
    def _stream_deadline(self, timeout):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise TimeoutError("El presupuesto de la consulta se agotó.")
        return time.monotonic() + timeout

    @api.model
    def _open_stream(self, settings, path, body, deadline):
        """The streaming POST, up to two attempts (the second one only for 502/503/504: nothing was processed)."""
        self._check_target(settings)
        headers = dict(self._headers(settings), accept="text/event-stream")
        headers["accept-encoding"] = "identity"  # a compressed stream would be buffered by the decompressor
        for attempt in (1, 2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("El presupuesto de la consulta se agotó.")
            try:
                # No redirects: a redirect could send the key to another host.
                response = requests.request("POST", settings["base_url"] + path, headers=headers, json=body,
                                            timeout=(min(CONNECT_TIMEOUT, remaining), remaining),
                                            allow_redirects=False, stream=True)
            except requests.Timeout as exc:
                raise TimeoutError("El proveedor excedió el tiempo disponible.") from exc
            except requests.RequestException as exc:
                raise ProviderError("network", _("No pude conectarme al proveedor. Revisá la dirección y la conexión del servidor.")) from exc
            error = self._status_error(response.status_code, self._error_body(response) if response.status_code == 400 else b"")
            if error is None and response.status_code != 200:
                error = ValueError("Respuesta inesperada del proveedor.")
            if error is None:
                return response
            response.close()
            left = deadline - time.monotonic() - RETRY_PAUSE
            if attempt == 1 and getattr(error, "status", None) in RETRY_STATUS and left >= RETRY_MIN_BUDGET:
                time.sleep(RETRY_PAUSE)
                continue
            raise error

    @staticmethod
    def _tighten_read_timeout(response, remaining):
        """Best effort: the socket read timeout is fixed when connecting, so shrink it to what is left of the budget.

        Without this a provider that sends one fragment and then stalls could hold the worker for a second full
        budget. If the internals of the HTTP library are not where we expect, the original timeout still applies.
        """
        try:
            sock = response.raw._connection.sock
            if sock is not None and remaining > 0:
                sock.settimeout(remaining)
        except Exception:  # noqa: BLE001 - purely an optimization
            pass

    @api.model
    def _list_models(self, timeout=15):
        """Model names this key can use (both protocols answer GET /models)."""
        settings = self._settings()
        data = self._call("GET", "/models", settings, timeout)
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ValueError("Lista de modelos inválida.")
        names = [str(item.get("id", "")).removeprefix("models/") for item in items if isinstance(item, dict)]
        return sorted(name for name in names if name)
