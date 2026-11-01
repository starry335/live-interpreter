import tempfile
import unittest
from pathlib import Path

import hotwords
from tools import eval_hotwords


class EvalMetricTests(unittest.TestCase):
    def test_chrf_identical_and_empty(self):
        self.assertEqual(eval_hotwords.chrf("今天很开心", "今天很开心"), 1.0)
        self.assertEqual(eval_hotwords.chrf("", "今天很开心"), 0.0)
        self.assertGreater(eval_hotwords.chrf("今天很开心", "今天很累"), 0.3)

    def test_name_hits_follow_canonical_aliases(self):
        self.assertEqual(eval_hotwords.expected_names("我是仲町阿拉蕾"), ["阿拉蕾"])
        self.assertTrue(eval_hotwords.name_hit("阿拉蕾", "我是阿拉蕾"))
        self.assertFalse(eval_hotwords.name_hit("阿拉蕾", "我是亚蕾"))

    def test_selection_skips_misaligned_short_cues(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "h.txt"
            path.write_text("あられ => 阿拉蕾\nゆのち => 由乃亲\n", encoding="utf-8")
            config = hotwords.load(path)
        rows = [
            {"id": "ok", "source": "じゃああられか", "reference": "那就阿拉蕾吧", "human_gold": True},
            {"id": "short", "source": "っ", "reference": "谢谢由乃酱", "human_gold": False},
            {"id": "misaligned", "source": "愛これが毛", "reference": "由乃酱喜欢的长句子" * 4, "human_gold": False},
            {"id": "general", "source": "今日はとても楽しかったです", "reference": "今天非常开心", "human_gold": False},
        ]
        named, general = eval_hotwords.select_rows(rows, config, 10, 10, 1)
        self.assertEqual([row["id"] for row in named], ["ok"])
        self.assertEqual([row["id"] for row in general], ["general"])

    def test_source_name_map_uses_target_canonical_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "h.txt"
            path.write_text("戦国ユーノ => 由乃\nあられ => 阿拉蕾\n", encoding="utf-8")
            config = hotwords.load(path)
        mapping = eval_hotwords.source_name_map(config)
        self.assertIn("戦国ユーノ", mapping)
        self.assertEqual(mapping["戦国ユーノ"], "由乃")


if __name__ == "__main__":
    unittest.main()
