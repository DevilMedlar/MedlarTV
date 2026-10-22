"""Resolve the Ollama client URL shared by desktop chat and service probes."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from typing import cast
from collections.abc import Mapping
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib import request as urllib_request


_DEFAULT_URL = "http://127.0.0.1:11434"
_HOST_CHARS = re.compile(r"[A-Za-z0-9_.-]+\Z")


def _resolve(environ: Mapping[str, str] | None = None) -> tuple[str, str, int]:
    values = os.environ if environ is None else environ
    raw = next(
        (value.strip() for key in ("OLLAMA_URL", "OLLAMA_HOST")
         if (value := values.get(key)) and value.strip()),
        _DEFAULT_URL,
    )
    if any(char.isspace() for char in raw):
        raise ValueError("Ollama endpoint must not contain whitespace")

    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlsplit(candidate)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        explicit_port = parsed.port
    except ValueError:
        # Parser errors may contain credential-bearing input or port values.
        raise ValueError("Invalid Ollama endpoint (stage=parsing)") from None

    if scheme not in ("http", "https"):
        raise ValueError("Ollama endpoint must use http or https")
    if not host:
        raise ValueError("Ollama endpoint must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Ollama endpoint must not include credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Ollama endpoint must not include a query or fragment")
    if parsed.netloc.endswith(":"):
        raise ValueError("Ollama endpoint port must be a number")
    if explicit_port == 0:
        raise ValueError("Ollama endpoint port must be between 1 and 65535")

    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError("Invalid Ollama IPv6 host (stage=host_validation)") from None
    elif not _HOST_CHARS.fullmatch(host) or host.startswith((".", "-")):
        raise ValueError("Invalid Ollama host (stage=host_validation)")

    # A server may bind every interface, but a client must connect to one.
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"

    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    authority = f"[{host}]" if ":" in host else host
    if explicit_port is not None:
        authority = f"{authority}:{explicit_port}"
    base_url = f"{scheme}://{authority}{parsed.path.rstrip('/')}"
    return base_url, host, port


def ollama_base_url(environ: Mapping[str, str] | None = None) -> str:
    """Return the normalized client URL, preferring OLLAMA_URL over OLLAMA_HOST."""
    return _resolve(environ)[0]


def ollama_probe_target(environ: Mapping[str, str] | None = None) -> tuple[str, int]:
    """Return the TCP host and port of the same effective Ollama URL."""
    _, host, port = _resolve(environ)
    return host, port


def ollama_api_ready(environ: Mapping[str, str] | None = None, timeout: float = 0.3) -> bool:
    """Check that the effective endpoint answers with Ollama's model-list shape."""
    base_url = ollama_base_url(environ)
    probe = urllib_request.Request(f"{base_url}/api/tags", headers={"Accept": "application/json"})
    try:
        with urllib_request.urlopen(probe, timeout=timeout) as response:
            if response.status != 200:
                return False
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            return False
        payload: object = json.loads(body)
    except HTTPError as exc:
        exc.close()
        return False
    except (OSError, HTTPException, ValueError):
        return False
    return isinstance(payload, dict) and isinstance(cast(dict[str, object], payload).get("models"), list)
