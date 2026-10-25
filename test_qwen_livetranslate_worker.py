import unittest
from qwen_livetranslate_worker import MODEL_38, ResultNormalizer, event_text, image_event, parse_args, session_config


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
            parse_args(["--source-language", "auto"])
        )
        self.assertEqual(config["modalities"], ["text"])
        self.assertNotIn("output_audio_format", config)
        self.assertNotIn("language", config["input_audio_transcription"])

    def test_qwen38_requests_text_only_and_speaker_detection(self):
        config = session_config(parse_args(["--model", MODEL_38]))
        self.assertEqual(config, {
            "output_modalities": ["text"],
            "translation": {"language": "zh"},
            "audio": {"input": {"turn_detection": {"type": "speaker_detection", "threshold": 0.5}}},
        })

    def test_qwen38_deltas_accumulate_and_interleaved_sentences_stay_paired(self):
        normalizer = ResultNormalizer()
        normalizer.handle({"type": "input_audio_buffer.speech_started", "item_id": "source-1"})
        source = normalizer.handle({"type": "conversation.item.input_audio_transcription.delta",
                                    "item_id": "source-1", "delta": "こん"})[0]
        source = normalizer.handle({"type": "conversation.item.input_audio_transcription.delta",
                                    "item_id": "source-1", "delta": "にちは"})[0]
        self.assertEqual(source["source"], "こんにちは")
        normalizer.handle({"type": "input_audio_buffer.speech_started", "item_id": "source-2"})
        source2 = normalizer.handle({"type": "conversation.item.input_audio_transcription.delta",
                                     "item_id": "source-2", "delta": "ありがとう"})[0]
        self.assertNotEqual(source["sentence_id"], source2["sentence_id"])
        for index, deltas in ((1, ("大", "家好")), (2, ("谢", "谢"))):
            response_id, item_id = f"response-{index}", f"translation-{index}"
            normalizer.handle({"type": "response.created", "response": {"id": response_id}})
            normalizer.handle({"type": "conversation.item.created", "previous_item_id": f"source-{index}",
                               "item": {"id": item_id, "role": "assistant"}})
            for delta in deltas:
                translated = normalizer.handle({"type": "response.text.delta", "response_id": response_id,
                                                "item_id": item_id, "delta": delta})[0]
            self.assertEqual(translated["translation"], "".join(deltas))
            self.assertEqual(translated["sentence_id"], str(index))
            self.assertFalse(translated["translation_final"])
            final = normalizer.handle({"type": "response.text.done", "response_id": response_id,
                                       "item_id": item_id, "text": "".join(deltas) + "。"})[0]
            self.assertEqual(final["translation"], "".join(deltas) + "。")
            self.assertTrue(final["translation_final"])
            source_final = normalizer.handle({"type": "conversation.item.input_audio_transcription.completed",
                                              "item_id": f"source-{index}", "transcript": "原文完成"})[0]
            self.assertEqual(source_final["sentence_id"], str(index))
            self.assertTrue(source_final["source_final"])
        self.assertEqual(normalizer.source_text, {})
        self.assertEqual(normalizer.translation_text, {})

    def test_visual_frame_uses_image_buffer_event(self):
        event = image_event(b"\xff\xd8jpeg\xff\xd9")
        self.assertEqual(event["type"], "input_image_buffer.append")
        self.assertEqual(event["image"], "/9hq cGVn/9k=".replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
