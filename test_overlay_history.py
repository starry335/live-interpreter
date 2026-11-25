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
        history, active, version = state.overlay_snapshot()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["translated"], "第一句译文")
        self.assertEqual(history[1]["source"], "第二句")
        self.assertEqual(active, [])
        self.assertGreater(version, 0)

    def test_partial_updates_do_not_create_history_entries(self):
        state = State()
        state.upsert_event("c1", source="正在", translated="", final=False)
        state.upsert_event("c1", source="正在识别", translated="草稿", final=False)
        history, active, _version = state.overlay_snapshot()
        self.assertEqual(history, [])
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["source"], "正在识别")

    def test_reconnect_finalizes_the_open_partial(self):
        state = State()
        state.upsert_event("c1", source="半句", translated="半句译文", final=False)
        state.begin_reconnect()
        state.finish_reconnect()
        history, active, _version = state.overlay_snapshot()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["translated"], "半句译文")
        self.assertEqual(active, [])

    def test_streaming_translation_of_an_older_sentence_stays_visible(self):
        state = State()
        state.upsert_event("old", source="前の文", translated="", final=False)
        state.upsert_event("old", translated="前一句的流式译文", final=False)
        state.upsert_event("new", source="新しい文", translated="", final=False)
        history, active, _version = state.overlay_snapshot()
        blocks, current_is_last = overlay_live_blocks(history, active, 3)
        self.assertEqual(blocks, ["前一句的流式译文", "新しい文"])
        self.assertTrue(current_is_last)
        # The old sentence's streaming translation keeps updating above the
        # new Japanese source instead of disappearing until its final.
        state.upsert_event("old", translated="前一句的流式译文更新", final=False)
        history, active, _version = state.overlay_snapshot()
        blocks, _current = overlay_live_blocks(history, active, 3)
        self.assertEqual(blocks[0], "前一句的流式译文更新")
        self.assertEqual(blocks[-1], "新しい文")
        state.upsert_event("new", translated="新句草稿", final=False)
        history, active, _version = state.overlay_snapshot()
        blocks, _current = overlay_live_blocks(history, active, 3)
        self.assertEqual(blocks[-1], "新句草稿")


class BlockTests(unittest.TestCase):
    @staticmethod
    def history(count: int, prefix: str = "第") -> list:
        return [
            {"chunk": f"s{i}", "order": i + 1,
             "source": f"s{i}", "translated": f"{prefix}{i}句已经确定了完整的中文字幕内容。"}
            for i in range(count)
        ]

    def test_live_view_is_two_finals_plus_active_sentence(self):
        history = self.history(5)
        active = [{"chunk": "s5", "order": 6, "source": "s5",
                   "translated": "第六句正在识别", "final": False}]
        blocks, current_is_last = overlay_live_blocks(history, active, max_blocks=3)
        self.assertEqual(blocks, ["第3句已经确定了完整的中文字幕内容。",
                                  "第4句已经确定了完整的中文字幕内容。", "第六句正在识别"])
        self.assertTrue(current_is_last)

    def test_live_view_without_active_shows_three_finals(self):
        blocks, current_is_last = overlay_live_blocks(self.history(5), [], max_blocks=3)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[-1], "第4句已经确定了完整的中文字幕内容。")
        self.assertFalse(current_is_last)

    def test_short_fragments_merge_but_long_sentences_do_not(self):
        merged = merge_short_lines(["这是一个足够长而且完整的句子。", "这是另一个足够长而且完整的句子。"])
        self.assertEqual(merged, ["这是一个足够长而且完整的句子。",
                                  "这是另一个足够长而且完整的句子。"])
        merged = merge_short_lines(["买给我？", "吗？"])
        self.assertEqual(merged, ["买给我，吗？"])

    def test_history_window_walks_back_from_the_newest(self):
        history = [
            {"chunk": f"s{i}", "order": i + 1,
             "translated": f"第{i}句已经确定了完整的历史字幕内容。"}
            for i in range(20)
        ]
        newest = overlay_history_blocks(history, 20, max_blocks=5)
        self.assertEqual(newest, [f"第{i}句已经确定了完整的历史字幕内容。" for i in range(15, 20)])
        older = overlay_history_blocks(history, 10, max_blocks=5)
        self.assertEqual(older, [f"第{i}句已经确定了完整的历史字幕内容。" for i in range(5, 10)])
        oldest = overlay_history_blocks(history, 2, max_blocks=5)
        self.assertEqual(oldest, ["第0句已经确定了完整的历史字幕内容。",
                                  "第1句已经确定了完整的历史字幕内容。"])


if __name__ == "__main__":
    unittest.main()
