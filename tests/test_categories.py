import pytest

from app.categories import CATEGORIES, NAMES, get_category, normalize_category, prompt_listing
from app.synth.schemas import MetadataOut


def test_taxonomy_is_the_five_agreed_categories():
    assert NAMES == ("Technology", "Politics", "Finance", "Entertainment", "Launches")


@pytest.mark.parametrize("raw,expected", [
    ("Technology", "Technology"), ("technology", "Technology"),
    ("Technology / Privacy", "Technology"), ("Artificial Intelligence", "Technology"),
    ("Politics & World", "Politics"), ("war", "Politics"), ("Election", "Politics"),
    ("Business", "Finance"), ("stock market", "Finance"), ("crypto", "Finance"),
    ("Sports", "Entertainment"), ("movies", "Entertainment"),
    ("new launches", "Launches"), ("motorcycles", "Launches"), ("cars", "Launches"),
])
def test_normalizes_labels_into_the_taxonomy(raw, expected):
    assert normalize_category(raw) == expected


@pytest.mark.parametrize("raw", ["Knitting", "", None, "   "])
def test_unknown_labels_are_not_guessed(raw):
    assert normalize_category(raw) is None


def test_every_synonym_resolves_to_exactly_one_category():
    seen = {}
    for c in CATEGORIES:
        for key in (c.name, c.slug, *c.synonyms):
            assert seen.setdefault(key.lower(), c.name) == c.name, f"{key!r} is ambiguous"


def test_only_politics_has_sensitive_visuals():
    assert [c.name for c in CATEGORIES if c.sensitive_visuals] == ["Politics"]


def test_model_output_schema_rejects_an_invented_category():
    base = dict(title_variants=["a"], target_keywords=[], hashtags=[], thumbnail_text="t", music_mood="m", region="Global", story_label="A story")
    assert MetadataOut(category="Finance", **base).category == "Finance"
    with pytest.raises(Exception):
        MetadataOut(category="Technology / Privacy", **base)


def test_prompt_lists_every_category():
    listing = prompt_listing()
    assert all(name in listing for name in NAMES)
