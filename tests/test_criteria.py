from issue_runner.criteria import parse, tick

ISSUE_337 = """## Problem

On the Summary tab, the left panel only scrolls with the whole tab.

## Required behaviour

1. The left panel scrolls on its own.
2. The main column scrolls on its own.

## Acceptance criteria

- [ ] In Dev at 1920x1080, scrolling the left panel leaves Resource Loading where it is.
- [ ] The left panel's bottom edge stays inside the window.
- [ ] Expanding every crew panel keeps the left panel scrollable to its last person.
"""

ISSUE_110 = """When a run finishes, show a detailed summary page.

Acceptance criteria:
- Summary page is displayed automatically on run completion (success and failure).
- All three sections above are populated from real run data, not placeholders.
- The page is dismissible and does not block further runs.

## Presentation

- The overlay is scrollable.
"""


def test_parses_checkbox_criteria_under_a_heading_and_ignores_other_lists():
    found = parse(ISSUE_337)
    assert [c.id for c in found] == ["AC1", "AC2", "AC3"]
    assert found[0].text.startswith("In Dev at 1920x1080")
    assert all(c.checkbox and not c.checked for c in found)


def test_parses_a_plain_label_and_stops_at_the_next_heading():
    found = parse(ISSUE_110)
    assert len(found) == 3
    assert found[2].text == "The page is dismissible and does not block further runs."
    assert not found[0].checkbox


def test_wrapped_and_nested_lines_join_their_criterion():
    body = "### Acceptance Criteria\n\n1. First part\n   continued here\n   - nested detail\n2. Second\n"
    found = parse(body)
    assert [c.text for c in found] == ["First part continued here - nested detail", "Second"]


def test_an_intro_sentence_is_allowed_and_trailing_prose_ends_the_list():
    body = "**Acceptance criteria**\nAll of these must hold:\n- [x] one\n- [ ] two\nThanks!\n- not a criterion\n"
    found = parse(body)
    assert [(c.text, c.checked) for c in found] == [("one", True), ("two", False)]


def test_no_section_means_no_criteria():
    assert parse("## Problem\n\n- a list that is not criteria\n") == []
    assert parse(None) == []


def test_tick_marks_only_passed_unchecked_boxes_and_keeps_everything_else():
    found = parse(ISSUE_337)
    updated = tick(ISSUE_337, found, {"AC1", "AC3"})
    lines = updated.splitlines()
    assert lines[found[0].line].startswith("- [x] In Dev")
    assert lines[found[1].line].startswith("- [ ] The left panel")
    assert lines[found[2].line].startswith("- [x] Expanding")
    assert updated.replace("[x]", "[ ]") == ISSUE_337


def test_tick_leaves_a_changed_line_alone():
    found = parse(ISSUE_337)
    edited = ISSUE_337.replace("- [ ] The left panel's", "Someone rewrote this line")
    assert tick(edited, found, {"AC2"}) == edited
