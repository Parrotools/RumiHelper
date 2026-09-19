import httpx
import pytest

from rumi_helper.services.http_utils import (
    ResponseBodyTooLargeError,
    read_limited_response_body,
)


class ChunkedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"123456"
        yield b"abcdef"


async def test_limited_response_body_rejects_chunked_overflow_without_content_length():
    response = httpx.Response(
        200,
        stream=ChunkedStream(),
    )

    with pytest.raises(ResponseBodyTooLargeError):
        await read_limited_response_body(response, 10)


async def test_limited_response_body_accepts_chunked_body_within_limit():
    response = httpx.Response(
        200,
        stream=ChunkedStream(),
    )

    assert await read_limited_response_body(response, 12) == b"123456abcdef"
