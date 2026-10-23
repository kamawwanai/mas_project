"""Offline evidence syntax checks shared by validation and evaluation."""
import re
import ipaddress
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import timezone, timedelta


def valid_url(value):
    if not isinstance(value, str) or re.search(r"[\s\\\x00-\x1f]", value) or re.search(r"%(?![0-9a-fA-F]{2})", value):
        return False
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            valid_host = True
        except ValueError:
            ascii_host = host.encode("idna").decode("ascii").rstrip(".")
            valid_host = len(ascii_host) <= 253 and all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in ascii_host.split("."))
        return bool(parts.scheme in {"http", "https"} and valid_host and
                    not parts.username and not parts.password and
                    (port is None or 1 <= port <= 65535))
    except (ValueError, UnicodeError):
        return False


def local_zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        if name == "Europe/Moscow":
            return timezone(timedelta(hours=3), name)
        raise ValueError(f"Invalid or unavailable timezone: {name}") from None


def supported_fields(source):
    fields = set(source.verified_fields) if valid_url(source.source_url) and source.checked_at else set()
    for evidence in source.additional_sources:
        if valid_url(evidence.source_url) and evidence.checked_at:
            fields.update(evidence.supports)
    return fields


def synthetic_source(source):
    return bool(source.notes and any(s in source.notes.lower() for s in
                ("synthetic", "синтетич", "not verified", "unverified")))
