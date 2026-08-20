"""ID 与 content-hash 测试。"""

from corpus.ids import (
    source_id_from_content,
    slugify,
    rename_suffix,
)


def test_source_id_stable_for_same_content():
    a = source_id_from_content("hello world")
    b = source_id_from_content("hello world")
    assert a == b
    assert len(a) == 16  # 16 hex chars


def test_source_id_differs_for_different_content():
    a = source_id_from_content("hello world")
    b = source_id_from_content("hello world!")
    assert a != b


def test_source_id_handles_unicode():
    a = source_id_from_content("你好,世界")
    b = source_id_from_content("你好,世界")
    assert a == b
    assert len(a) == 16


def test_slugify_basic():
    assert slugify("PostgreSQL MVCC") == "postgresql-mvcc"
    assert slugify("Hello World") == "hello-world"
    assert slugify("  spaces  ") == "spaces"


def test_slugify_special_chars():
    assert slugify("C++ Programming") == "c-programming"
    assert slugify("Q&A: How?") == "q-a-how"
    assert slugify("foo/bar baz") == "foo-bar-baz"


def test_slugify_truncates_long_titles():
    long_title = "a" * 200
    slug = slugify(long_title)
    assert len(slug) <= 80


def test_slugify_empty_fallback():
    assert slugify("") == "untitled"
    assert slugify("!!!") == "untitled"


def test_rename_suffix_format():
    """格式: ingest-<UTC compact ISO>, 例 ingest-20260820-183000."""
    import re
    s = rename_suffix()
    assert s.startswith("ingest-")
    # ingest- + 8位日期 + - + 6位时间
    m = re.fullmatch(r"ingest-(\d{8})-(\d{6})", s)
    assert m is not None, f"unexpected format: {s!r}"
    # 简单 sanity: 日期 2025+ (系统当前年份之后), 时间 < 240000
    yyyymmdd = m.group(1)
    hhmmss = m.group(2)
    assert int(yyyymmdd[:4]) >= 2025
    assert int(hhmmss) < 240000
