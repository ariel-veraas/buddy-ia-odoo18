"""Streaming helpers without any Odoo dependency (tested on their own).

Three independent pieces:

* `SSEDecoder`: bytes of a Server-Sent Events response -> complete events. Incremental UTF-8 (a multibyte character
  split between two network packets is never corrupted), lines split between packets, CR/LF/CRLF endings, comments
  and keep-alives, and a cap on the total size.
* `OpenAIStream` / `AnthropicStream`: one SSE event at a time -> the new text, plus a check that the provider really
  closed the answer (finish_reason "stop" / stop_reason "end_turn"). A stream that just ends is an incomplete answer.
* `AnswerExtractor`: fed with the fragments of the JSON the model is writing, returns the NEW decoded text of the
  value of the `answer` key. The widget shows it while the model keeps writing; the complete JSON is still validated
  strictly by `prompts.parse_model_json` at the end, so a wrong guess here can only ever show text that is then
  replaced by an error.
* `GreetingGate`: sits after the extractor and holds back the first characters of the answer while they could still
  be an opening greeting, so the greeting that `buddy_text.strip_greeting` removes from the final answer is never
  shown (and then replaced) in the live text either.
"""
import codecs
import json
import re
from collections import namedtuple

from .buddy_text import may_be_greeting, strip_greeting

# Total bytes accepted from one streamed response (same order of magnitude as the non-streaming cap).
MAX_STREAM_BYTES = 2_000_000
# A stream of text longer than this can never be a valid answer (prompts.parse_model_json refuses 12000 characters).
MAX_STREAM_CHARS = 12000 + 64

SSEEvent = namedtuple("SSEEvent", "event data")

_EOL = re.compile(r"\r\n|\n|\r")
_FENCE = re.compile(r"\A\s*```[ \t]*(?:json)?[ \t]*\r?\n(.*?)\r?\n?[ \t]*```\s*\Z", re.S | re.I)
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
REPLACEMENT = "�"

ERROR_MESSAGES = {
    "invalid_key": "El proveedor rechazó la clave de API. Revisá que esté completa y vigente.",
    "not_found": "No encuentro el modelo o la dirección del proveedor. Revisá el nombre del modelo.",
    "rate_limited": "El proveedor limitó las consultas (cuota o saldo). Probá más tarde.",
    "unavailable": "El proveedor cortó la respuesta con un error.",
}


class StreamError(Exception):
    """The provider reported a failure inside the stream. The message is safe to show: it never echoes the provider."""

    def __init__(self, code):
        super().__init__(ERROR_MESSAGES[code])
        self.code = code


class StreamTooLarge(ValueError):
    pass


def strip_code_fence(raw):
    """Remove ONE ```json ... ``` wrapper around the whole text (models without a JSON mode add it). Nothing else."""
    match = _FENCE.match(raw) if isinstance(raw, str) else None
    return match.group(1) if match else raw


def scrub_text(value):
    """Lone UTF-16 surrogates (from a JSON \\ud800 escape) cannot be encoded to UTF-8 or stored: replace them."""
    if isinstance(value, str):
        return _LONE_SURROGATE.sub(REPLACEMENT, value)
    if isinstance(value, list):
        return [scrub_text(item) for item in value]
    if isinstance(value, dict):
        return {scrub_text(key): scrub_text(item) for key, item in value.items()}
    return value


# ------------------------------------------------------------------------------------------------ SSE
class SSEDecoder:
    """Incremental Server-Sent Events decoder. `feed(bytes)` returns the events completed by those bytes."""

    def __init__(self, max_bytes=MAX_STREAM_BYTES):
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._max = max_bytes
        self._total = 0
        self._buffer = ""
        self._event = ""
        self._data = []
        self._started = False

    def feed(self, chunk):
        self._total += len(chunk)
        if self._total > self._max:
            raise StreamTooLarge("La respuesta del proveedor es demasiado grande.")
        text = self._decoder.decode(chunk)
        if not self._started and text:
            self._started = True
            text = text.removeprefix("﻿")
        buffer = self._buffer + text
        events, position = [], 0
        while True:
            match = _EOL.search(buffer, position)
            if not match or (match.group() == "\r" and match.end() == len(buffer)):
                break  # no complete line yet (a lone CR at the end may be the first half of CRLF)
            self._line(buffer[position:match.start()], events)
            position = match.end()
        self._buffer = buffer[position:]
        return events

    def close(self):
        """End of the connection. A multibyte character left half-way is an error; an unfinished event is dropped."""
        self._decoder.decode(b"", final=True)

    def _line(self, line, events):
        if not line:
            if self._data:
                events.append(SSEEvent(self._event or "message", "\n".join(self._data)))
            self._event, self._data = "", []
            return
        if line.startswith(":"):
            return  # comment / keep-alive
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)


# ------------------------------------------------------------------------------------ provider events
def _json_event(event):
    try:
        payload = json.loads(event.data)
    except ValueError as exc:
        raise ValueError("Evento inválido del proveedor.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Evento inválido del proveedor.")
    return payload


def _error_code(details):
    """Map a provider error object (OpenAI or Anthropic shape) to one of our stable codes."""
    kind = ""
    if isinstance(details, dict):
        kind = " ".join(str(details.get(key) or "") for key in ("type", "code")).lower()
    if "authentication" in kind or "permission" in kind or "invalid_api_key" in kind:
        return "invalid_key"
    if "not_found" in kind:
        return "not_found"
    if "rate_limit" in kind or "quota" in kind:
        return "rate_limited"
    return "unavailable"


def _count(value):
    """A token count the provider reported, or None when it is not a sane non-negative integer."""
    return value if type(value) is int and 0 <= value < 10 ** 9 else None


def usage_counts(usage):
    """(tokens in, tokens out) from the `usage` object of a provider answer (OpenAI or Anthropic naming).

    Either one can be None when the provider did not say. Never raises: usage is only a statistic.
    """
    if not isinstance(usage, dict):
        return None, None
    tokens_in = _count(usage.get("prompt_tokens", usage.get("input_tokens")))
    tokens_out = _count(usage.get("completion_tokens", usage.get("output_tokens")))
    return tokens_in, tokens_out


def merge_usage(target, tokens_in, tokens_out):
    """Keep the latest number the provider gave for each side in `target` ({"in": n, "out": n}); None leaves it alone."""
    if target is None:
        return
    if tokens_in is not None:
        target["in"] = tokens_in
    if tokens_out is not None:
        target["out"] = tokens_out


class OpenAIStream:
    """chat.completions with stream=true: `data: {...choices[0].delta.content...}` and a final `data: [DONE]`."""

    def __init__(self):
        self.done = False
        self.finish = None
        self.usage = {}  # {"in": n, "out": n} when the provider reports it (usually in the last chunk)
        self._text = []

    def feed(self, event):
        text = self._feed(event)
        if text:
            self._text.append(text)
        return text

    def _feed(self, event):
        if self.done:
            return ""
        data = event.data.strip()
        if data == "[DONE]":
            self.done = True
            return ""
        payload = _json_event(event)
        if event.event == "error" or payload.get("error"):
            raise StreamError(_error_code(payload.get("error")))
        merge_usage(self.usage, *usage_counts(payload.get("usage")))
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""  # e.g. a usage-only chunk
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError("Evento inválido del proveedor.")
        text = ""
        delta = choice.get("delta")
        if isinstance(delta, dict) and delta.get("content") is not None:
            if not isinstance(delta["content"], str):
                raise ValueError("Fragmento sin texto.")
            text = delta["content"]
        if choice.get("finish_reason"):
            self.finish = choice["finish_reason"]
        return text

    def complete(self):
        # "length" is fine only when the cap hit after the JSON object was already closed (reasoning ate the rest).
        if self.finish != "stop" and not (self.finish == "length" and complete_json_object("".join(self._text))):
            raise ValueError("Respuesta incompleta o rechazada.")


def complete_json_object(text):
    """True when `text` is one whole JSON object (a cut-off answer is not)."""
    if not isinstance(text, str):
        return False
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


class AnthropicStream:
    """Messages API with stream=true: content_block_delta/text_delta, message_delta (stop_reason), message_stop."""

    def __init__(self):
        self.done = False
        self.stop_reason = None
        self.usage = {}  # {"in": n, "out": n}: input tokens come in message_start, output tokens in message_delta

    def feed(self, event):
        payload = _json_event(event)
        kind = payload.get("type") or event.event
        if kind == "error":
            raise StreamError(_error_code(payload.get("error")))
        if kind == "message_start":
            message = payload.get("message")
            merge_usage(self.usage, *usage_counts(message.get("usage") if isinstance(message, dict) else None))
        elif kind == "message_delta":
            merge_usage(self.usage, *usage_counts(payload.get("usage")))
        if kind == "content_block_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                return delta["text"]
        elif kind == "content_block_start":
            block = payload.get("content_block")
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                return block["text"]
        elif kind == "message_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason"):
                self.stop_reason = delta["stop_reason"]
        elif kind == "message_stop":
            self.done = True
        return ""

    def complete(self):
        if not self.done or self.stop_reason != "end_turn":
            raise ValueError("Respuesta incompleta o rechazada.")


# ------------------------------------------------------------------------------------- answer extractor
class _Bad(Exception):
    """The text is not the JSON object we expect: stop extracting (the strict validation will report it)."""


_WS = " \t\r\n"
_HEX = "0123456789abcdefABCDEF"
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", '"': '"', "\\": "\\", "/": "/"}


class AnswerExtractor:
    """Pulls the decoded value of the top-level `answer` string out of a JSON object that is still being written.

    Character-driven state machine: it understands keys in any order (a value that is not `answer` is skipped, nested
    objects/arrays and strings included, so braces and quotes inside text never confuse it), every JSON escape
    (including \\uXXXX surrogate pairs split between fragments) and one optional ```json fence. It never raises:
    when the text is not what it expects it simply stops producing text.
    """

    def __init__(self):
        self.failed = False
        self._answered = False
        self._pending = []
        self._machine = self._run()
        next(self._machine)

    def feed(self, fragment):
        """Feed the next fragment; returns the new answer text it completed (possibly empty)."""
        if self._machine is not None:
            try:
                for char in fragment:
                    self._machine.send(char)
            except StopIteration:
                self._machine = None
            except _Bad:
                self._machine, self.failed = None, True
        text = "".join(self._pending)
        self._pending.clear()
        return text

    # -- state machine (a generator that receives one character per send) -----------------------
    def _skip_ws(self):
        while True:
            char = yield
            if char not in _WS:
                return char

    def _run(self):
        char = yield from self._skip_ws()
        if char == "`":
            for _ in range(2):
                if (yield) != "`":
                    raise _Bad
            info = ""
            while True:
                char = yield
                if char == "\n":
                    break
                info += char
                if len(info) > 16:
                    raise _Bad
            if info.strip().lower() not in ("", "json"):
                raise _Bad
            char = yield from self._skip_ws()
        if char != "{":
            raise _Bad
        char = yield from self._skip_ws()
        while True:
            if char == "}":
                return
            if char != '"':
                raise _Bad
            key = yield from self._string(False)
            if (yield from self._skip_ws()) != ":":
                raise _Bad
            char = yield from self._skip_ws()
            if key == "answer" and not self._answered and char == '"':
                self._answered = True
                yield from self._string(True)
                char = yield from self._skip_ws()
            else:
                char = yield from self._skip_value(char)
            if char == ",":
                char = yield from self._skip_ws()
            elif char != "}":
                raise _Bad

    def _string(self, emit):
        """Consume the rest of a JSON string (the opening quote is already read). Emits its text when `emit`."""
        parts, size, high = [], 0, None

        def put(piece):
            nonlocal size
            if emit:
                self._pending.append(piece)
            elif size < 256:
                parts.append(piece)
                size += len(piece)

        while True:
            char = yield
            if high is not None and char != "\\":
                put(REPLACEMENT)  # a high surrogate that no low surrogate followed
                high = None
            if char == '"':
                return "".join(parts)
            if char != "\\":
                put(char)
                continue
            escape = yield
            if escape in _ESCAPES:
                if high is not None:
                    put(REPLACEMENT)
                    high = None
                put(_ESCAPES[escape])
                continue
            if escape != "u":
                raise _Bad
            digits = ""
            for _ in range(4):
                digit = yield
                if digit not in _HEX:
                    raise _Bad
                digits += digit
            unit = int(digits, 16)
            if high is not None:
                if 0xDC00 <= unit <= 0xDFFF:
                    put(chr(0x10000 + ((high - 0xD800) << 10) + (unit - 0xDC00)))
                    high = None
                    continue
                put(REPLACEMENT)
                high = None
            if 0xD800 <= unit <= 0xDBFF:
                high = unit
            elif 0xDC00 <= unit <= 0xDFFF:
                put(REPLACEMENT)
            else:
                put(chr(unit))

    def _skip_value(self, char):
        """Consume a whole JSON value that starts with `char`; returns the first non-blank character after it."""
        if char == '"':
            yield from self._string(False)
            return (yield from self._skip_ws())
        if char in "{[":
            depth = 1
            while depth:
                char = yield
                if char == '"':
                    yield from self._string(False)
                elif char in "{[":
                    depth += 1
                elif char in "}]":
                    depth -= 1
            return (yield from self._skip_ws())
        if char in ",}]":
            raise _Bad
        while True:  # number, true, false, null
            char = yield
            if char in ",}":
                return char
            if char in _WS:
                return (yield from self._skip_ws())


# ------------------------------------------------------------------------------------------- greeting gate
class GreetingGate:
    """Holds back the beginning of the streamed answer while it could still be a greeting to remove.

    `feed(text)` returns what may be shown now; `finish()` returns whatever is still held (call it when the answer
    ends). With `strip=False` (the person greeted first) everything passes through at once. An answer that does not
    start like a greeting is released at its first word, so the delay only exists for answers that open with "Hola".
    What is released is exactly `strip_greeting(full answer)`.
    """

    HOLD = 40  # the longest opening greeting ("¡Hola, Alejandra María!" plus an emoji) is well under this

    def __init__(self, strip=True):
        self._open = not strip
        self._held = ""

    def feed(self, text):
        if self._open:
            return text
        self._held += text
        if len(self._held) < self.HOLD and may_be_greeting(self._held):
            return ""
        return self._release()

    def finish(self):
        return "" if self._open else self._release()

    def _release(self):
        self._open = True
        held, self._held = self._held, ""
        return strip_greeting(held)
