"""Loopback-only local API.

Binding to 127.0.0.1 is not authentication. Other processes and browser
requests on the same PC are outside this MVP's protection boundary. The API
does not install permissive CORS, token placeholders, or a remote-bind
fallback. Real transcripts, audio, and databases remain private local data.
They are never committed to the repository.
"""

from market_voice_forecast_ledger.api.app import create_app

__all__ = ["create_app"]
