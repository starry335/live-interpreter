"""Prompt building for the translation stage (force / reference / icl)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import hotwords


class BuildSystemPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.terms = [hotwords.Term("ゆのち", "由乃亲"), hotwords.Term("夢見た", "梦限大")]
        self.attrs = {"ゆのち": "人名", "夢見た": "乐队名；同音普通词很常见"}

    def test_without_terms_returns_base_prompt_only(self) -> None:
        prompt = hotwords.build_system_prompt("icl", [], attributes=self.attrs)
        self.assertIn("Return ONLY the translation of current_text", prompt)
        self.assertNotIn("Name glossary", prompt)
        self.assertNotIn("Examples of the judgement", prompt)

    def test_icl_prompt_has_cards_examples_and_rules(self) -> None:
        prompt = hotwords.build_system_prompt("icl", self.terms, attributes=self.attrs)
        self.assertIn("ゆのち => 由乃亲  [人名]", prompt)
        self.assertIn("夢見た => 梦限大", prompt)
        self.assertIn("同音普通词", prompt)
        for source, _target, _note in hotwords.ICL_EXAMPLES:
            self.assertIn(source, prompt)
        self.assertIn("do not write the name", prompt)

    def test_icl_prompt_is_capped(self) -> None:
        many = [hotwords.Term(f"term{index}", f"目标{index}") for index in range(2000)]
        prompt = hotwords.build_system_prompt("icl", many)
        self.assertLessEqual(len(prompt), hotwords.ICL_PROMPT_LIMIT)

    def test_reference_prompt_keeps_cautious_wording(self) -> None:
        prompt = hotwords.build_system_prompt("reference", self.terms)
        self.assertIn("Reference spellings", prompt)
        self.assertIn("Never insert a name", prompt)
        self.assertNotIn("Name glossary", prompt)

    def test_force_prompt_keeps_legacy_wording(self) -> None:
        prompt = hotwords.build_system_prompt("force", self.terms)
        self.assertIn("always use the target", prompt)

    def test_unknown_mode_falls_back_to_force(self) -> None:
        prompt = hotwords.build_system_prompt("something-else", self.terms)
        self.assertIn("always use the target", prompt)


class NameCardTests(unittest.TestCase):
    def test_cards_include_note_when_present(self) -> None:
        text = hotwords.name_cards([hotwords.Term("ゆのち", "由乃亲")], {"ゆのち": "人名"})
        self.assertEqual(text, "- ゆのち => 由乃亲  [人名]")

    def test_cards_omit_note_when_absent(self) -> None:
        text = hotwords.name_cards([hotwords.Term("ゆのち", "由乃亲")])
        self.assertEqual(text, "- ゆのち => 由乃亲")

    def test_examples_are_contrastive(self) -> None:
        texts = " ".join(note for _source, _target, note in hotwords.ICL_EXAMPLES)
        self.assertIn("普通词", texts)
        self.assertTrue(any("真正的" in note or "昵称" in note for _s, _t, note in hotwords.ICL_EXAMPLES))


class LoadAttributesTests(unittest.TestCase):
    def test_loads_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cards.json"
            path.write_text(json.dumps({"ゆのち": "人名"}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(hotwords.load_attributes(str(path)), {"ゆのち": "人名"})

    def test_empty_path_returns_empty_mapping(self) -> None:
        self.assertEqual(hotwords.load_attributes(""), {})

    def test_rejects_non_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cards.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(hotwords.HotwordError):
                hotwords.load_attributes(str(path))

    def test_rejects_broken_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cards.json"
            path.write_text("{oops", encoding="utf-8")
            with self.assertRaises(hotwords.HotwordError):
                hotwords.load_attributes(str(path))


class Icl2PromptTests(unittest.TestCase):
    def test_homophone_notes_only_for_ambiguous_surfaces(self) -> None:
        terms = [hotwords.Term("夢見た", "梦限大"), hotwords.Term("アフターグロウ", "Afterglow")]
        notes = hotwords.homophone_notes(terms)
        self.assertIn("夢見た", notes)
        self.assertNotIn("アフターグロウ", notes)

    def test_icl2_prompt_includes_homophone_section_and_extra_examples(self) -> None:
        prompt = hotwords.build_system_prompt("icl2", [hotwords.Term("夢見た", "梦限大")])
        self.assertIn("Words that are usually ordinary", prompt)
        self.assertIn("迷子", prompt)
        self.assertIn("Fix obvious ASR mishearings", prompt)
        self.assertLessEqual(len(prompt), hotwords.ICL_PROMPT_LIMIT)

    def test_icl_prompt_without_ambiguous_terms_has_no_homophone_section(self) -> None:
        prompt = hotwords.build_system_prompt("icl2", [hotwords.Term("アフターグロウ", "Afterglow")])
        self.assertNotIn("Words that are usually ordinary", prompt)


if __name__ == "__main__":
    unittest.main()
