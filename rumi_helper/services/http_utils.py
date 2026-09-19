"""Small bounded HTTP response helpers shared by outbound API clients."""

class ResponseBodyTooLargeError(ValueError):
    """Remote response exceeded the configured in-memory body budget."""


async def read_limited_response_body(response, max_bytes: int) -> bytes:
    """Read a streaming httpx response with a hard decoded-byte limit."""
    limit = max(1, int(max_bytes))
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > limit:
            raise ResponseBodyTooLargeError("response body too large")

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise ResponseBodyTooLargeError("response body too large")
        chunks.append(chunk)
    return b"".join(chunks)
