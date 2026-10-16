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


def test_headline_is_withheld_from_footage_prompts_only_for_politics():
    h = "Leader X orders troops to border"
    assert footage_context("Politics", h) == ""
    assert footage_context("Technology", h) == h
