"""Small warm-name table recalled by pronunciation, for MT prompt only.

ASR only gets the hot list (MewType + band names).  Open-world names
(characters from the current game/anime, other bands, voice actors) are
matched phonetically here and appended to the *final* translation prompt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Tuple

_HONORIFICS = ("ちゃん", "さん", "くん", "様", "さま", "ちん", "たん")


def _kata_to_hira(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return "".join(out)


@dataclass(frozen=True)
class WarmName:
    aliases: Tuple[str, ...]
    target: str
    add_honorific: bool = True


# Priority: MewType is already in the hot list; this table covers the
# Magical-Trial cast and the four newer bands requested by the user.
NAMES: Tuple[WarmName, ...] = (
    # 魔法少女ノ魔女裁判（中文名按萌娘百科）
    WarmName(("二階堂ヒロ", "にかいどうひろ"), "二阶堂希罗"),
    WarmName(("ひろ", "ヒロ"), "希罗"),
    WarmName(("桜羽エマ", "さくらばえま"), "樱羽艾玛"),
    WarmName(("えま", "エマ"), "艾玛"),
    WarmName(("蓮見レイア", "はすみれいあ"), "莲见蕾雅"),
    WarmName(("れいあ", "レイア"), "蕾雅"),
    WarmName(("黒部ナノカ", "くろべなのか"), "黑部奈叶香"),
    WarmName(("なのか", "ナノカ"), "奈叶香"),
    WarmName(("氷上メルル", "ひかみめるる"), "冰上梅露露"),
    WarmName(("めるる", "メルル"), "梅露露"),
    WarmName(("橘シェリー", "たちばなしぇりー"), "橘雪莉"),
    WarmName(("しぇりー", "シェリー"), "雪莉"),
    WarmName(("夏目アンアン", "なつめあんあん"), "夏目安安"),
    WarmName(("あんあん", "アンアン"), "安安"),
    WarmName(("紫藤アリサ", "しどうありさ"), "紫藤艾莉莎"),
    WarmName(("宝生マーゴ", "ほうしょうまーご"), "宝生玛戈"),
    WarmName(("まーご", "マーゴ"), "玛戈"),
    WarmName(("沢渡ココ", "さわたりここ"), "泽渡可可"),
    WarmName(("佐伯ミリア", "さえきみりあ"), "佐伯米莉亚"),
    WarmName(("みりあ", "ミリア"), "米莉亚"),
    WarmName(("城ヶ崎ノア", "じょうがさきのあ"), "城崎诺亚"),
    WarmName(("のあ", "ノア"), "诺亚"),
    WarmName(("遠野ハンナ", "とおのはんな"), "远野汉娜"),
    WarmName(("はんな", "ハンナ"), "汉娜"),
    WarmName(("月代ユキ", "つきしろゆき"), "月代雪"),
    # Ave Mujica
    WarmName(("ういか", "ウイカ"), "初华"),
    WarmName(("むつみ", "ムツミ"), "睦"),
    WarmName(("うみり", "ウミリ"), "海铃"),
    WarmName(("にゃむ", "ニャム"), "喵梦"),
    WarmName(("さきこ", "サキコ"), "祥子"),
    WarmName(("Doloris", "ディオロリス"), "Doloris"),
    WarmName(("Mortis", "モルティス"), "Mortis"),
    WarmName(("Timoris", "ティモリス"), "Timoris"),
    WarmName(("Amoris", "アモリス"), "Amoris"),
    WarmName(("Oblivionis", "オブリビオニス"), "Oblivionis"),
    # MyGO!!!!!
    WarmName(("ともり", "トモリ"), "灯"),
    WarmName(("ともりん", "トモリン", "tomirin"), "小灯"),
    WarmName(("あのん", "アノン"), "爱音"),
    WarmName(("らな", "ラナ"), "乐奈"),
    WarmName(("そよ", "ソヨ"), "素世"),
    WarmName(("たき", "タキ"), "立希"),
    # 一家Dumb Rock!
    WarmName(("ほうさき", "ホウサキ"), "蓬咲"),
    WarmName(("ちえり", "チエリ"), "千樱梨"),
    WarmName(("れいか", "レイカ"), "蕾叶"),
    WarmName(("ねづき", "ネヅキ"), "宁月"),
    WarmName(("みく", "ミク"), "心玖"),
    # millsage
    WarmName(("ほたる", "ホタル"), "萤"),
    WarmName(("なつめ", "ナツメ"), "枣"),
    WarmName(("なぎ", "ナギ"), "凪"),
    WarmName(("まほろ", "マホロ"), "茉幌"),
    WarmName(("ともか", "トモカ"), "朋花"),
    # Other BanG Dream! bands (characters)
    WarmName(("かすみ", "カスミ"), "香澄"),
    WarmName(("たえ", "タエ"), "多惠"),
    WarmName(("りみ", "リミ"), "里美"),
    WarmName(("さや", "サヤ"), "沙绫"),
    WarmName(("ありさ", "アリサ"), "有咲"),
    WarmName(("ゆきな", "ユキナ"), "友希那"),
    WarmName(("さよ", "サヨ"), "纱夜"),
    WarmName(("りさ", "リサ"), "莉莎"),
    WarmName(("あこ", "アコ"), "亚子"),
    WarmName(("りんこ", "リンコ"), "燐子"),
    WarmName(("らん", "ラン"), "兰"),
    WarmName(("もか", "モカ"), "摩卡"),
    WarmName(("ひまり", "ヒマリ"), "绯玛丽"),
    WarmName(("つぐみ", "ツグミ"), "鸫"),
    WarmName(("ともえ", "トモエ"), "巴"),
    WarmName(("あや", "アヤ"), "彩"),
    WarmName(("ひな", "ヒナ"), "日菜"),
    WarmName(("ちさと", "チサト"), "千圣"),
    WarmName(("まや", "マヤ"), "麻弥"),
    WarmName(("いぶ", "イブ"), "伊芙"),
    WarmName(("こころ", "ココロ"), "心"),
    WarmName(("かおる", "カオル"), "薰"),
    WarmName(("はぐみ", "ハグミ"), "育美"),
    WarmName(("かのん", "カノン"), "花音"),
    WarmName(("みさき", "ミサキ"), "美咲"),
    WarmName(("ましろ", "マシロ"), "真白"),
    WarmName(("とうこ", "トウコ"), "透子"),
    WarmName(("ななみ", "ナナミ"), "七深"),
    WarmName(("つくし", "ツクシ"), "筑紫"),
    WarmName(("るい", "ルイ"), "瑠唯"),
    WarmName(("れい", "レイ"), "蕾"),
    WarmName(("ろっか", "ロッカ"), "六花"),
    WarmName(("ますき", "マスキ"), "益木"),
    WarmName(("れおな", "レオナ"), "令王那"),
    WarmName(("ちゆ", "チユ"), "知由"),
    # Poppin'Party / MyGO!!!!! / Ave Mujica voice actors
    WarmName(("あみ", "アミ"), "爱美"),
    WarmName(("おおつかさえ", "オオツカサエ"), "大冢纱英"),
    WarmName(("にしもとりみ", "ニシモトリミ"), "西本里美"),
    WarmName(("おおはしあやか", "オオハシアヤカ"), "大桥彩香"),
    WarmName(("いとうあやさ", "イトウアヤサ"), "伊藤彩沙"),
    WarmName(("ひつじみやひな", "ヒツジミヤヒナ"), "羊宫妃那"),
    WarmName(("たていしりん", "タテイシリン"), "立石凛"),
    WarmName(("あおきひな", "アオキヒナ"), "青木阳菜"),
    WarmName(("こひなたみか", "コヒナタミカ"), "小日向美香"),
    WarmName(("はやしここ", "ハヤシココ"), "林鼓子"),
    WarmName(("ささきりこ", "ササキリコ"), "佐佐木李子"),
    WarmName(("わたせゆづき", "ワタセユヅキ"), "渡濑结月"),
    WarmName(("おかだめい", "オカダメイ"), "冈田梦以"),
    WarmName(("よねざわあかね", "ヨネザワアカネ"), "米泽茜"),
    WarmName(("たかおかのん", "タカオカノン"), "高尾奏音"),
    # 丸君 / 薇欧拉
    WarmName(("まるくん", "マルクン"), "丸君"),
    WarmName(("う゛ぃおら", "ヴィオラ", "びおら"), "薇欧拉"),
    # 家庭成员 / 宠物（中文站常用称呼；家庭成员不加敬称）
    WarmName(("ママまち", "まままち"), "妈妈町", False),
    WarmName(("パパまち", "ぱぱまち"), "爸爸町", False),
    WarmName(("おとうとまち", "おととまち", "弟まち"), "弟弟町", False),
    WarmName(("ママちー", "ままちー"), "妈妈亲", False),
    WarmName(("パパちー", "ぱぱちー"), "爸爸亲", False),
    WarmName(("ママなが", "ままなが", "ママ永"), "妈妈永", False),
    WarmName(("パパなが", "ぱぱなが", "パパ永"), "爸爸永", False),
    WarmName(("ママりつ", "ままりつ", "ママ律"), "妈妈律", False),
    WarmName(("パパりつ", "ぱぱりつ", "パパ律", "パパリツ"), "爸爸律", False),
    WarmName(("めいちゃん", "メイちゃん"), "娃娃", False),
    WarmName(("ぽんちゃん", "ポンちゃん"), "Pon酱", False),
)


def _has_boundary(text: str, start: int, end: int) -> bool:
    if any(text.startswith(h, end) for h in _HONORIFICS):
        return True
    after = text[end:end + 1]
    before = text[start - 1:start]
    return after in ("", "、", "。", "！", "？", "は", "が", "の", "を", "に", "で", "と", "も", "ね", "よ") or before in ("", "、", "。", " ")


def retrieve(text: str, limit: int = 6) -> List[Tuple[str, str]]:
    """Return (source_surface, Chinese_target) matches found in text."""
    norm = _kata_to_hira(text)
    found: List[Tuple[int, int, int, str, str]] = []
    for name in NAMES:
        for alias in name.aliases:
            alias_norm = _kata_to_hira(alias)
            if len(alias_norm) < 2:
                continue
            start = norm.find(alias_norm)
            if start < 0:
                continue
            end = start + len(alias_norm)
            honorific = any(norm.startswith(h, end) for h in _HONORIFICS)
            # Short given names are extremely common words (ここ, あや, ひな…).
            # Only trust them when the speaker actually attaches an honorific.
            if len(alias_norm) <= 3 and not honorific and name.add_honorific:
                continue
            if not honorific and not _has_boundary(norm, start, end):
                continue
            # Prefer the surface as written in the source (kana/katakana).
            surface = text[start:end] if text[start:end].lower() == alias.lower() else alias
            target = name.target
            if name.add_honorific and norm.startswith("ちゃん", end):
                target += "酱"
            found.append((start, end, len(alias_norm), surface, target))
            break
    # Keep the longest match at each position; drop contained aliases such as
    # りさ inside ありさ.
    found.sort(key=lambda item: (item[0], -item[2]))
    kept: List[Tuple[int, int, int, str, str]] = []
    for item in found:
        start, end = item[0], item[1]
        if any(not (end <= other[0] or start >= other[1]) for other in kept):
            continue
        kept.append(item)
    kept.sort(key=lambda item: (-item[2], item[0]))
    result: List[Tuple[str, str]] = []
    for _, _, _, surface, target in kept:
        result.append((surface, target))
        if len(result) >= limit:
            break
    return result
