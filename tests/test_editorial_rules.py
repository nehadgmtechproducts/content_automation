"""The drafting prompt must carry the rules the website's /legal-compliance page promises readers.

The page says accused people are presumed innocent in our wording and that children and survivors of
sexual offences are never identified. Those are promises about this prompt, so they are pinned here:
if someone edits the rule away, this fails and the page has to be changed with it."""
from app.synth.engine import DRAFT_SYSTEM


def _rule(heading: str) -> str:
    start = DRAFT_SYSTEM.index(heading)
    return DRAFT_SYSTEM[start:start + 900]


def test_accused_people_are_never_called_guilty_before_a_court_says_so():
    rule = _rule("Crime, courts and victims")
    assert "accused or charged, never as guilty" in rule
    assert "court has convicted" in rule
    assert "attribute every allegation" in rule


def test_children_and_survivors_of_sexual_offences_are_never_identified_even_if_a_source_does():
    rule = _rule("Crime, courts and victims")
    assert "Never name, describe or otherwise identify a child" in rule
    assert "survivor of a sexual offence" in rule
    assert "even if a source publishes it" in rule


def test_the_rule_applies_to_every_category_not_only_politics():
    assert "in ANY category" in _rule("Crime, courts and victims")
    # and it sits in the shared part of the prompt, before the hard rules that apply to everything
    assert DRAFT_SYSTEM.index("Crime, courts and victims") < DRAFT_SYSTEM.index("Hard rules:")
