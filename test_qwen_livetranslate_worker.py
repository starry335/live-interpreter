import unittest
from argparse import Namespace

from qwen_livetranslate_worker import ResultNormalizer, event_text, image_event, session_config


class ResultNormalizerTests(unittest.TestCase):
    def test_source_and_translation_share_vad_sentence(self):
        normalizer = ResultNormalizer()
        normalizer.handle({"type": "input_audio_buffer.speech_started"})

        source = normalizer.handle(
            {
                "type": "conversation.item.input_audio_transcription.text",
                "item_id": "source-1",
                "text": "こんにちは",
                "stash": "、皆さん",
            }
        )[0]
        normalizer.handle({"type": "response.created", "response": {"id": "response-1"}})
        translation = normalizer.handle(
            {
                "type": "response.text.text",
                "response_id": "response-1",
                "text": "大家好",
            }
        )[0]
        final = normalizer.handle(
            {
                "type": "response.text.done",
                "response_id": "response-1",
                "text": "大家好。",
            }
        )[0]

        self.assertEqual(source["sentence_id"], translation["sentence_id"])
        self.assertEqual(source["source"], "こんにちは、皆さん")
        self.assertFalse(translation["translation_final"])
        self.assertTrue(final["translation_final"])

    def test_prediction_stash_is_replaceable_preview(self):
        self.assertEqual(event_text({"text": "已经确认", "stash": "预测"}), "已经确认预测")

    def test_session_requests_text_only_and_allows_auto_language(self):
        config = session_config(
            Namespace(source_language="auto", target_language="zh", silence_duration_ms=500)
        )
        self.assertEqual(config["modalities"], ["text"])
        self.assertNotIn("output_audio_format", config)
        self.assertNotIn("language", config["input_audio_transcription"])

    def test_visual_frame_uses_image_buffer_event(self):
        event = image_event(b"\xff\xd8jpeg\xff\xd9")
        self.assertEqual(event["type"], "input_image_buffer.append")
        self.assertEqual(event["image"], "/9hq cGVn/9k=".replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
