"""Strict, small multipart/form-data parser (no third-party package, bounded work, never touches the file system).

Text fields end up in a dict like url-encoded forms do (first value wins); file parts end up in a second dict. Anything
malformed raises MultipartError, which the ASGI adapter turns into a plain 400.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_FIELDS = 450
MAX_FILES = 3
MAX_FIELD_BYTES = 64 * 1024
MAX_HEADER_BYTES = 4096
_BOUNDARY = re.compile(r"^[0-9A-Za-z'()+_,\-./:=? ]{1,70}$")
_PARAM = re.compile(r';\s*([A-Za-z0-9*_-]+)\s*=\s*(?:"([^"]*)"|([^;\s]*))')  # browsers encode a quote as %22; no escapes


class MultipartError(Exception):
    pass


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    data: bytes


def boundary_of(content_type: str) -> str:
    match = re.search(r';\s*boundary=(?:"([^"]+)"|([^;\s]+))', content_type, re.IGNORECASE)
    boundary = (match.group(1) or match.group(2)) if match else ""
    if not _BOUNDARY.match(boundary or ""):
        raise MultipartError("bad boundary")
    return boundary


def _disposition(headers: bytes) -> dict[str, str]:
    if len(headers) > MAX_HEADER_BYTES:
        raise MultipartError("header too large")
    for line in headers.decode("utf-8", "replace").split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-disposition":
            if not value.strip().lower().startswith("form-data"):
                raise MultipartError("bad disposition")
            params = {}
            for m in _PARAM.finditer(value):
                raw = m.group(2) if m.group(2) is not None else m.group(3)
                params.setdefault(m.group(1).lower(), raw or "")
            return params
    raise MultipartError("no disposition")


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, UploadedFile]]:
    delimiter = b"--" + boundary_of(content_type).encode("latin-1")
    chunks = body.split(delimiter)
    if len(chunks) < 2 or not chunks[-1].startswith(b"--"):
        raise MultipartError("unterminated")
    fields: dict[str, str] = {}
    files: dict[str, UploadedFile] = {}
    count = 0
    for chunk in chunks[1:-1]:
        if not chunk.startswith(b"\r\n") or not chunk.endswith(b"\r\n"):
            raise MultipartError("bad part framing")
        head, sep, payload = chunk[2:-2].partition(b"\r\n\r\n")
        if not sep:
            if chunk[2:-2].endswith(b"\r\n"):  # a part without any body
                head, payload = chunk[2:-2][:-2], b""
            else:
                raise MultipartError("no header end")
        params = _disposition(head)
        name = params.get("name", "")
        if not name or len(name) > 200:
            raise MultipartError("bad field name")
        count += 1
        if count > MAX_FIELDS:
            raise MultipartError("too many fields")
        if "filename" in params:
            if len(files) >= MAX_FILES:
                raise MultipartError("too many files")
            if payload or params["filename"]:  # browsers send an empty part when no file was chosen
                files.setdefault(name, UploadedFile(params["filename"], payload))
        else:
            if len(payload) > MAX_FIELD_BYTES:
                raise MultipartError("field too large")
            fields.setdefault(name, payload.decode("utf-8", "replace"))
    return fields, files
