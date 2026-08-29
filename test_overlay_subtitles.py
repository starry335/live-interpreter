from live_interpreter import compose_live_subtitles, resize_geometry, subtitle_spacing, visibility_progress


def event(index: int, finalized_at: float) -> dict:
    return {
        "source": f"source {index}",
        "translated": f"translation {index}",
        "final": True,
        "finalized_at": finalized_at,
    }


events = [event(index, 100.0 + index) for index in range(6)]
text, source = compose_live_subtitles(events, 3, 6, 8.0, 800, now=107.0)
assert text.splitlines() == [f"translation {index}" for index in range(6)]
assert source.splitlines() == ["source 4", "source 5"]

text, source = compose_live_subtitles(events, 3, 6, 8.0, 800, now=120.0)
assert text.splitlines() == ["translation 3", "translation 4", "translation 5"]
assert source.splitlines() == ["source 4", "source 5"]
print("overlay subtitle retention: ok")

assert resize_geometry("se", 100, 100, 980, 280, 120, 80) == (100, 100, 1100, 360)
assert resize_geometry("nw", 100, 100, 980, 280, 50, 30) == (150, 130, 930, 250)
assert resize_geometry("nw", 100, 100, 500, 200, 100, 80) == (120, 120, 480, 180)
print("overlay free resize: ok")

assert visibility_progress(True, 10.0, 10.0) == 0.0
assert abs(visibility_progress(True, 10.0, 10.075) - 0.5) < 1e-9
assert abs(visibility_progress(False, 10.0, 10.075) - 0.5) < 1e-9
assert visibility_progress(False, 10.0, 10.15) == 0.0
print("overlay control transition: ok")

assert subtitle_spacing(19) == (26, 9)
large_line_height, large_sentence_gap = subtitle_spacing(42)
assert large_line_height >= 56
assert large_sentence_gap >= 11
print("overlay dynamic spacing: ok")
