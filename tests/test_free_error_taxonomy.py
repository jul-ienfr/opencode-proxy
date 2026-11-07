"""Taxonomie free 007 : 403 ≠ 429, quota ≠ rate-limit, transient ≠ rotation."""

from __future__ import annotations

from core.errors import (
    FREE_ERROR_ACCOUNT_OR_GLOBAL,
    FREE_ERROR_AUTH,
    FREE_ERROR_GATE_403,
    FREE_ERROR_OK,
    FREE_ERROR_QUOTA_429,
    FREE_ERROR_RATE_LIMIT_429,
    FREE_ERROR_TRANSIENT,
    classify_free_error,
)

FREE = '{"error":{"type":"FreeUsageLimitError","message":"free usage limit"}}'


def test_ok():
    assert classify_free_error(200, "") == FREE_ERROR_OK


def test_403_form_is_gate_not_auth():
    body = '{"error":{"type":"FreeTierError","message":"invalid agent"}}'
    assert classify_free_error(403, body) == FREE_ERROR_GATE_403


def test_403_invalid_key_is_auth():
    assert classify_free_error(403, '{"error":{"message":"invalid api key"}}') == FREE_ERROR_AUTH


def test_401_is_auth():
    assert classify_free_error(401, "") == FREE_ERROR_AUTH


def test_402_is_account_global():
    assert classify_free_error(402, "") == FREE_ERROR_ACCOUNT_OR_GLOBAL


def test_free_usage_limit_is_quota():
    assert classify_free_error(429, FREE) == FREE_ERROR_QUOTA_429


def test_empty_429_is_conservative_quota():
    assert classify_free_error(429, "") == FREE_ERROR_QUOTA_429


def test_rate_limit_is_short():
    body = '{"error":{"type":"RateLimitError","message":"rate limit exceeded"}}'
    assert classify_free_error(429, body) == FREE_ERROR_RATE_LIMIT_429


def test_transient_429_is_not_rotation():
    body = '{"error":{"message":"Endpoint is unavailable"}}'
    assert classify_free_error(429, body) == FREE_ERROR_TRANSIENT


def test_5xx_and_timeout_are_transient():
    assert classify_free_error(500, "") == FREE_ERROR_TRANSIENT
    assert classify_free_error(503, "") == FREE_ERROR_TRANSIENT
    assert classify_free_error(408, "") == FREE_ERROR_TRANSIENT
    assert classify_free_error(None, "") == FREE_ERROR_TRANSIENT


def test_other_4xx_is_gate():
    assert classify_free_error(400, "") == FREE_ERROR_GATE_403
    assert classify_free_error(404, "") == FREE_ERROR_GATE_403


def test_bytes_body_and_headers_are_supported():
    assert classify_free_error(429, FREE.encode()) == FREE_ERROR_QUOTA_429
    assert classify_free_error(429, FREE, headers={"retry-after": "120"}) == FREE_ERROR_QUOTA_429
