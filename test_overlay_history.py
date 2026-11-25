# -*- coding: utf-8 -*-
"""Overlay history, live blocks and scroll-window tests."""
from __future__ import annotations

import unittest

from live_interpreter import (
    State, merge_short_lines, overlay_history_blocks, overlay_live_blocks,
)


class StateHistoryTests(unittest.TestCase):
    def test_source_and_translation_final_collapse_into_one_history_line(self):
        state = State()
        state.upsert_event("c1", source="第一句", translated="", final=True)
        state.upsert_event("c1", translated="第一句译文", final=True)
        state.upsert_event("c2", source="第二句", translated="第二句译文", final=True)
        history, current, version = state.overlay_snapshot()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["translated"], "第一句译文")
        self.assertEqual(history[1]["source"], "第二句")
        self.assertIsNone(current)
        self.assertGreater(version, 0)

    def test_partial_updates_do_not_create_history_entries(self):
        state = State()
        state.upsert_event("c1", source="正在", translated="", final=False)
        state.upsert_event("c1", source="正在识别", translated="草稿", final=False)
        history, current, _version = state.overlay_snapshot()
        self.assertEqual(history, [])
        self.assertEqual(current["source"], "正在识别")

    def test_reconnect_finalizes_the_open_partial(self):
        state = State()
        state.upsert_event("c1", source="半句", translated="半句译文", final=False)
        state.begin_reconnect()
        state.finish_reconnect()
        history, current, _version = state.overlay_snapshot()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["translated"], "半句译文")


class BlockTests(unittest.TestCase):
    def test_live_view_is_two_finals_plus_current(self):
        history = [
            {"source": f"s{i}", "translated": f"第{i}句已确定内容。"}
            for i in range(5)
        ]
        current = {"source": "s5", "translated": "第六句正在识别", "final": False}
        blocks = overlay_live_blocks(history, current, max_blocks=3)
        self.assertEqual(blocks, ["第3句已确定内容。", "第4句已确定内容。", "第六句正在识别"])

    def test_live_view_without_current_shows_three_finals(self):
        history = [
            {"source": f"s{i}", "translated": f"第{i}句已确定内容。"}
            for i in range(5)
        ]
        blocks = overlay_live_blocks(history, None, max_blocks=3)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[-1], "第4句已确定内容。")

    def test_short_fragments_merge_but_sentences_do_not(self):
        merged = merge_short_lines(["这是一个完整的句子。", "律。", "下一句也要独立。"])
        self.assertEqual(merged, ["这是一个完整的句子。", "律。", "下一句也要独立。"])
        merged = merge_short_lines(["买给我？", "吗？"])
        self.assertEqual(merged, ["买给我，吗？"])

    def test_history_window_walks_back_from_the_newest(self):
        history = [
            {"source": f"s{i}", "translated": f"第{i}句已经确定的内容。"}
            for i in range(20)
        ]
        newest = overlay_history_blocks(history, 20, max_blocks=5)
        self.assertEqual(newest, [f"第{i}句已经确定的内容。" for i in range(15, 20)])
        older = overlay_history_blocks(history, 10, max_blocks=5)
        self.assertEqual(older, [f"第{i}句已经确定的内容。" for i in range(5, 10)])
        oldest = overlay_history_blocks(history, 2, max_blocks=5)
        self.assertEqual(oldest, ["第0句已经确定的内容。", "第1句已经确定的内容。"])


if __name__ == "__main__":
    unittest.main()
