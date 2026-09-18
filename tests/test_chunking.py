import pytest

from rumi_helper.services.web.summarizer import chunk_text


def test_short_text_single_chunk():
    assert chunk_text("短文本", chunk_chars=100) == ["短文本"]


def test_exact_fit():
    text = "a" * 100
    assert chunk_text(text, chunk_chars=100, overlap=0) == [text]


def test_multiple_chunks_with_overlap():
    text = "".join(str(i % 10) for i in range(250))  # 250 字符
    chunks = chunk_text(text, chunk_chars=100, overlap=20)
    assert len(chunks) == 3
    # 相邻块有 overlap
    assert chunks[1][0] == text[80]
    assert chunks[2][0] == text[160]
    # 拼起来（去掉 overlap）能还原全文顺序
    assert chunks[0][:100] == text[:100]
    assert chunks[1][:100] == text[80:180]


def test_no_empty_chunks():
    chunks = chunk_text("abcdefghij", chunk_chars=4, overlap=3)
    assert all(c for c in chunks)


def test_invalid_chunk_chars():
    with pytest.raises(ValueError):
        chunk_text("abc", chunk_chars=0)


def test_overlap_larger_than_chunk_coerced():
    chunks = chunk_text("x" * 30, chunk_chars=10, overlap=50)
    assert len(chunks) >= 2
