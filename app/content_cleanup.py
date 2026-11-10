"""清洗从 MOJi 爬取的题目内容里残留的私有标记。

源数据里 `<MOJiTest_URL>` 是空的占位标签（没有闭合、没有内容），对展示无意义。
导入时用 `strip_moji_markup` 清理；已入库的数据由启动迁移 `strip_moji_markup_in_db` 清理。
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

MOJI_TAGS = ("<MOJiTest_URL>", "</MOJiTest_URL>")
# 只在这几个字段里出现过；选项、解析、翻译都没有。
MOJI_FIELDS = ("title", "passage", "subtitle")


def strip_moji_markup(value: str | None) -> str:
    result = value or ""
    for tag in MOJI_TAGS:
        result = result.replace(tag, "")
    return result


def strip_moji_markup_in_db(db: Session) -> int:
    """幂等：只更新仍含标记的行，返回更新行数。"""
    updated = 0
    for column in MOJI_FIELDS:
        expr = column
        for tag in MOJI_TAGS:
            expr = f"REPLACE({expr}, '{tag}', '')"
        result = db.execute(
            text(f"UPDATE exam_questions SET {column} = {expr} WHERE {column} LIKE :pattern"),
            {"pattern": "%<%MOJiTest_URL>%"},
        )
        updated += result.rowcount or 0
    db.commit()
    return updated
