"""Reading uploaded files without trusting their size."""

from __future__ import annotations

from fastapi import UploadFile

from suliko.core.errors import PayloadTooLargeError, ValidationError

CHUNK_BYTES = 1024 * 1024


async def read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    """The upload's bytes, refusing anything over ``max_bytes`` or empty.

    Read in chunks and counted as it goes: the multipart ``size`` is whatever
    the client said, and one file is held in memory at a time. A request-body
    limit in the proxy in front of the API is still the first line of defence.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError(
                f"The file is larger than the {max_bytes // CHUNK_BYTES} MB limit."
            )
        chunks.append(chunk)
    if total == 0:
        raise ValidationError("The file is empty.")
    return b"".join(chunks)
