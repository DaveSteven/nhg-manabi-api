"""逐段精听：把 MOJi 的字幕 HTML 与译文 HTML 合并成带译文的分段。

字幕里每个 `<p data-starttime data-endtime>` 是一段，译文是同样数量的 `<p>`；
按顺序一一对应。数量对不上时不猜测，译文留空，由前端给出提示。
"""

import re
from html.parser import HTMLParser

_TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})[,.](\d{1,3})$")


def parse_time(value: str | None) -> float | None:
    match = _TIME.match((value or "").strip())
    if not match:
        return None
    h, m, s, fraction = match.groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(fraction.ljust(3, "0")) / 1000


class _Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._current: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self._finish()
            self._current = {"attrs": dict(attrs), "text": ""}
        elif tag == "br" and self._current is not None:
            self._current["text"] += "\n"

    def handle_data(self, data):
        if self._current is not None:
            self._current["text"] += data

    def handle_endtag(self, tag):
        if tag == "p":
            self._finish()

    def _finish(self):
        if self._current is not None:
            self._current["text"] = self._current["text"].strip()
            self.items.append(self._current)
        self._current = None


def _paragraphs(html: str | None) -> list[dict]:
    parser = _Paragraphs()
    parser.feed(html or "")
    parser.close()
    parser._finish()
    return parser.items


def listening_segments(subtitle: str | None, translation: str | None) -> list[dict]:
    source = _paragraphs(subtitle)
    lines = [item["text"] for item in _paragraphs(translation)]
    aligned = len(lines) == len(source)
    segments = []
    for index, item in enumerate(source):
        start = parse_time(item["attrs"].get("data-starttime"))
        end = parse_time(item["attrs"].get("data-endtime"))
        if start is None or end is None or end <= start or not item["text"]:
            continue
        segments.append({"start": start, "end": end, "text": item["text"],
                         "translation": lines[index] if aligned else ""})
    return segments
