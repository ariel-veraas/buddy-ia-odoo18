"""Standalone tests for the vector helpers of the semantic search (no Odoo, no database).

Run from the repo root (tools/validate_local.py does it):
    .venv\\Scripts\\python.exe -m unittest discover -s odoo-addons/buddy_ia/tests -p test_vectors.py
"""
import array
import importlib.util
import math
import random
import sys
import threading
import unittest
from pathlib import Path

MODELS = Path(__file__).resolve().parents[1] / "models"


def load():
    spec = importlib.util.spec_from_file_location("buddy_vectors_under_test", MODELS / "buddy_vectors.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


v = load()


def unit(values):
    return v.normalize(list(values))


def random_vector(rng, dims=16):
    return unit(rng.gauss(0, 1) for _ in range(dims))


class NormalizeAndPackTests(unittest.TestCase):
    def test_normalize_gives_unit_length(self):
        vector = unit([3.0] + [4.0] + [0.0] * 6)
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vector)), 1.0, places=6)
        self.assertAlmostEqual(vector[0], 0.6, places=6)

    def test_normalize_rejects_what_is_not_a_vector(self):
        for bad in (None, "texto", 5, [], [1.0] * 3, [0.0] * 16, [float("nan")] * 16, [float("inf")] * 16,
                    ["a"] * 16, [None] * 16, [1.0] * (v.MAX_DIMS + 1), {"x": 1}, [1e300] * 16):
            with self.assertRaises(ValueError, msg=repr(bad)[:40]):
                v.normalize(bad)

    def test_pack_roundtrip_is_little_endian_float32(self):
        vector = unit(range(1, 17))
        blob = v.pack(vector)
        self.assertEqual(len(blob), 16 * 4)
        self.assertEqual(blob[:4], array.array("f", [vector[0]]).tobytes() if sys.byteorder == "little" else blob[:4])
        again = v.unpack(blob)
        self.assertEqual(list(again), list(vector))

    def test_dot_of_a_unit_vector_with_itself_is_one(self):
        vector = random_vector(random.Random(1), 64)
        self.assertAlmostEqual(v.dot(vector, vector), 1.0, places=5)


class EmbeddingTextTests(unittest.TestCase):
    def test_text_is_context_newline_content(self):
        self.assertEqual(v.embedding_text("PR 46 HelpDesk", "Los tiempos son"), "PR 46 HelpDesk\nLos tiempos son")
        self.assertEqual(v.embedding_text(None, "solo cuerpo"), "\nsolo cuerpo")

    def test_text_is_cut(self):
        self.assertEqual(len(v.embedding_text("c", "x" * 10000)), v.EMBED_TEXT_CHARS)

    def test_hash_is_stable_and_changes_with_the_text(self):
        first = v.text_hash(v.embedding_text("a", "b"))
        self.assertEqual(first, v.text_hash(v.embedding_text("a", "b")))
        self.assertNotEqual(first, v.text_hash(v.embedding_text("a", "c")))
        self.assertEqual(len(first), 64)
        self.assertEqual(v.text_hash("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


class SemanticRankingTests(unittest.TestCase):
    def corpus(self, size=40, dims=32, seed=7):
        rng = random.Random(seed)
        return [(index + 1, 100 + index // 4, index % 4, v.pack(random_vector(rng, dims))) for index in range(size)]

    def test_the_closest_vector_is_first(self):
        rows = self.corpus()
        query = v.unpack(rows[17][3])
        scored = v.score_rows(query, rows, 32)
        self.assertEqual(len(scored), 40)
        best = max(scored)
        self.assertEqual(best[1], 18)
        self.assertAlmostEqual(best[0], 1.0, places=5)

    def test_vectors_of_another_length_are_skipped(self):
        rows = self.corpus(10, 32) + [(99, 1, 0, v.pack(unit(range(1, 17)))), (100, 1, 1, None), (101, 1, 2, b"abc")]
        query = v.unpack(rows[0][3])
        self.assertEqual({item[1] for item in v.score_rows(query, rows, 32)}, set(range(1, 11)))

    def test_select_keeps_only_what_stands_out(self):
        rows = self.corpus(60)
        query = v.unpack(rows[5][3])
        picked = v.select_semantic(v.score_rows(query, rows, 32), 6, min_z=3.0)
        self.assertTrue(picked)
        self.assertEqual(picked[0][1], 6)
        self.assertLess(len(picked), 6, "random vectors: only the identical one is far above the mean")

    def test_select_respects_the_limit_and_the_order(self):
        scored = [(0.9 - index * 0.001, index + 1, 1, 0) for index in range(6)] + [(0.1, 100 + i, 2, 0) for i in range(100)]
        picked = v.select_semantic(scored, 4)
        self.assertEqual([item[1] for item in picked], [1, 2, 3, 4])
        self.assertEqual(v.select_semantic(scored, 0), [])
        self.assertEqual(v.select_semantic([], 5), [])

    def test_small_corpus_uses_only_the_floor(self):
        scored = [(0.9, 1, 1, 0), (0.5, 2, 1, 1), (0.1, 3, 1, 2)]
        self.assertEqual([item[1] for item in v.select_semantic(scored, 6)], [1, 2])

    def test_equal_scores_break_ties_by_chunk_id(self):
        scored = [(0.8, 7, 1, 0), (0.8, 3, 1, 1), (0.8, 5, 1, 2)]
        self.assertEqual([item[1] for item in v.select_semantic(scored, 3)], [3, 5, 7])


class FusionTests(unittest.TestCase):
    def test_a_chunk_found_by_both_searches_wins(self):
        fused = v.rrf([[1, 2, 3], [9, 2, 8]])
        self.assertEqual(fused[0][0], 2)
        self.assertEqual({item[0] for item in fused}, {1, 2, 3, 8, 9})

    def test_scores_follow_the_formula(self):
        fused = dict(v.rrf([[1], [1]], k=60))
        self.assertAlmostEqual(fused[1], 2 / 61)

    def test_one_empty_ranking_leaves_the_other_untouched(self):
        self.assertEqual([item[0] for item in v.rrf([[4, 5, 6], []])], [4, 5, 6])
        self.assertEqual([item[0] for item in v.rrf([[], [4, 5, 6]])], [4, 5, 6])
        self.assertEqual(v.rrf([[], []]), [])

    def test_weights_tilt_the_result(self):
        self.assertEqual(v.rrf([[1], [2]], weights=[1, 3])[0][0], 2)
        self.assertEqual(v.rrf([[1], [2]], weights=[3, 1])[0][0], 1)

    def test_ties_keep_the_order_of_the_first_list(self):
        self.assertEqual([item[0] for item in v.rrf([[1], [2]])], [1, 2])

    def test_a_chunk_only_the_text_search_found_keeps_its_place_among_semantic_ones(self):
        text, meaning = [10, 11, 12], [20, 21, 22]
        order = [item[0] for item in v.rrf([text, meaning])]
        self.assertEqual(order, [10, 20, 11, 21, 12, 22])


class BatchTests(unittest.TestCase):
    def test_batches_respect_both_limits(self):
        items = [(index, "x" * 100) for index in range(10)]
        groups = list(v.batches(items, max_items=4, max_chars=100000))
        self.assertEqual([len(group) for group in groups], [4, 4, 2])
        groups = list(v.batches(items, max_items=100, max_chars=250))
        self.assertEqual([len(group) for group in groups], [2, 2, 2, 2, 2])
        self.assertEqual([key for group in groups for key, _text in group], list(range(10)))

    def test_a_long_text_goes_alone_and_nothing_is_lost(self):
        items = [(1, "a"), (2, "b" * 500), (3, "c")]
        groups = list(v.batches(items, max_items=10, max_chars=100))
        self.assertEqual([[key for key, _ in group] for group in groups], [[1], [2], [3]])
        self.assertEqual(list(v.batches([])), [])


class ParseTests(unittest.TestCase):
    def answer(self, vectors, indexes=None):
        data = []
        for position, vector in enumerate(vectors):
            item = {"object": "embedding", "embedding": list(vector)}
            if indexes is not None:
                item["index"] = indexes[position]
            data.append(item)
        return {"object": "list", "data": data, "model": "m"}

    def test_vectors_come_back_unit_and_in_order(self):
        rng = random.Random(3)
        raw = [[rng.gauss(0, 5) for _ in range(16)] for _ in range(3)]
        parsed = v.parse_embeddings(self.answer(raw), 3)
        for original, got in zip(raw, parsed):
            self.assertAlmostEqual(v.dot(got, got), 1.0, places=5)
            cosine = sum(a * b for a, b in zip(original, got)) / math.sqrt(sum(a * a for a in original))
            self.assertAlmostEqual(cosine, 1.0, places=5)

    def test_gemini_leaves_the_first_index_empty_and_the_position_is_used(self):
        a, b = [1.0] + [0.0] * 15, [0.0, 1.0] + [0.0] * 14
        parsed = v.parse_embeddings(self.answer([a, b], indexes=[None, 1]), 2)
        self.assertAlmostEqual(parsed[0][0], 1.0)
        self.assertAlmostEqual(parsed[1][1], 1.0)

    def test_a_consistent_index_reorders(self):
        a, b = [1.0] + [0.0] * 15, [0.0, 1.0] + [0.0] * 14
        parsed = v.parse_embeddings(self.answer([b, a], indexes=[1, 0]), 2)
        self.assertAlmostEqual(parsed[0][0], 1.0)
        self.assertAlmostEqual(parsed[1][1], 1.0)

    def test_a_repeated_index_falls_back_to_the_position(self):
        a, b = [1.0] + [0.0] * 15, [0.0, 1.0] + [0.0] * 14
        parsed = v.parse_embeddings(self.answer([a, b], indexes=[0, 0]), 2)
        self.assertAlmostEqual(parsed[0][0], 1.0)

    def test_malformed_answers_are_refused(self):
        good = [1.0] + [0.0] * 15
        for bad in (None, [], {}, {"data": None}, {"data": []}, {"data": [{"embedding": good}]},  # one for two
                    {"data": [{"embedding": good}, {"embedding": good[:9]}]},  # mixed lengths
                    {"data": [{"embedding": good}, {"embedding": "texto"}]}, {"data": [{"embedding": good}, 7]},
                    {"data": [{"embedding": good}, {"nothing": 1}]}):
            with self.assertRaises(ValueError, msg=repr(bad)[:50]):
                v.parse_embeddings(bad, 2)


class CacheAndBreakerTests(unittest.TestCase):
    def test_query_key_ignores_case_and_spacing_and_never_holds_the_text(self):
        self.assertEqual(v.query_key("  ¿Cuánto   TARDA? "), v.query_key("¿cuánto tarda?"))
        self.assertNotEqual(v.query_key("uno"), v.query_key("dos"))
        key = v.query_key("la escala salarial del gerente")
        self.assertEqual(len(key), 64)
        self.assertNotIn("salarial", key)
        self.assertEqual(v.query_key("a" * 5000), v.query_key("a" * v.QUERY_TEXT_CHARS))

    def test_cache_entries_expire(self):
        now = [0.0]
        cache = v.QueryCache(size=5, ttl=100, clock=lambda: now[0])
        cache.put("k", "vector")
        now[0] = 99
        self.assertEqual(cache.get("k"), "vector")
        now[0] = 100
        self.assertIsNone(cache.get("k"))
        self.assertEqual(len(cache), 0)

    def test_cache_is_a_bounded_lru(self):
        cache = v.QueryCache(size=3)
        for key in "abc":
            cache.put(key, key.upper())
        self.assertEqual(cache.get("a"), "A")  # "a" is now the freshest
        cache.put("d", "D")
        self.assertIsNone(cache.get("b"))
        self.assertEqual((cache.get("a"), cache.get("c"), cache.get("d")), ("A", "C", "D"))
        self.assertEqual(len(cache), 3)
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_cache_survives_threads(self):
        cache = v.QueryCache(size=50)

        def work(offset):
            for index in range(500):
                cache.put((offset, index % 80), index)
                cache.get((offset, (index * 7) % 80))

        threads = [threading.Thread(target=work, args=(number,)) for number in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertLessEqual(len(cache), 50)

    def test_breaker_closes_after_repeated_failures_and_reopens(self):
        now = [100.0]
        breaker = v.Breaker(limit=3, pause=60, clock=lambda: now[0])
        self.assertTrue(breaker.allowed("k"))
        breaker.failure("k")
        breaker.failure("k")
        self.assertTrue(breaker.allowed("k"))
        breaker.failure("k")
        self.assertFalse(breaker.allowed("k"))
        self.assertTrue(breaker.allowed("other"))
        now[0] += 59
        self.assertFalse(breaker.allowed("k"))
        now[0] += 2
        self.assertTrue(breaker.allowed("k"))  # one more try after the pause
        breaker.failure("k")
        self.assertFalse(breaker.allowed("k"))  # and it closes again at once
        breaker.success("k")
        self.assertTrue(breaker.allowed("k"))

    def test_after_the_pause_only_one_probe_goes_through(self):
        now = [0.0]
        breaker = v.Breaker(limit=2, pause=60, clock=lambda: now[0], probe=10)
        breaker.failure("k")
        breaker.failure("k")
        self.assertFalse(breaker.allowed("k"))
        now[0] = 61
        self.assertTrue(breaker.allowed("k"), "the probe")
        self.assertFalse(breaker.allowed("k"), "everybody else waits for its verdict")
        now[0] = 72
        self.assertTrue(breaker.allowed("k"), "the probe never answered: another one after the probe time")
        breaker.success("k")
        self.assertTrue(breaker.allowed("k"))
        self.assertTrue(breaker.allowed("k"))

    def test_a_success_resets_the_count(self):
        breaker = v.Breaker(limit=2, pause=60, clock=lambda: 0.0)
        breaker.failure("k")
        breaker.success("k")
        breaker.failure("k")
        self.assertTrue(breaker.allowed("k"))


class CapacityTests(unittest.TestCase):
    def test_the_scan_capacity_follows_the_vector_size_with_a_hard_ceiling(self):
        self.assertEqual(v.scan_capacity(768), 16_000_000 // 768)
        self.assertEqual(v.scan_capacity(3072), 16_000_000 // 3072)
        self.assertEqual(v.scan_capacity(8), v.MAX_SCAN_CHUNKS, "a tiny vector does not allow millions of fragments")
        self.assertEqual(v.scan_capacity(0), v.MAX_SCAN_CHUNKS)


class SpeedTests(unittest.TestCase):
    def test_scoring_ten_thousand_vectors_is_fast_enough(self):
        rng = random.Random(5)
        dims = 256
        base = [v.pack(random_vector(rng, dims)) for _ in range(200)]
        rows = [(index + 1, index, 0, base[index % 200]) for index in range(10000)]
        query = random_vector(rng, dims)
        import time
        started = time.perf_counter()
        scored = v.score_rows(query, rows, dims)
        v.select_semantic(scored, 24)
        self.assertEqual(len(scored), 10000)
        self.assertLess(time.perf_counter() - started, 3.0)


if __name__ == "__main__":
    unittest.main()
