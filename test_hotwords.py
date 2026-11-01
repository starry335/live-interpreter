import io
import json
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import hotwords


SAMPLE = """
# leading comment
[hotwords]
あられ => 阿拉蕾
ユノ
~戦国ユーノ => 由乃
!战国由乃 => 由乃

[forbidden]
拉卡马奇奥尔雷 => 阿拉蕾
"""


class HotwordFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_text_format_routes_terms_to_the_right_stage(self):
        config = hotwords.load(self.write("terms.txt", SAMPLE))
        self.assertEqual([(t.source, t.target) for t in config.pairs], [("あられ", "阿拉蕾")])
        self.assertEqual(config.asr_only, ["ユノ"])
        self.assertEqual([(t.source, t.target) for t in config.aliases], [("戦国ユーノ", "由乃")])
        self.assertEqual(
            [(t.source, t.target) for t in config.forbidden],
            [("战国由乃", "由乃"), ("拉卡马奇奥尔雷", "阿拉蕾")],
        )
        self.assertEqual(config.asr_terms(), ["あられ", "ユノ"])
        self.assertIn("戦国ユーノ", [term.source for term in config.mt_terms()])
        self.assertNotIn("戦国ユーノ", config.asr_terms())
        self.assertEqual(config.postprocess("战国由乃和拉卡马奇奥尔雷"), "由乃和阿拉蕾")
        self.assertEqual(config.postprocess("正常文本"), "正常文本")

    def test_sections_and_derived_forbidden_target(self):
        path = self.write(
            "sections.txt",
            "[hotwords]\n千石ユノ => 由乃\n[asr]\nユノ\n"
            "[aliases]\n戦国ユーノ => 由乃\n[forbidden]\n战国由乃\n",
        )
        config = hotwords.load(path)
        self.assertEqual(config.asr_terms(), ["千石ユノ", "ユノ"])
        self.assertEqual([(t.source, t.target) for t in config.forbidden], [("战国由乃", "由乃")])

    def test_duplicate_source_keeps_first_and_warns(self):
        path = self.write("dup.txt", "あられ => 阿拉蕾\nあられ => 亚蕾\n")
        config = hotwords.load(path)
        self.assertEqual([(t.source, t.target) for t in config.pairs], [("あられ", "阿拉蕾")])
        self.assertTrue(any("duplicate" in warning for warning in config.warnings))

    def test_asr_length_and_hash_filter(self):
        long_term = "あ" * 16
        path = self.write("limits.txt", f"あられ => 阿拉蕾\n{long_term} => 长词\nタグ# => 标签\n")
        config = hotwords.load(path)
        self.assertEqual(config.asr_terms(), ["あられ"])
        self.assertNotIn("タグ#", config.asr_terms())
        self.assertIn(long_term, [term.source for term in config.mt_terms()])

    def test_kb_terms_json_is_accepted(self):
        path = self.root / "kb_terms.json"
        path.write_text(json.dumps({
            "schema": "kb-terms/v1",
            "person": {"あられ": "阿拉蕾", "戦国ユーノ": "由乃"},
            "honorific": {"ゆのち": "由乃亲"},
            "forbidden": ["战国由乃", "拉卡马奇奥尔雷"],
        }, ensure_ascii=False), encoding="utf-8")
        config = hotwords.load(path)
        self.assertEqual(
            [(t.source, t.target) for t in config.pairs],
            [("あられ", "阿拉蕾"), ("戦国ユーノ", "由乃"), ("ゆのち", "由乃亲")],
        )
        self.assertEqual([(t.source, t.target) for t in config.forbidden], [("战国由乃", "由乃")])
        self.assertTrue(any("拉卡马奇奥尔雷" in warning for warning in config.warnings))

    def test_select_mt_terms_uses_only_sentence_terms_longest_first(self):
        path = self.write(
            "select.txt",
            "ゆの => 由乃\nゆのち => 由乃亲\nあられ => 阿拉蕾\n戦国ユーノ => 由乃\n",
        )
        config = hotwords.load(path)
        self.assertEqual(
            [(t.source, t.target) for t in config.select_mt_terms("ゆのちと一緒に")],
            [("ゆのち", "由乃亲")],
        )
        self.assertEqual(
            [t.source for t in config.select_mt_terms("はい戦国ユーノです")],
            ["戦国ユーノ"],
        )
        self.assertEqual(config.select_mt_terms("今日は晴れ"), [])

    def test_single_character_surface_is_not_applied(self):
        config = hotwords.load(self.write("single.txt", "律 => 律\nりつ => 律\n"))
        self.assertEqual(config.select_mt_terms("旋律が好き"), [])
        self.assertEqual([t.source for t in config.select_mt_terms("りつが好き")], ["りつ"])

    def test_prompt_block_is_bounded(self):
        lines = "\n".join(f"語{i} => 词{i}" for i in range(300))
        config = hotwords.load(self.write("many.txt", lines))
        block = config.prompt_block(max_chars=400)
        self.assertLessEqual(len(block), 420)
        self.assertTrue(block.startswith("Terminology"))

    def test_repository_example_keeps_short_names_and_skips_asr_miswrites(self):
        path = Path(__file__).resolve().parent / "hotwords" / "yumemita_bangdream.txt"
        config = hotwords.load(path)
        self.assertEqual(config.warnings, [])
        self.assertIn("あられ", config.asr_terms())
        self.assertIn("ゆのち", config.asr_terms())
        self.assertNotIn("戦国ユーノ", config.asr_terms())
        self.assertIn("戦国ユーノ", [term.source for term in config.mt_terms()])
        self.assertIn("ラカマチオルレ", [term.source for term in config.mt_terms()])
        self.assertEqual(config.postprocess("我是战国由乃"), "我是由乃")
        # A Chinese target must not leave Japanese kana in the subtitle. This
        # caught the offline kb_terms typo "のの酱".
        kana = re.compile(r"[ぁ-ゖァ-ヺ]")
        for term in config.mt_terms():
            self.assertFalse(kana.search(term.target), term)

    def test_invalid_files_fail_fast(self):
        with self.assertRaises(hotwords.HotwordError):
            hotwords.load(self.root / "missing.txt")
        with self.assertRaises(hotwords.HotwordError):
            hotwords.load(self.root)
        bad = self.root / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        with self.assertRaises(hotwords.HotwordError):
            hotwords.load(bad)

    def test_saved_memory_and_equals_text_are_compatible(self):
        path = self.write("memory.json", json.dumps({"enabled": True, "terms": {"山田リョウ": "山田凉"}}))
        self.assertEqual(hotwords.load(path).translation_phrases()["山田リョウ"], "山田凉")
        self.assertEqual(hotwords.load(path).translation_phrases()["あられ"], "阿拉蕾")
        path.write_text(json.dumps({"enabled": False, "terms": {"山田リョウ": "山田凉"}}), encoding="utf-8")
        self.assertEqual(hotwords.load(path).asr_terms(), [])
        self.assertEqual(hotwords.load(path).translation_phrases(), {})
        config = hotwords.load(self.write("equals.txt", "山田リョウ = 山田凉\n"))
        self.assertEqual(config.translation_phrases(), {"山田リョウ": "山田凉"})

    def test_builtin_terms_merge_with_custom_overrides_without_losing_aliases(self):
        path = self.write("memory.json", json.dumps({
            "enabled": True, "terms": {"あられ": "自定义阿拉蕾", "山田リョウ": "山田凉"},
        }))
        config = hotwords.load(path)
        self.assertEqual(config.translation_phrases()["あられ"], "自定义阿拉蕾")
        self.assertEqual(config.translation_phrases()["山田リョウ"], "山田凉")
        self.assertIn("戦国ユーノ", config.translation_phrases())
        self.assertNotIn("戦国ユーノ", config.asr_terms())
        self.assertEqual(config.postprocess("我是战国由乃"), "我是由乃")

    def test_builtin_can_be_disabled_without_losing_custom_terms(self):
        path = self.write("memory.json", json.dumps({
            "enabled": True, "use_builtin": False, "terms": {"山田リョウ": "山田凉"},
        }))
        self.assertEqual(hotwords.load(path).translation_phrases(), {"山田リョウ": "山田凉"})

    def test_ascii_asr_limit_is_word_count_not_character_count(self):
        config = hotwords.load(self.write("ascii.txt", "Live Interpreter Translation Project\none two three four five six seven eight\n"))
        self.assertEqual(config.asr_terms(), ["Live Interpreter Translation Project"])


class VocabularyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / "cache.json"

    def test_creates_then_reuses_cache(self):
        calls = []

        def post(url, api_key, payload, timeout, ssl_context=None):
            calls.append(payload["input"]["action"])
            if payload["input"]["action"] == "list_vocabulary":
                return {"output": {"vocabulary_list": []}}
            return {"output": {"vocabulary_id": "vocab-test-1"}, "usage": {"count": 1}}

        with mock.patch.object(hotwords, "_post_json", side_effect=post):
            first = hotwords.ensure_vocabulary("key", "ws", ["あられ", "ゆのち"], cache_path=self.cache)
            second = hotwords.ensure_vocabulary("key", "ws", ["あられ", "ゆのち"], cache_path=self.cache)
        self.assertEqual(first, "vocab-test-1")
        self.assertEqual(second, "vocab-test-1")
        self.assertEqual(calls, ["list_vocabulary", "create_vocabulary"])

    def test_lost_cache_reuses_existing_list(self):
        calls = []

        def post(url, api_key, payload, timeout, ssl_context=None):
            calls.append(payload["input"]["action"])
            return {"output": {"vocabulary_list": [
                {"vocabulary_id": "vocab-existing", "status": "OK"},
            ]}}

        with mock.patch.object(hotwords, "_post_json", side_effect=post):
            vocabulary_id = hotwords.ensure_vocabulary("key", "ws", ["あられ"], cache_path=self.cache)
        self.assertEqual(vocabulary_id, "vocab-existing")
        self.assertEqual(calls, ["list_vocabulary"])
        entries = json.loads(self.cache.read_text())["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(next(iter(entries.values()))["vocabulary_id"], "vocab-existing")

    def test_creation_error_is_a_hotword_error(self):
        error = urllib.error.HTTPError(
            "https://example.invalid", 400, "Bad Request", {}, io.BytesIO(b"bad vocabulary")
        )

        def post(url, api_key, payload, timeout, ssl_context=None):
            if payload["input"]["action"] == "list_vocabulary":
                return {"output": {}}
            raise error

        with mock.patch.object(hotwords, "_post_json", side_effect=post):
            with self.assertRaisesRegex(hotwords.HotwordError, "create_vocabulary failed"):
                hotwords.ensure_vocabulary("key", "ws", ["あられ"], cache_path=self.cache)

    def test_empty_terms_fail_before_network(self):
        with self.assertRaises(hotwords.HotwordError):
            hotwords.ensure_vocabulary("key", "ws", [], cache_path=self.cache)

    def test_cache_is_order_independent_but_separated_by_key(self):
        with mock.patch.object(hotwords, "_post_json", return_value={"output": {"vocabulary_id": "vocab-A"}}) as post:
            hotwords.ensure_vocabulary("account-A", "ws", ["ゆのち", "あられ", "あられ"], cache_path=self.cache)
            calls = post.call_count
            hotwords.ensure_vocabulary("account-A", "ws", ["あられ", "ゆのち"], cache_path=self.cache)
            self.assertEqual(post.call_count, calls)
        with mock.patch.object(hotwords, "_post_json", return_value={"output": {"vocabulary_id": "vocab-B"}}) as post:
            result = hotwords.ensure_vocabulary("account-B", "ws", ["あられ", "ゆのち"], cache_path=self.cache)
            self.assertTrue(post.called)
            self.assertEqual(result, "vocab-B")
        content = self.cache.read_text(encoding="utf-8")
        self.assertNotIn("account-A", content)
        self.assertNotIn("account-B", content)

    def test_corrupt_cache_does_not_disable_hotwords(self):
        self.cache.write_bytes(b"\xff\xfeinvalid-utf8")
        with mock.patch.object(hotwords, "_post_json", return_value={"output": {"vocabulary_id": "vocab-recovered"}}):
            self.assertEqual(hotwords.ensure_vocabulary("key", "ws", ["あられ"], cache_path=self.cache), "vocab-recovered")


if __name__ == "__main__":
    unittest.main()
