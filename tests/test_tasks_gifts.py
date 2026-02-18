from app.config import settings
from app.tasks import gifts


def test_tone_profiles_cover_all_gift_tiers() -> None:
    tier_codes = {
        str(tier.get("code") or "").strip().lower()
        for tier in (getattr(settings, "GIFT_TIERS", []) or [])
        if isinstance(tier, dict) and str(tier.get("code") or "").strip()
    }

    profiles = gifts._gift_tone_profiles()

    assert tier_codes <= set(profiles.keys())
    assert gifts._ensure_tone_profile_coverage() == set()


def test_unknown_gift_code_uses_default_tone_hint() -> None:
    assert gifts._tone_hint_for_code("unknown_code") == gifts.DEFAULT_TONE_HINT


def test_fallback_reply_is_safe_and_valid_for_ru_en() -> None:
    label = "RGB Setup"

    for lang in ("ru", "en"):
        reply = gifts._fallback_reply(label, lang)

        assert gifts._looks_forbidden(reply) is False
        assert gifts._mentions_gift_once(reply, label) is True
        assert gifts._ok_reply(reply, label) is True
