from rumi_helper.services.web.url_parser import (
    extract_urls,
    has_url,
    redact_url_secrets_in_text,
    safe_url_for_history,
    safe_url_for_log,
)


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


def test_safe_url_for_history_preserves_only_github_repo_path():
    assert safe_url_for_history(
        "https://user:password@github.com/OpenAI/openai-python/issues/123"
        "?token=secret#frag"
    ) == "https://github.com/OpenAI/openai-python"
    assert safe_url_for_history(
        "https://example.com/private/document-id?token=secret#frag"
    ) == "https://example.com/…"
    assert redact_url_secrets_in_text(
        "看看 https://user:password@example.com/private?a=secret#frag，然后继续。"
    ) == "看看 https://example.com/…，然后继续。"


def test_safe_url_for_log_redacts_credentials_path_query_and_fragment():
    assert safe_url_for_log(
        "https://user:password@example.com:8443/private/token?secret=abc#fragment"
    ) == "https://example.com:8443/…"
    assert safe_url_for_log("https://example.com?token=abc") == "https://example.com/"
    assert safe_url_for_log("https://[2606:4700::1111]/private") == "https://[2606:4700::1111]/…"
    assert safe_url_for_log("not-a-url") == "<invalid-url>"
