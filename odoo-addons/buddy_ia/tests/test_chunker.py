"""Standalone tests for buddy_chunker (no Odoo, no network).

Run from the repo root (tools/validate_local.py does it):
    .venv\\Scripts\\python.exe -m unittest discover -s odoo-addons/buddy_ia/tests -p test_chunker.py
"""
import importlib.util
import random
import re
import sys
import time
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "models" / "buddy_chunker.py"
_spec = importlib.util.spec_from_file_location("buddy_chunker_under_test", MODULE_PATH)
chunker = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = chunker
_spec.loader.exec_module(chunker)
split_text = chunker.split_text


def words(count, seed=1, length=(2, 9)):
    rng = random.Random(seed)
    return ["".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for unused in range(rng.randint(*length))) for unused in range(count)]


class SplitTextTests(unittest.TestCase):
    def test_empty_and_blank_input_give_no_chunks(self):
        for text in ("", " ", "\n\n", " \r\n \r\n\t ", "\n\n\n\n"):
            self.assertEqual(split_text(text), [], repr(text))

    def test_short_text_is_a_single_normalized_chunk(self):
        self.assertEqual(split_text("Hola   mundo\r\nsegunda  línea"), ["Hola mundo segunda línea"])

    def test_paragraphs_are_kept_together_until_the_size_limit(self):
        text = "uno uno uno\n\ndos dos dos\n\ntres tres tres"
        self.assertEqual(split_text(text, size=100, overlap=0), ["uno uno uno dos dos dos tres tres tres"])
        self.assertEqual(split_text(text, size=25, overlap=0), ["uno uno uno dos dos dos", "tres tres tres"])

    def test_no_chunk_exceeds_the_maximum_size(self):
        text = "\n\n".join(" ".join(words(120, seed)) for seed in range(8))
        for size, overlap in ((1000, 150), (200, 40), (60, 10), (30, 0)):
            chunks = split_text(text, size=size, overlap=overlap, limit=10_000)
            self.assertTrue(chunks)
            self.assertLessEqual(max(map(len, chunks)), size, (size, overlap))

    def test_long_paragraph_is_cut_on_spaces(self):
        text = " ".join(words(400, 3))
        chunks = split_text(text, size=100, overlap=0, limit=10_000)
        self.assertGreater(len(chunks), 5)
        self.assertEqual(" ".join(chunks).split(), text.split(), "no word is cut in half or lost")

    def test_unbroken_text_is_cut_at_the_size(self):
        chunks = split_text("x" * 2500, size=1000, overlap=0)
        self.assertEqual([len(chunk) for chunk in chunks], [1000, 1000, 500])

    def test_each_chunk_repeats_the_end_of_the_previous_one(self):
        text = "\n\n".join(" ".join(words(10, seed)) for seed in range(60))
        chunks = split_text(text, size=300, overlap=60, limit=10_000)
        self.assertGreater(len(chunks), 3)
        for previous, current in zip(chunks, chunks[1:]):
            shared = [n for n in range(1, 61) if previous.endswith(current[:n]) and (n == len(current) or current[n] == " ")]
            self.assertTrue(shared, "the next chunk must start with the tail of the previous one")
            self.assertTrue(previous.endswith(current[:max(shared)]))

    def test_overlap_never_starts_in_the_middle_of_a_word(self):
        text = "\n\n".join(" ".join(words(30, seed)) for seed in range(6))
        chunks = split_text(text, size=250, overlap=50, limit=10_000)
        vocabulary = set(text.split())
        for chunk in chunks:
            self.assertIn(chunk.split()[0], vocabulary)
            self.assertIn(chunk.split()[-1], vocabulary)

    def test_zero_overlap_chunks_do_not_repeat_text(self):
        text = "\n\n".join("párrafo %d " % n + "palabra " * 20 for n in range(30))
        chunks = split_text(text, size=300, overlap=0, limit=10_000)
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_chunk_count_is_capped_by_the_limit(self):
        text = "\n\n".join("párrafo número %d con algo de contenido" % n for n in range(20000))
        self.assertEqual(len(split_text(text, size=100, overlap=10, limit=7)), 7)
        self.assertLessEqual(len(split_text(text)), chunker.MAX_CHUNKS_PER_DOCUMENT)
        self.assertEqual(len(split_text(text)), chunker.MAX_CHUNKS_PER_DOCUMENT)
        self.assertEqual(split_text(text, size=100, overlap=10, limit=0), [])

    def test_the_limit_keeps_the_first_chunks(self):
        text = "\n\n".join("sección %03d " % n + "texto " * 10 for n in range(100))
        everything = split_text(text, size=120, overlap=0, limit=10_000)
        self.assertEqual(split_text(text, size=120, overlap=0, limit=5), everything[:5])

    def test_invalid_parameters_are_rejected(self):
        for size, overlap in ((0, 0), (-5, 0), (100, 100), (100, 150), (100, -1)):
            with self.subTest(size=size, overlap=overlap), self.assertRaises(ValueError):
                split_text("texto", size=size, overlap=overlap)

    def test_never_returns_empty_or_blank_chunks_whatever_the_input(self):
        rng = random.Random(7)
        alphabet = ["a", "bb", "ccc", " ", "  ", "\n", "\n\n", "\r\n", "\t", "ñ", "é"]
        for round_number in range(200):
            text = "".join(rng.choice(alphabet) for unused in range(rng.randint(0, 400)))
            size = rng.choice((5, 20, 80, 1000))
            overlap = rng.choice((0, 1, size // 3, size - 1))
            chunks = split_text(text, size=size, overlap=overlap, limit=rng.choice((1, 3, 400)))
            for chunk in chunks:
                self.assertTrue(chunk.strip(), (round_number, text, chunk))
                self.assertLessEqual(len(chunk), size, (round_number, text))
                self.assertEqual(chunk, chunk.strip())

    def test_default_settings_on_a_realistic_document(self):
        text = "\n\n".join(" ".join(words(90, seed)) + "." for seed in range(60))
        chunks = split_text(text)
        self.assertTrue(all(0 < len(chunk) <= chunker.CHUNK_SIZE for chunk in chunks))
        covered = set(" ".join(chunks).split())
        self.assertEqual(covered, set(text.split()))


def reference_split(text, size, overlap, limit):
    """The straightforward (quadratic) version of split_text, kept here to prove the fast one gives the same chunks."""
    def pieces():
        for paragraph in text.replace("\r", "").split("\n\n"):
            paragraph = " ".join(paragraph.split())
            while len(paragraph) > size:
                cut = paragraph.rfind(" ", size // 2, size)
                cut = cut if cut > 0 else size
                yield paragraph[:cut].strip()
                paragraph = paragraph[cut:].strip()
            if paragraph:
                yield paragraph

    chunks, current = [], ""
    for piece in pieces():
        if current and len(current) + 1 + len(piece) > size:
            chunks.append(current)
            candidate = (chunker._overlap_tail(current, overlap) + " " + piece).strip()
            current = candidate if len(candidate) <= size else piece
        else:
            current = (current + " " + piece) if current else piece
    if current:
        chunks.append(current)
    return chunks[:limit]


class SplitTextExTests(unittest.TestCase):
    def test_reports_truncation_only_when_something_was_left_out(self):
        text = "\n\n".join("párrafo número %d con algo de contenido" % n for n in range(500))
        chunks, truncated = chunker.split_text_ex(text, size=100, overlap=10, limit=7)
        self.assertEqual((len(chunks), truncated), (7, True))
        everything, truncated = chunker.split_text_ex(text, size=100, overlap=10, limit=100_000)
        self.assertFalse(truncated)
        self.assertGreater(len(everything), 7)
        exact, truncated = chunker.split_text_ex(text, size=100, overlap=10, limit=len(everything))
        self.assertEqual((exact, truncated), (everything, False), "exactly at the limit is not truncated")
        self.assertEqual(chunker.split_text_ex("", limit=5), ([], False))

    def test_default_limit_is_the_documented_400(self):
        text = "\n\n".join("sección %d " % n + "texto " * 30 for n in range(3000))
        chunks, truncated = chunker.split_text_ex(text)
        self.assertEqual((len(chunks), truncated), (chunker.MAX_CHUNKS_PER_DOCUMENT, True))
        self.assertEqual(chunker.MAX_CHUNKS_PER_DOCUMENT, 400)

    def test_same_chunks_as_the_reference_implementation(self):
        rng = random.Random(11)
        alphabet = ["a", "bb", "ccc", " ", "  ", "\n", "\n\n", "\r\n", "\t", "ñ", "é", "xyz"]
        for round_number in range(400):
            text = "".join(rng.choice(alphabet) for unused in range(rng.randint(0, 500)))
            size = rng.choice((1, 2, 5, 20, 80, 1000))
            overlap = rng.choice([0, size // 3, size - 1] if size > 1 else [0])
            limit = rng.choice((1, 3, 400))
            self.assertEqual(split_text(text, size, overlap, limit), reference_split(text, size, overlap, limit),
                             (round_number, text, size, overlap, limit))


class HostileInputTests(unittest.TestCase):
    """Drive files are untrusted: nothing a document contains may take more than a moment to process."""

    BUDGET = 1.0  # seconds, for an input far larger than any real line

    def timed(self, function, *args):
        started = time.perf_counter()
        result = function(*args)
        return result, time.perf_counter() - started

    def test_line_of_200k_dots_is_not_a_redos(self):
        for line in ("Objetivo " + "." * 200_000 + " 3", "." * 200_000 + "3", ". " * 100_000 + "3",
                     "x" + ".." * 100_000 + " 12", "." * 200_000, "a" + ".  " * 70_000 + "1"):
            text = "Introducción\n" + line + "\n" + line + "\nFin"
            result, elapsed = self.timed(chunker.strip_toc, text)
            self.assertLess(elapsed, self.BUDGET, line[:12])
            self.assertEqual(result, text, "a line this long is prose, never a table-of-contents entry")

    def test_many_hostile_lines_stay_linear(self):
        for unit in ("x" + "." * 190 + "1", "x. . . . . . . . . . . . . . . . . . . . . . . . . . . . . . x 1", "." * 150 + " " * 40 + "a1"):
            text = "\n".join([unit] * 5000)
            _, elapsed = self.timed(chunker.strip_toc, text)
            self.assertLess(elapsed, self.BUDGET, unit[:20])

    def test_flat_text_with_a_heading_and_endless_entries(self):
        text = "Índice " + "Tema 1 " * 200_000
        result, elapsed = self.timed(chunker.strip_toc, text)
        self.assertLess(elapsed, self.BUDGET)
        self.assertTrue(result)

    def test_giant_inputs_are_split_in_linear_time(self):
        for text in ("palabra " * 125_000, "x" * 1_000_000, "\n\n".join(["párrafo corto"] * 80_000), "a b " * 250_000):
            chunks, elapsed = self.timed(split_text, text)
            self.assertLess(elapsed, self.BUDGET)
            self.assertTrue(0 < len(chunks) <= chunker.MAX_CHUNKS_PER_DOCUMENT)

    def test_a_long_line_cannot_be_a_heading(self):
        text = "Contenido" + " " * 300 + "\nUno 1\nDos 2\nTres 3\nCuatro 4\nTexto real del documento."
        self.assertEqual(chunker.strip_toc(text), text)

    def test_dot_leaders_still_recognised_on_normal_lines(self):
        text = "\n".join(["Objetivo .......... 3", "Alcance ........ 4", "Procedimiento ......... 5", "Anexos . . . . . 9",
                          "Texto real del documento que sí importa."])
        self.assertEqual(chunker.strip_toc(text), "Texto real del documento que sí importa.")

    def test_dot_leader_detector_matches_the_original_pattern(self):
        original = re.compile(r"\S.*(?:\.\s?){3,}\s*\d{1,4}\s*\Z")
        rng = random.Random(5)
        alphabet = [".", " ", "1", "a", "2", "  ", "\t", "-", "99999"]
        for unused in range(20000):
            line = "".join(rng.choice(alphabet) for unused in range(rng.randint(0, 14)))
            self.assertEqual(chunker._is_dotted(line), bool(original.fullmatch(line.rstrip())), repr(line))


if __name__ == "__main__":
    unittest.main(verbosity=2)
