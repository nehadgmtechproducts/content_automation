"""The validator that stops a confidently invented number reaching publication."""
from app.synth.engine import validate_grounding

FACT_SHEET = {
    "headline_summary": "Nvidia will license NVLink to third-party chip designers.",
    "consensus_facts": [
        {"statement": "Nvidia will license NVLink to outside designers.",
         "supporting_source_indices": [1, 2]},
        {"statement": "NVLink offers 1.8 TB/s of bandwidth per GPU.",
         "supporting_source_indices": [1, 3]},
    ],
    "numbers": [{"value": "$4.2 billion", "describes": "quarterly data centre revenue",
                 "supporting_source_indices": [2]}],
    "timeline": [{"when": "2026", "what": "programme announced"}],
    "disputed_claims": [],
}


def draft_with(body: str, narration: str = "Nvidia opens NVLink."):
    return {
        "article": {"dek": "", "why_it_matters": "", "key_takeaways": [],
                    "sections": [{"heading": "H", "body_markdown": body}]},
        "script": {"beats": [{"narration": narration}]},
    }


def test_grounded_figures_pass():
    draft = draft_with("NVLink offers 1.8 TB/s and revenue hit $4.2 billion.")
    assert validate_grounding(draft, FACT_SHEET) == []


def test_hallucinated_figure_is_caught():
    draft = draft_with("Nvidia said the programme covers 47 partners.")
    problems = validate_grounding(draft, FACT_SHEET)
    assert "47" in problems


def test_hallucinated_money_is_caught():
    draft = draft_with("Revenue reached $9.9 billion this quarter.")
    assert any("9.9" in p for p in problems_of(draft))


def problems_of(draft):
    return validate_grounding(draft, FACT_SHEET)


def test_narration_is_checked_too():
    """A figure invented only in the video script is just as published as one in the
    article — the script must be validated as well."""
    draft = draft_with("All figures here are fine.", narration="It is a 63 percent jump.")
    assert "63" in validate_grounding(draft, FACT_SHEET)


def test_years_and_small_counts_are_not_flagged():
    draft = draft_with("In 2026, the top 3 vendors responded within 2 days.")
    assert validate_grounding(draft, FACT_SHEET) == []


def test_formatting_differences_do_not_trip_the_check():
    draft = draft_with("Revenue was $4,200,000,000 — or 1.8 TB/s of bandwidth.")
    problems = validate_grounding(draft, FACT_SHEET)
    assert "1.8" not in problems
