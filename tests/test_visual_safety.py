import pytest

from app.assets.safety import footage_context, is_risky_shot, safe_shot


@pytest.mark.parametrize("shot", [
    "Soldiers marching through a city street at dawn",
    "Crowd of protesters holding signs outside parliament",
    "The president speaking at a podium",
    "A national flag waving over a burning building",
    "Close-up of a woman voting at a polling booth",
    "Missile launching into the night sky",
])
def test_risky_politics_shots_are_replaced_with_symbolic_ones(shot):
    out, replaced = safe_shot("Politics", shot)
    assert replaced and out != shot
    assert not is_risky_shot("Politics", out), "replacement must itself be safe"


def test_safe_politics_shots_pass_through():
    shot = "Slow push-in on a wooden gavel on a desk"
    assert safe_shot("Politics", shot) == (shot, False)


def test_other_categories_are_never_rewritten():
    shot = "Crowd of fans cheering in a football stadium"
    for category in ("Technology", "Finance", "Entertainment", "Launches", None):
        assert safe_shot(category, shot) == (shot, False)


@pytest.mark.parametrize("shot", [
    "A surgeon operating on a patient in a hospital theatre",
    "Close-up of a nurse giving a child an injection",
    "Doctor examining a sick baby",
    "A syringe being filled with vaccine, close-up on the needle",
    "Wounded people lying on hospital beds",
])
def test_risky_healthcare_shots_are_replaced_with_medical_symbols_not_political_ones(shot):
    out, replaced = safe_shot("Healthcare", shot)
    assert replaced and out != shot
    assert not is_risky_shot("Healthcare", out), "replacement must itself be safe"
    for political in ("gavel", "ballot", "compass", "scales"):
        assert political not in out.lower(), f"a healthcare story must not get a political shot ({political})"


def test_safe_healthcare_shots_pass_through():
    shot = "Laboratory glassware and a microscope on a bench"
    assert safe_shot("Healthcare", shot) == (shot, False)


def test_healthcare_headlines_are_withheld_from_footage_prompts():
    assert footage_context("Healthcare", "Drug X approved for patients") == ""
    assert footage_context("Technology", "Drug X approved") == "Drug X approved"


def test_headline_is_withheld_from_footage_prompts_only_for_politics():
    h = "Leader X orders troops to border"
    assert footage_context("Politics", h) == ""
    assert footage_context("Technology", h) == h


def test_every_replacement_shot_is_itself_safe_for_every_sensitive_category():
    """The replacement is the backstop: if it tripped the filter it would be replaced by itself forever."""
    from app.assets.safety import _SYMBOLIC
    from app.categories import CATEGORIES
    sensitive = [c for c in CATEGORIES if c.sensitive_visuals]
    assert {c.name for c in sensitive} >= {"Politics", "Healthcare"}
    for c in sensitive:
        for shot in c.symbolic_shots or _SYMBOLIC:
            assert not is_risky_shot(c.name, shot), (c.name, shot)
