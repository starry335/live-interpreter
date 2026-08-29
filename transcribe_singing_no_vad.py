"""Transcribe a song without discarding sung audio as non-speech."""
import json
from pathlib import Path

import torch
import whisperx
from faster_whisper import WhisperModel

ROOT = Path(r"E:\tool-live")
AUDIO = r"D:\Desktop\素材\8月5日(9).mp3"
WHISPER = ROOT / "models" / "whisperx" / "models--Systran--faster-whisper-large-v3" / "snapshots" / "edaa852ec7e145841d8ffdb056a99866b5f0a478"
ALIGN = ROOT / "models" / "whisperx" / "align-ja-ivy"
OUT = ROOT / "outputs" / "8月5日(9)_whisperx_novad.json"
PROMPT = "キミがいないと何もできないよ。キミのごはんが食べたいよ。もしキミが帰ってきたら、とびっきりの笑顔で抱きつくよ。"

def main():
    model = WhisperModel(str(WHISPER), device="cuda", compute_type="float16")
    segments, _ = model.transcribe(AUDIO, language="ja", beam_size=5, vad_filter=False, initial_prompt=PROMPT, condition_on_previous_text=True)
    transcript = [{"start": segment.start, "end": segment.end, "text": segment.text.strip()} for segment in segments if segment.text.strip()]
    del model
    torch.cuda.empty_cache()
    align_model, metadata = whisperx.load_align_model("ja", "cuda", model_name=str(ALIGN), model_cache_only=True)
    aligned = whisperx.align(transcript, align_model, metadata, AUDIO, "cuda", return_char_alignments=False)
    result = {"language": "ja", "segments": aligned["segments"], "word_segments": aligned["word_segments"]}
    assert result["word_segments"]
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"segments": len(transcript), "words": len(result["word_segments"]), "output": str(OUT)}, ensure_ascii=False))

if __name__ == "__main__":
    main()
