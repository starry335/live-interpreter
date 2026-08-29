"""Force-align the sung U&I lyrics to the known vocal passages."""
from pathlib import Path
import json
import whisperx

ROOT = Path(r"E:\tool-live")
AUDIO = r"D:\Desktop\素材\8月5日(9).mp3"
OUT = ROOT / "outputs" / "8月5日(9)_中日双语.srt"
MODEL = ROOT / "models" / "whisperx" / "align-ja-ivy"

STANZAS = [
    (61.0, 131.2, [
        ("キミがいないと何もできないよ", "没有你，我什么也做不了。"),
        ("キミのごはんが食べたいよ", "好想吃你做的饭。"),
        ("もしキミが帰ってきたら", "如果你回来了，"),
        ("とびっきりの笑顔で抱きつくよ", "我会带着最灿烂的笑容紧紧抱住你。"),
        ("キミがいないと謝れないよ", "没有你，我连道歉也做不到。"),
        ("キミの声が聞きたいよ", "好想听见你的声音。"),
        ("キミの笑顔が見れればそれだけでいいんだよ", "只要能看见你的笑容，我就心满意足。"),
        ("キミがそばにいるだけでいつも勇気もらってた", "只要你在身边，总能带给我勇气。"),
        ("いつまででも一緒にいたい", "我想永远和你在一起。"),
        ("この気持ちを伝えたいよ", "想把这份心意传达给你。"),
        ("晴れの日にも雨の日も", "无论晴天还是雨天，"),
        ("キミはそばにいてくれた", "你都陪在我身边。"),
        ("目を閉じればキミの笑顔輝いてる", "闭上眼，你灿烂的笑容仍在闪耀。"),
    ]),
    (146.4, 182.0, [
        ("キミがいないとなにもわからないよ", "没有你，我什么也搞不懂。"),
        ("砂糖としょうゆはどこだっけ?", "糖和酱油到底放在哪儿来着？"),
        ("もしキミが帰って来たら", "如果你回来了，"),
        ("びっくりさせようと思ったのにな", "我本来想给你一个惊喜的。"),
        ("キミについつい甘えちゃうよ", "可我总是不知不觉依赖着你。"),
        ("キミが優しすぎるから", "因为你实在太温柔了。"),
        ("キミにもらってばかりでなにもあげられてないよ", "我总是从你那里得到，却什么也没能给你。"),
        ("キミがそばにいることを当たり前に思ってた", "我曾以为你在我身边是理所当然的。"),
        ("こんな日々がずっとずっと続くんだと思ってたよ", "我以为这样的日子会一直一直持续下去。"),
    ]),
    (201.7, 217.5, [
        ("ゴメン今は気づいたよ", "对不起，现在我才明白。"),
        ("当たり前じゃないことに", "原来这一切并不是理所当然。"),
        ("まずはキミに伝えなくちゃ", "首先，我必须告诉你。"),
        ("「ありがとう」を", "谢谢你。"),
    ]),
    (241.8, 277.5, [
        ("キミの胸に届くかな?今は自信ないけれど", "这份心意能传到你心里吗？虽然我现在还没有自信。"),
        ("笑わないでどうか聴いて", "请别笑我，拜托你听一听。"),
        ("思いを歌に込めたから", "因为我把心意都写进了歌里。"),
        ("ありったけの「ありがとう」", "满满的“谢谢你”。"),
        ("歌に乗せて届けたい", "想借着歌声传达给你。"),
        ("この気持ちはずっとずっと忘れないよ", "这份心意，我会永远永远铭记。"),
        ("思いよ　届け", "思念啊，传达出去吧。"),
    ]),
]

def stamp(value):
    milliseconds = round(value * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"

def main():
    model, metadata = whisperx.load_align_model("ja", "cuda", model_name=str(MODEL), model_cache_only=True)
    transcript = []
    source_lines = []
    for start, end, lines in STANZAS:
        total = sum(len(japanese) for japanese, _ in lines)
        cursor = start
        for japanese, chinese in lines:
            duration = (end - start) * len(japanese) / total
            transcript.append({"start": max(start, cursor - 1.5), "end": min(end, cursor + duration + 1.5), "text": japanese})
            source_lines.append((japanese, chinese))
            cursor += duration
    aligned = whisperx.align(transcript, model, metadata, AUDIO, "cuda", return_char_alignments=True)
    cues = []
    for (japanese, chinese), result in zip(source_lines, aligned["segments"]):
        chars = result["chars"]
        timed = [char for char in chars if char.get("start") is not None and char.get("end") is not None]
        if not timed:
            raise RuntimeError(f"Could not align lyric line: {japanese}")
        cues.append((timed[0]["start"], timed[-1]["end"], japanese, chinese))
    assert len(cues) == len(source_lines)
    assert all(end >= start for start, end, _, _ in cues)
    assert all(cues[i][0] >= cues[i - 1][0] for i in range(1, len(cues)))
    blocks = [f"{index}\n{stamp(start)} --> {stamp(end)}\n{japanese}\n{chinese}" for index, (start, end, japanese, chinese) in enumerate(cues, 1)]
    OUT.write_text("\n\n".join(blocks) + "\n", encoding="utf-8-sig")
    print(json.dumps({"cues": len(cues), "output": str(OUT)}, ensure_ascii=False))

if __name__ == "__main__":
    main()
