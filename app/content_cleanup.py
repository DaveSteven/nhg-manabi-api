"""清洗从 MOJi 爬取的题目内容里残留的私有标记。

源数据残留三类 MOJi 私有标记：
- `<MOJiTest_URL>` 空占位标签（无闭合、无内容）；
- `class="moji-*"` 属性（moji-content / moji-trans / moji-notation / moji-toolkit-org 等）；
- 正文中**空值**的 `data-starttime="" data-endtime=""` 属性（读解 passage 常见）。

导入时用 `strip_moji_markup` / `strip_moji_subtitle_markup` / `clean_option_text` 清理；
已入库的数据由启动迁移 `strip_moji_markup_in_db` 清理（幂等）。

红线：`subtitle` 里带真实时间值的 `data-starttime/endtime` 是听力分段的功能数据
（`app/listening.py` 与前端 `lib/listening.ts` 按它切分段），只清 class、保留 data 属性。
"""

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

MOJI_TAGS = ("<MOJiTest_URL>", "</MOJiTest_URL>")
# 只在这几个字段里出现过；解析（analysis）没有残留。
MOJI_FIELDS = ("title", "passage", "subtitle", "translation")

_MOJI_CLASS_ATTR = re.compile(r'\s+class="[^"]*moji[^"]*"', re.IGNORECASE)
_EMPTY_TIMING_ATTR = re.compile(r'\s+data-(?:start|end)time=""', re.IGNORECASE)
# options 注音壳：<span class="moji-notation" ...><span class="moji-toolkit-org" ...>内层</span></span>
_MOJI_TOOLKIT_SPAN = re.compile(
    r'<span\s+class="[^"]*moji-notation[^"]*"[^>]*>\s*'
    r'<span\s+class="[^"]*moji-toolkit[^"]*"[^>]*>(.*?)</span>\s*</span>',
    re.IGNORECASE | re.DOTALL,
)
# 其余 moji-notation 壳（内层可能是 <ruby> 等，非贪婪到第一个 </span>）。
_MOJI_NOTATION_SPAN = re.compile(
    r'<span\s+class="[^"]*moji-notation[^"]*"[^>]*>(.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)


def _strip_moji_markup(value: str | None, drop_empty_timing: bool) -> str:
    result = value or ""
    for tag in MOJI_TAGS:
        result = result.replace(tag, "")
    result = _MOJI_TOOLKIT_SPAN.sub(r"\1", result)
    result = _MOJI_NOTATION_SPAN.sub(r"\1", result)
    result = _MOJI_CLASS_ATTR.sub("", result)
    if drop_empty_timing:
        result = _EMPTY_TIMING_ATTR.sub("", result)
    return result


def strip_moji_markup(value: str | None) -> str:
    """题干/文章/译文清洗：MOJi 标签 + moji class 属性 + 空时间轴属性。"""
    return _strip_moji_markup(value, drop_empty_timing=True)


def strip_moji_subtitle_markup(value: str | None) -> str:
    """字幕清洗：时间轴 data 属性是功能数据，只清标签与 class 属性。"""
    return _strip_moji_markup(value, drop_empty_timing=False)


def clean_option_text(value: str | None) -> str:
    """选项文本：解包 moji 注音壳，保留内层内容。"""
    return _strip_moji_markup(value, drop_empty_timing=False)


def strip_moji_markup_in_db(db: Session) -> int:
    """幂等：只更新仍含 MOJi 残留的行，返回更新行数。

    regex 清洗无法用纯 SQL REPLACE 表达，改为 LIKE 预筛（moji 大小写两版）
    后在 Python 侧清洗、逐行更新。行数上限约 6000（首次运行），此后为 0。
    """
    updated = 0
    for column in MOJI_FIELDS:
        rows = db.execute(
            text(
                f"SELECT id, {column} FROM exam_questions "
                f"WHERE {column} LIKE '%moji%' OR {column} LIKE '%MOJi%'"
            )
        ).fetchall()
        for row_id, value in rows:
            clean = strip_moji_subtitle_markup if column == "subtitle" else strip_moji_markup
            cleaned = clean(value)
            if cleaned != value:
                db.execute(
                    text(f"UPDATE exam_questions SET {column} = :v WHERE id = :id"),
                    {"v": cleaned, "id": row_id},
                )
                updated += 1
    rows = db.execute(
        text("SELECT id, text FROM exam_options WHERE text LIKE '%moji%' OR text LIKE '%MOJi%'")
    ).fetchall()
    for row_id, value in rows:
        cleaned = clean_option_text(value)
        if cleaned != value:
            db.execute(
                text("UPDATE exam_options SET text = :v WHERE id = :id"),
                {"v": cleaned, "id": row_id},
            )
            updated += 1
    db.commit()
    return updated
