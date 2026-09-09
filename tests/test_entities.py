from app.detect.entities import canonicalize_entity


def test_aliases_collapse_to_canonical():
    assert canonicalize_entity("Open AI") == "OpenAI"
    assert canonicalize_entity("DeepMind") == "Google DeepMind"
    assert canonicalize_entity("NVIDIA Corp") == "Nvidia"
    assert canonicalize_entity("Meta Platforms") == "Meta"


def test_legal_suffixes_stripped():
    assert canonicalize_entity("Anthropic PBC") == "Anthropic"
    assert canonicalize_entity("Acme Robotics Inc.") == "Acme Robotics"


def test_noise_rejected():
    assert canonicalize_entity("AI") is None
    assert canonicalize_entity("the") is None
    assert canonicalize_entity("  ") is None
    assert canonicalize_entity("reuters") is None
    # lowercase multi-word fragments are NER noise, not proper nouns
    assert canonicalize_entity("some vague phrase") is None
