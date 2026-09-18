from app.services.web.url_parser import extract_urls, has_url


def test_single_url():
    assert extract_urls("访问 https://example.com 看看") == ["https://example.com"]


def test_chinese_text_with_query_url():
    text = "帮我总结一下这个\nhttps://example.com/test?id=123"
    assert extract_urls(text) == ["https://example.com/test?id=123"]


def test_multiple_urls_dedup_preserve_order():
    text = "https://a.com 然后 https://b.com 再来 https://a.com"
    assert extract_urls(text) == ["https://a.com", "https://b.com"]


def test_trailing_punctuation_stripped():
    assert extract_urls("看 https://example.com/a。") == ["https://example.com/a"]
    assert extract_urls("看 https://example.com/a，") == ["https://example.com/a"]
    assert extract_urls("看 https://example.com/a）") == ["https://example.com/a"]


def test_unbalanced_paren_stripped():
    assert extract_urls("见 https://en.wikipedia.org/wiki/Python_(language)") == [
        "https://en.wikipedia.org/wiki/Python_(language)"
    ]
    assert extract_urls("见 https://example.com/x) 了") == ["https://example.com/x"]


def test_http_and_https():
    text = "http://a.com 和 https://b.com"
    assert extract_urls(text) == ["http://a.com", "https://b.com"]


def test_no_url():
    assert extract_urls("今天晚上吃什么") == []
    assert has_url("今天晚上吃什么") is False


def test_case_insensitive_host_dedup():
    assert extract_urls("HTTPS://A.com/X https://a.com/X") == ["HTTPS://A.com/X"]


def test_path_and_query_case_are_not_deduplicated():
    assert extract_urls(
        "https://example.com/A https://example.com/a "
        "https://example.com/x?Key=V https://example.com/x?key=V"
    ) == [
        "https://example.com/A",
        "https://example.com/a",
        "https://example.com/x?Key=V",
        "https://example.com/x?key=V",
    ]
