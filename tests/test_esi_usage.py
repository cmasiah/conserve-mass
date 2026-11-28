

def test_errors_keep_status_message_and_hint():
    from app.esi import usage
    usage.reset()
    usage.record("/corporations/123/assets/", 403, {}, method="GET",
                 body='{"error": "Character does not have required role(s)"}')
    usage.record("/universe/types/34/", 200, {})
    fails = usage.summary()["failures"]
    assert len(fails) == 1
    f = fails[0]
    assert f["status"] == 403 and f["family"] == "Corp assets"
    assert f["message"] == "Character does not have required role(s)"
    assert "role" in f["hint"]
    usage.reset()



def test_limit_bars_count_tokens_per_character_in_the_window():
    import base64
    import json

    from app.esi import usage
    usage.reset()
    h = {"X-Ratelimit-Group": "corp-contracts", "X-Ratelimit-Limit": "150/15m"}
    now = 1_000_000.0
    for status in (200, 200, 304, 404, 500):          # 2 + 2 + 1 + 5 + 0 = 10 tokens
        usage.record("/corporations/1/contracts/", status, {**h, "X-Ratelimit-Remaining": "140"},
                     who=11)
    usage.record("/corporations/1/contracts/", 200, {**h, "X-Ratelimit-Remaining": "148"}, who=22)
    for c in usage._calls:
        c.at = now - 60
    old = usage.Call(now - 1200, "Corp contracts", 200, "corp-contracts", 10, "150/15m", 11)
    usage._calls.appendleft(old)                       # outside the 15 min window
    lim = usage.limits(now)
    g = lim["groups"][0]
    assert (g["group"], g["max"], g["window_label"]) == ("corp-contracts", 150, "15 min")
    busiest = g["buckets"][0]
    assert busiest["who"] == 11 and busiest["used"] == 10 and busiest["calls"] == 5
    assert busiest["peak"] == 140                      # 150 - 10 left, seen 20 min ago
    assert g["buckets"][1]["used"] == 2
    assert lim["errors_min"] == 2                      # the 404 and the 500
    assert usage.parse_limit("300/1h") == (300, 3600) and usage.parse_limit("x") is None
    tok = "h." + base64.urlsafe_b64encode(json.dumps({"sub": "CHARACTER:EVE:93399339"})
                                          .encode()).decode().rstrip("=") + ".s"
    assert usage.who_from_token(tok) == 93399339 and usage.who_from_token(None) == 0
    usage.reset()
