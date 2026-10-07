"""Standalone tests for buddy_stream (no Odoo, no network): SSE decoding, provider events, answer extraction.

Run from the repo root (tools/validate_local.py does it):
    .venv\\Scripts\\python.exe -m unittest discover -s odoo-addons/buddy_ia/tests -p test_stream.py
"""
import importlib.util
import json
import random
import sys
import types
import unittest
from pathlib import Path

MODELS = Path(__file__).resolve().parents[1] / "models"
_package = types.ModuleType("buddy_stream_pkg")
_package.__path__ = [str(MODELS)]
sys.modules[_package.__name__] = _package


def load(name):
    spec = importlib.util.spec_from_file_location(_package.__name__ + "." + name, MODELS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


stream = load("buddy_stream")
prompts = load("prompts")
VALID = {"answer": "Hola", "expression": "happy", "needs_human": False, "contact_role": None, "ticket": None}


def sse(*frames):
    return "".join(frames).encode("utf-8")


def events_of(data, size=1):
    """Decode `data` in packets of `size` bytes."""
    decoder = stream.SSEDecoder()
    out = []
    for start in range(0, len(data), size):
        out += decoder.feed(data[start:start + size])
    decoder.close()
    return out


def extract(text, size=1):
    extractor, out = stream.AnswerExtractor(), []
    for start in range(0, len(text), size):
        out.append(extractor.feed(text[start:start + size]))
    return "".join(out), extractor


class SSEDecoderTests(unittest.TestCase):
    def test_events_split_at_every_byte_boundary(self):
        data = sse("event: a\ndata: uno\n\n", "data: dos\ndata: tres\n\n")
        for size in (1, 2, 3, 7, 1000):
            self.assertEqual(events_of(data, size), [("a", "uno"), ("message", "dos\ntres")], size)

    def test_utf8_character_split_between_packets_is_not_corrupted(self):
        data = sse('data: {"t":"ñandú 😀 áéí"}\n\n')
        for size in (1, 2, 3):
            self.assertEqual(json.loads(events_of(data, size)[0].data)["t"], "ñandú 😀 áéí")

    def test_invalid_utf8_and_a_cut_multibyte_character_are_errors(self):
        with self.assertRaises(ValueError):
            stream.SSEDecoder().feed(b"data: \xff\n\n")
        decoder = stream.SSEDecoder()
        decoder.feed("data: ñ".encode()[:-1])
        with self.assertRaises(ValueError):
            decoder.close()

    def test_comments_keepalives_and_line_endings(self):
        data = b": keepalive\r\n\r\nevent: x\r\ndata: 1\r\n\r\n: otro\n\ndata: 2\r\r: fin\n"
        self.assertEqual(events_of(data, 1), [("x", "1"), ("message", "2")])
        self.assertEqual(events_of(data, 100), [("x", "1"), ("message", "2")])

    def test_crlf_split_between_packets_is_one_line_break(self):
        decoder = stream.SSEDecoder()
        self.assertEqual(decoder.feed(b"data: a\r"), [])
        self.assertEqual(decoder.feed(b"\n\r"), [])
        self.assertEqual(decoder.feed(b"\n"), [("message", "a")])

    def test_unfinished_event_is_dropped_and_fields_without_space_work(self):
        self.assertEqual(events_of(b"data:uno\n\ndata: sin cierre"), [("message", "uno")])

    def test_total_size_is_capped(self):
        decoder = stream.SSEDecoder(max_bytes=100)
        decoder.feed(b"data: " + b"x" * 50)
        with self.assertRaises(stream.StreamTooLarge):
            decoder.feed(b"y" * 60)


def oai(text=None, finish=None):
    choice = {"index": 0, "delta": {} if text is None else {"content": text}, "finish_reason": finish}
    return "data: %s\n\n" % json.dumps({"choices": [choice]})


def run_openai(frames, size=1000):
    parser, out = stream.OpenAIStream(), ""
    for event in events_of(frames.encode(), size):
        out += parser.feed(event)
        if parser.done:
            break
    parser.complete()
    return out


class OpenAIStreamTests(unittest.TestCase):
    def test_text_is_joined_and_closed_by_stop_and_done(self):
        frames = oai("Hol") + oai("a ñ") + oai(None, "stop") + "data: [DONE]\n\n"
        for size in (1, 5, 1000):
            self.assertEqual(run_openai(frames, size), "Hola ñ")

    def test_usage_only_and_empty_chunks_are_ignored(self):
        frames = oai("x") + 'data: {"choices": []}\n\n' + oai("") + oai(None, "stop") + "data: [DONE]\n\n"
        self.assertEqual(run_openai(frames), "x")

    def test_stream_without_finish_reason_or_with_other_reason_is_incomplete(self):
        for frames in (oai("x") + "data: [DONE]\n\n", oai("x"), oai("x", "length") + "data: [DONE]\n\n",
                       oai("x", "content_filter")):
            with self.subTest(frames=frames[-40:]), self.assertRaises(ValueError):
                run_openai(frames)

    def test_length_is_accepted_only_when_the_json_object_is_already_complete(self):
        # Gemini "thinking" can exhaust the cap after the answer was fully written.
        whole = '{"answer": "Hola", "needs_human": false}'
        for size in (1, 7, 1000):
            self.assertEqual(run_openai(oai(whole[:20]) + oai(whole[20:], "length") + "data: [DONE]\n\n", size), whole)
        for cut in ('{"answer": "Hola', '["no objeto"]', ''):
            with self.subTest(cut=cut), self.assertRaises(ValueError):
                run_openai(oai(cut, "length") + "data: [DONE]\n\n")

    def test_complete_json_object_helper(self):
        self.assertTrue(stream.complete_json_object('{"a": 1}'))
        for value in ('{"a": ', '[1]', '"x"', '', None, 5):
            self.assertFalse(stream.complete_json_object(value))

    def test_error_in_the_middle_maps_to_stable_codes_without_echoing_the_provider(self):
        secret = "Bearer sk-SECRETA"
        cases = {"rate_limit_exceeded": "rate_limited", "invalid_api_key": "invalid_key", "server_error": "unavailable"}
        for kind, code in cases.items():
            frames = oai("parcial") + "data: %s\n\n" % json.dumps({"error": {"type": kind, "message": secret}})
            with self.subTest(kind=kind), self.assertRaises(stream.StreamError) as caught:
                run_openai(frames)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("SECRETA", str(caught.exception))

    def test_malformed_events_are_errors(self):
        for frames in ("data: {no json\n\n", "data: [1]\n\n", 'data: {"choices": [5]}\n\n',
                       'data: {"choices": [{"delta": {"content": 5}}]}\n\n'):
            with self.subTest(frames=frames), self.assertRaises(ValueError):
                run_openai(frames)


def anth(kind, **fields):
    return "event: %s\ndata: %s\n\n" % (kind, json.dumps(dict(type=kind, **fields)))


def run_anthropic(frames, size=1000):
    parser, out = stream.AnthropicStream(), ""
    for event in events_of(frames.encode(), size):
        out += parser.feed(event)
    parser.complete()
    return out


ANTHROPIC_OK = (anth("message_start", message={"id": "m"}) + anth("content_block_start", index=0, content_block={"type": "text", "text": ""})
                + anth("ping") + anth("content_block_delta", index=0, delta={"type": "text_delta", "text": "Hola "})
                + anth("content_block_delta", index=0, delta={"type": "text_delta", "text": "mundo ñ"})
                + anth("content_block_stop", index=0) + anth("message_delta", delta={"stop_reason": "end_turn"})
                + anth("message_stop"))


class AnthropicStreamTests(unittest.TestCase):
    def test_full_conversation(self):
        for size in (1, 9, 1000):
            self.assertEqual(run_anthropic(ANTHROPIC_OK, size), "Hola mundo ñ")

    def test_other_delta_types_are_ignored(self):
        frames = anth("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": "{}"}) + ANTHROPIC_OK
        self.assertEqual(run_anthropic(frames), "Hola mundo ñ")

    def test_missing_stop_message_or_other_stop_reason_is_incomplete(self):
        for frames in (ANTHROPIC_OK.replace(anth("message_stop"), ""),
                       ANTHROPIC_OK.replace("end_turn", "max_tokens"), ANTHROPIC_OK.replace("end_turn", "refusal"),
                       anth("content_block_delta", index=0, delta={"type": "text_delta", "text": "x"})):
            with self.subTest(frames=frames[-60:]), self.assertRaises(ValueError):
                run_anthropic(frames)

    def test_error_event_maps_to_a_code(self):
        for kind, code in (("overloaded_error", "unavailable"), ("rate_limit_error", "rate_limited"),
                           ("authentication_error", "invalid_key"), ("not_found_error", "not_found")):
            frames = ANTHROPIC_OK.split(anth("content_block_stop", index=0))[0] + anth(
                "error", error={"type": kind, "message": "sk-ant-SECRETA"})
            with self.subTest(kind=kind), self.assertRaises(stream.StreamError) as caught:
                run_anthropic(frames)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("SECRETA", str(caught.exception))


class UsageTests(unittest.TestCase):
    """Token counts the providers report: statistics only, so anything odd is ignored instead of failing the answer."""

    @staticmethod
    def feed(parser, frames, size=7):
        text = ""
        for event in events_of(frames.encode(), size):
            text += parser.feed(event)
        return text

    def test_both_namings_are_understood(self):
        self.assertEqual(stream.usage_counts({"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}), (12, 5))
        self.assertEqual(stream.usage_counts({"input_tokens": 12, "output_tokens": 5}), (12, 5))
        self.assertEqual(stream.usage_counts({"input_tokens": 12}), (12, None))
        self.assertEqual(stream.usage_counts({"output_tokens": 0}), (None, 0))

    def test_anything_odd_is_ignored(self):
        for junk in (None, [], "12", 7, {"prompt_tokens": "12"}, {"prompt_tokens": -1}, {"prompt_tokens": 1.5},
                     {"prompt_tokens": True}, {"prompt_tokens": 10 ** 12}, {"prompt_tokens": None, "completion_tokens": []}):
            self.assertEqual(stream.usage_counts(junk), (None, None), repr(junk))

    def test_merge_keeps_the_latest_number_for_each_side(self):
        target = {}
        stream.merge_usage(target, 10, None)
        stream.merge_usage(target, None, 3)
        stream.merge_usage(target, None, 9)
        self.assertEqual(target, {"in": 10, "out": 9})
        stream.merge_usage(None, 1, 1)  # no destination: nothing happens, nothing breaks

    def test_openai_reports_usage_in_a_final_chunk_without_choices(self):
        usage_chunk = 'data: {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 7}}\n\n'
        frames = oai("Hola") + oai(None, "stop") + usage_chunk + "data: [DONE]\n\n"
        parser = stream.OpenAIStream()
        self.assertEqual(self.feed(parser, frames), "Hola")
        parser.complete()
        self.assertEqual(parser.usage, {"in": 40, "out": 7})

    def test_openai_without_usage_leaves_it_empty(self):
        parser = stream.OpenAIStream()
        self.feed(parser, oai("Hola") + oai(None, "stop") + "data: [DONE]\n\n")
        self.assertEqual(parser.usage, {})

    def test_anthropic_input_comes_with_the_start_and_output_with_the_delta(self):
        frames = (anth("message_start", message={"usage": {"input_tokens": 25, "output_tokens": 1}})
                  + anth("content_block_delta", index=0, delta={"type": "text_delta", "text": "Hola"})
                  + anth("message_delta", delta={"stop_reason": "end_turn"}, usage={"output_tokens": 15})
                  + anth("message_stop"))
        parser = stream.AnthropicStream()
        self.assertEqual(self.feed(parser, frames), "Hola")
        parser.complete()
        self.assertEqual(parser.usage, {"in": 25, "out": 15}, "the cumulative output of the delta wins")

    def test_a_stream_that_fails_keeps_what_was_counted_so_far(self):
        frames = (anth("message_start", message={"usage": {"input_tokens": 25}})
                  + anth("error", error={"type": "overloaded_error", "message": "x"}))
        parser = stream.AnthropicStream()
        with self.assertRaises(stream.StreamError):
            self.feed(parser, frames)
        self.assertEqual(parser.usage, {"in": 25})

    def test_malformed_usage_in_an_event_never_breaks_the_stream(self):
        frames = (anth("message_start", message="no es un objeto")
                  + anth("message_delta", delta={"stop_reason": "end_turn"}, usage="x") + anth("message_stop"))
        parser = stream.AnthropicStream()
        self.feed(parser, frames)
        parser.complete()
        self.assertEqual(parser.usage, {})


ANSWER_JSON = json.dumps({"answer": 'Línea 1\nLínea "2" \\ / {llaves} [x] 😀 é', "expression": "happy", "needs_human": False,
                          "contact_role": None, "ticket": None})


class AnswerExtractorTests(unittest.TestCase):
    def test_the_decoded_answer_comes_out_whatever_the_fragment_size(self):
        expected = json.loads(ANSWER_JSON)["answer"]
        for size in (1, 2, 3, 5, 11, 1000):
            text, extractor = extract(ANSWER_JSON, size)
            self.assertEqual(text, expected, size)
            self.assertFalse(extractor.failed)

    def test_text_arrives_incrementally(self):
        extractor = stream.AnswerExtractor()
        self.assertEqual(extractor.feed('{"answer": "Ho'), "Ho")
        self.assertEqual(extractor.feed("la, mun"), "la, mun")
        self.assertEqual(extractor.feed('do", "expression": "happy"}'), "do")

    def test_ascii_escaped_json_with_surrogate_pairs_split_anywhere(self):
        raw = json.dumps({"answer": "a😀b é ñ  ", "expression": "happy"}, ensure_ascii=True)
        self.assertIn("\\ud83d\\ude00", raw)
        for size in (1, 2, 4, 6, 7, 13, 1000):
            self.assertEqual(extract(raw, size)[0], "a😀b é ñ  ", size)

    def test_lone_surrogates_become_replacement_characters(self):
        for raw, expected in (('{"answer": "a\\ud83dz"}', "a�z"), ('{"answer": "a\\ude00z"}', "a�z"),
                              ('{"answer": "a\\ud83d"}', "a�"), ('{"answer": "\\ud83d\\n"}', "�\n")):
            for size in (1, 100):
                self.assertEqual(extract(raw, size)[0], expected, (raw, size))

    def test_every_simple_escape(self):
        raw = '{"answer": "\\n\\t\\r\\b\\f\\"\\\\\\/ \\u00e9"}'
        self.assertEqual(extract(raw, 1)[0], '\n\t\r\b\f"\\/ é')

    def test_keys_in_any_order(self):
        raw = ('{"expression": "happy", "ticket": {"title": "T}", "description": "{\\"answer\\": \\"no\\"}"}, '
               '"needs_human": true, "contact_role": null, "n": -1.5e3, "list": [1, "a]", {"answer": "no"}], '
               '"answer": "Al final"}')
        for size in (1, 3, 1000):
            text, extractor = extract(raw, size)
            self.assertEqual(text, "Al final", size)
            self.assertFalse(extractor.failed)

    def test_a_nested_answer_key_is_not_the_answer(self):
        self.assertEqual(extract('{"ticket": {"answer": "no"}, "answer": "si"}')[0], "si")
        self.assertEqual(extract('{"ticket": {"answer": "no"}}')[0], "")

    def test_a_second_answer_key_is_ignored(self):
        self.assertEqual(extract('{"answer": "uno", "answer": "dos"}')[0], "uno")

    def test_whitespace_everywhere(self):
        self.assertEqual(extract('  \n{ "answer"\n :\t "x" ,\n "expression" : "happy" }  ')[0], "x")

    def test_truncated_json_gives_what_was_written(self):
        text, extractor = extract('{"answer": "Se corta acá y no hay cie', 4)
        self.assertEqual(text, "Se corta acá y no hay cie")
        self.assertFalse(extractor.failed)
        self.assertEqual(extract('{"answer": "\\u00', 1)[0], "")

    def test_one_json_fence_is_tolerated(self):
        for wrapper in ("```json\n%s\n```", "```\n%s\n```", "  ```JSON\r\n%s\r\n```  ", "```json\n%s"):
            raw = wrapper % ANSWER_JSON
            for size in (1, 100):
                self.assertEqual(extract(raw, size)[0], json.loads(ANSWER_JSON)["answer"], (wrapper, size))

    def test_other_fences_and_prose_produce_nothing(self):
        for raw in ("Claro: " + ANSWER_JSON, "```python\n" + ANSWER_JSON, "[" + ANSWER_JSON + "]", "``x", "null",
                    '{"answer": 5}', '{"answer" "x"}', '{"answer": "bad \\x escape"}', '{"a" 1}'):
            text, extractor = extract(raw, 3)
            self.assertEqual(text, "" if raw != '{"answer": "bad \\x escape"}' else "bad ", raw)

    def test_raw_text_after_a_failure_is_ignored(self):
        text, extractor = extract('{"answer": "bad \\x escape and more"}', 5)
        self.assertTrue(extractor.failed)
        self.assertEqual(text, "bad ")

    def test_random_chunking_always_matches_json_loads(self):
        rng = random.Random(7)
        for _ in range(200):
            answer = "".join(rng.choice('ab "\\\n{}[]é😀 /\t') for _ in range(rng.randint(1, 40)))
            for ensure_ascii in (True, False):
                raw = json.dumps({"x": {"answer": 1}, "answer": answer, "expression": "happy"}, ensure_ascii=ensure_ascii)
                extractor, out, position = stream.AnswerExtractor(), [], 0
                while position < len(raw):
                    step = rng.randint(1, 9)
                    out.append(extractor.feed(raw[position:position + step]))
                    position += step
                self.assertEqual("".join(out), answer)

    def test_the_extractor_never_raises(self):
        rng = random.Random(3)
        alphabet = '{}[]":,\\ntu0123456789abcdefg`\n\t -.'
        for _ in range(500):
            extractor = stream.AnswerExtractor()
            for _ in range(rng.randint(1, 30)):
                extractor.feed("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 6))))


class FenceAndValidationTests(unittest.TestCase):
    def test_strip_code_fence_only_removes_one_json_wrapper(self):
        inner = json.dumps(VALID)
        for raw in ("```json\n%s\n```" % inner, "```\n%s\n```" % inner, "\n```json\r\n%s\r\n```\n" % inner):
            self.assertEqual(stream.strip_code_fence(raw), inner)
        for raw in (inner, "texto ```json\n%s\n```" % inner, "```python\n%s\n```" % inner, "```json\n%s" % inner, "x", ""):
            self.assertEqual(stream.strip_code_fence(raw), raw)

    def test_parse_model_json_accepts_a_fenced_valid_object_and_stays_strict(self):
        inner = json.dumps(VALID)
        self.assertEqual(prompts.parse_model_json("```json\n%s\n```" % inner), dict(VALID, sources_used=None, confidence=None, conflict=False))
        for raw in ("```json\n{}\n```", "```json\n%s\n```\n```" % inner, "Aquí está:\n```json\n%s\n```" % inner,
                    "```json\n" + json.dumps(dict(VALID, expression="otra")) + "\n```",
                    "```json\n" + json.dumps(dict(VALID, needs_human="no")) + "\n```"):
            with self.subTest(raw=raw[:50]), self.assertRaises(ValueError):
                prompts.parse_model_json(raw)
        # Keys outside the contract are ignored (not an error); the optional ones get their defaults.
        fenced = prompts.parse_model_json("```json\n" + json.dumps(dict(VALID, extra=1)) + "\n```")
        self.assertEqual(fenced, dict(VALID, sources_used=None, confidence=None, conflict=False))
        self.assertEqual(prompts.parse_model_json('{"answer": "a", "expression": "happy", "needs_human": false}')["ticket"], None)

    def test_lone_surrogates_in_the_model_output_are_cleaned(self):
        result = prompts.parse_model_json('{"answer": "a\\ud83d", "expression": "happy", "needs_human": false, '
                                          '"contact_role": null, "ticket": null}')
        self.assertEqual(result["answer"], "a�")
        result["answer"].encode("utf-8")

    def test_the_prompt_asks_for_answer_first(self):
        self.assertIn("answer va PRIMERA", prompts.build_system_prompt("Buddy", "Acme", ["general"]))

    def test_scrub_text_recurses(self):
        self.assertEqual(stream.scrub_text({"a": ["\ud800x", 1, None]}), {"a": ["�x", 1, None]})


def gate_output(answer, size=1, strip=True):
    """What a GreetingGate lets through for an answer written `size` characters at a time (pieces, then the flush)."""
    gate, pieces = stream.GreetingGate(strip=strip), []
    for start in range(0, len(answer), size):
        pieces.append(gate.feed(answer[start:start + size]))
    pieces.append(gate.finish())
    return pieces


class GreetingGateTests(unittest.TestCase):
    ANSWER = "¡Hola, Ana! Para crear un ticket abrí Soporte y completá el formulario."

    def test_the_greeting_is_held_back_and_never_released_at_any_packet_size(self):
        for size in (1, 2, 3, 5, 11, 40, 500):
            with self.subTest(size=size):
                pieces = gate_output(self.ANSWER, size)
                self.assertEqual("".join(pieces), "Para crear un ticket abrí Soporte y completá el formulario.")
                self.assertFalse(any("Hola" in piece or "¡" in piece for piece in pieces))

    def test_the_gate_agrees_with_strip_greeting_for_every_opening(self):
        text = sys.modules[_package.__name__ + ".buddy_text"]
        openings = ["¡Hola! ", "Hola, Ana! ", "Buen día, Juan. ", "¡Buenos días! ", "Buenas tardes, Ana: ", "Hey! ", "hola! ", "¡Hola Ana! 😊 ",
                    "Hola ¿qué necesitás? ", "Buenas prácticas: ", "Hola Para ", "Holanda es ", "Hay que ", "Hi, ", "Sí. ", "Abrí ", "1. Abrí "]
        for opening in openings:
            for size in (1, 4, 100):
                with self.subTest(opening=opening, size=size):
                    full = opening + "para crear un ticket abrí Soporte y completá el formulario."
                    self.assertEqual("".join(gate_output(full, size)), text.strip_greeting(full))

    def test_answers_that_cannot_be_a_greeting_are_released_at_their_first_word(self):
        for answer in ("Abrí Soporte y elegí Nuevo.", "Para crear un ticket abrí Soporte.", "1. Abrí Soporte y elegí Nuevo.",
                       "Hay tres pasos para crear el ticket.", "No encontré eso en los documentos."):
            with self.subTest(answer=answer):
                pieces = gate_output(answer, 1)
                first = next(index for index, piece in enumerate(pieces) if piece)
                self.assertLessEqual(first, 6, "released within the first word or two, not after the 40 held characters")
                self.assertEqual("".join(pieces), answer)

    def test_a_greeting_like_start_is_held_until_it_is_decided(self):
        pieces = gate_output("Hola, mundo", 1)
        self.assertEqual(pieces[:-1], [""] * 10 + [""], "nothing is shown while it could still be a greeting to remove")
        self.assertEqual(pieces[-1], "Mundo")

    def test_when_the_person_greeted_everything_passes_through_at_once(self):
        self.assertEqual(gate_output(self.ANSWER, 3, strip=False)[:-1], [self.ANSWER[i:i + 3] for i in range(0, len(self.ANSWER), 3)])
        self.assertEqual("".join(gate_output(self.ANSWER, 1, strip=False)), self.ANSWER)

    def test_a_bare_greeting_or_a_short_answer_is_flushed_when_the_answer_ends(self):
        self.assertEqual("".join(gate_output("¡Hola!", 2)), "¡Hola!")
        self.assertEqual("".join(gate_output("¡Hola! Sí.", 2)), "Sí.")
        gate = stream.GreetingGate()
        self.assertEqual((gate.feed(""), gate.finish(), gate.finish()), ("", "", ""))

    def test_it_never_loses_or_duplicates_text_whatever_the_input(self):
        rng = random.Random(7)
        alphabet = "¡!¿?. ,Hola buenasdíAnaPara\n😊x"
        text = sys.modules[_package.__name__ + ".buddy_text"]
        for unused in range(300):
            answer = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 90)))
            expected = text.strip_greeting(answer)
            self.assertEqual("".join(gate_output(answer, rng.randint(1, 9))), expected, repr(answer))


if __name__ == "__main__":
    unittest.main()
