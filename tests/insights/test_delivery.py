from app.services.insight_delivery import normalize_insight_mode


def test_delivery_mode_defaults_to_legacy_and_suppresses_shadow():
    assert normalize_insight_mode(None) == "legacy"
    assert normalize_insight_mode("invalid") == "legacy"
    assert normalize_insight_mode("shadow") == "shadow"
