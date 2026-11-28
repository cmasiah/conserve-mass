

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

