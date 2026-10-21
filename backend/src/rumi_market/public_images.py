"""Conditional browser caching for already-validated public encyclopedia PNGs."""

from pathlib import Path
import re

from fastapi import Request
from fastapi.responses import FileResponse, Response


PUBLIC_IMAGE_CACHE_CONTROL = "public, max-age=3600"
_ENTITY_TAG = r'(?:W/)?"[\x21\x23-\x7e\x80-\xff]*"'
_ENTITY_TAG_LIST = re.compile(rf'[ \t,]*(?:{_ENTITY_TAG}[ \t]*(?:,[ \t]*(?:{_ENTITY_TAG}[ \t]*)?)*)?')


def _etag_matches(condition: str, etag: str) -> bool:
    # GET uses weak comparison. Commas inside an opaque tag are not separators.
    if condition.strip(" \t") == "*":
        return True
    if not _ENTITY_TAG_LIST.fullmatch(condition):
        return False
    return any(tag.removeprefix("W/") == etag for tag in re.findall(_ENTITY_TAG, condition))


def public_png(path: Path, request: Request) -> Response:
    response = FileResponse(path, media_type="image/png", stat_result=path.stat(),
                            headers={"Cache-Control": PUBLIC_IMAGE_CACHE_CONTROL})
    condition = ",".join(request.headers.getlist("if-none-match"))
    if condition and _etag_matches(condition, response.headers["etag"]):
        return Response(status_code=304, headers={
            name: response.headers[name] for name in ("cache-control", "etag", "last-modified")
        })
    return response
