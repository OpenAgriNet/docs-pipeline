"""Shared rate-limit configuration for the ASGI app and route modules."""

import os

from slowapi import Limiter
from slowapi.util import get_remote_address


RATE_LIMIT_DEFAULT = os.environ.get("RATE_LIMIT_DEFAULT", "100/minute")
RATE_LIMIT_UPLOAD = os.environ.get("RATE_LIMIT_UPLOAD", "10/minute")

limiter = Limiter(key_func=get_remote_address)


__all__ = ["RATE_LIMIT_DEFAULT", "RATE_LIMIT_UPLOAD", "limiter"]
