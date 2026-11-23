from free import classify_429 as c

QUOTA_BODY = (
    '{"type":"error","error":{"type":"FreeUsageLimitError",'
    '"message":"Free usage limit reached."}}'
)

FORMAT_BODY = (
    '{"type":"error","error":{"type":"FreeTierError",'
    '"message":"free tier can only be used from within OpenCode"}}'
)


def test_free_usage_limit_is_quota_even_on_an_unproven_ip():
    result = c.classify_429(429, body=QUOTA_BODY, ip_proven=False, now=1000.0)
    assert result.cls == c.QUOTA
    assert result.reset_at is not None and result.reset_at > 1000.0


def test_free_usage_limit_on_proven_ip_is_quota_with_retry_after():
    result = c.classify_429(
        429,
        headers={"Retry-After": "9000"},
        body=QUOTA_BODY,
        ip_proven=True,
        wire_fp_match=True,
        now=1000.0,
    )
    assert result.cls == c.QUOTA
    assert result.retry_after_s == 9000.0
    assert result.reset_at == 10000.0


def test_explicit_free_tier_rejection_is_a_format_burn():
    result = c.classify_429(
        429,
        body=FORMAT_BODY,
        ip_proven=True,
        wire_fp_match=True,
        now=1000.0,
    )
    assert result.cls == c.FORMAT
    assert "form" in result.reason


def test_unclassified_429_is_transient_not_a_burn():
    result = c.classify_429(429, body="", ip_proven=False)
    assert result.cls == c.UPSTREAM
    assert "transient" in result.reason


def test_retry_after_http_date_is_parsed():
    headers = {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}
    seconds = c.parse_retry_after(headers["retry-after"], now=1445412480.0)
    assert seconds is not None and seconds == 0.0


def test_session_hint_is_session_class():
    result = c.classify_429(429, body="maximum number of sessions reached", ip_proven=True)
    assert result.cls == c.SESSION


def test_transient_429_is_upstream():
    result = c.classify_429(429, body="Endpoint is unavailable, try again", ip_proven=True)
    assert result.cls == c.UPSTREAM


def test_non_429_is_upstream():
    result = c.classify_429(500, body="boom")
    assert result.cls == c.UPSTREAM


def test_next_utc_midnight_is_strictly_future():
    reset = c.next_utc_midnight(1000.0)
    assert reset > 1000.0


def test_generic_free_tier_rate_limit_is_transient_not_a_burn():
    # [2026-11-21] « free tier » seul ne doit plus suffire a classer FORMAT :
    # un 429 transitoire/rate-limit qui mentionne « free tier » brulait l'IP 24 h.
    body = '{"error":{"type":"RateLimitError","message":"Free tier rate limit reached, try again later"}}'
    result = c.classify_429(429, body=body, ip_proven=False, now=1000.0)
    assert result.cls == c.UPSTREAM


def test_specific_free_tier_rejection_still_burns():
    # La hint SPECIFIQUE reste un burn de forme — mais seulement quand la
    # preuve (IP prouvée + wire_fp identique) est présente.
    result = c.classify_429(
        429,
        body=FORMAT_BODY,
        ip_proven=True,
        wire_fp_match=True,
        now=1000.0,
    )
    assert result.cls == c.FORMAT


def test_format_hint_on_unproven_ip_is_not_a_burn():
    # [fix 2026-11-21] Sans 200 prouvé, un rejet de forme peut venir de notre
    # propre corps : brûler l'IP 24 h n'est pas mesuré.
    result = c.classify_429(429, body=FORMAT_BODY, ip_proven=False, now=1000.0)
    assert result.cls == c.UPSTREAM
    assert "no burn" in result.reason


def test_format_hint_with_mismatched_wire_fp_is_not_a_burn():
    # [fix 2026-11-21] La requête n'est pas le wire prouvé : le rejet ne prouve
    # rien sur l'IP, il prouve que CE corps diffère du format attendu.
    result = c.classify_429(
        429,
        body=FORMAT_BODY,
        ip_proven=True,
        wire_fp_match=False,
        now=1000.0,
    )
    assert result.cls == c.UPSTREAM
    assert "no burn" in result.reason


def test_retry_after_inf_is_rejected():
    assert c.parse_retry_after("inf") is None
    assert c.parse_retry_after("nan") is None


def test_retry_after_huge_is_capped_to_24h():
    assert c.parse_retry_after("9" * 400) is None or c.parse_retry_after("9" * 400) <= 86400.0
    assert c.parse_retry_after("999999999999") == 86400.0


def test_retry_after_normal_value_preserved():
    assert c.parse_retry_after("9000") == 9000.0
