"""Tests for normalize_zulip_markdown — table blank lines and bold/link rewrites."""
from zulipmcp.core import normalize_zulip_markdown


def test_injects_blank_before_table():
    text = "Some text\n| h1 | h2 |\n| --- | --- |\n| a | b |"
    assert normalize_zulip_markdown(text) == "Some text\n\n| h1 | h2 |\n| --- | --- |\n| a | b |"


def test_no_double_inject():
    text = "Some text\n\n| h1 | h2 |\n| --- | --- |"
    assert normalize_zulip_markdown(text) == text


def test_table_at_message_start():
    text = "| h1 | h2 |\n| --- | --- |\n| a | b |"
    assert normalize_zulip_markdown(text) == text


def test_skips_fenced_code_block():
    text = "```\nSome text\n| h1 | h2 |\n| --- | --- |\n```"
    assert normalize_zulip_markdown(text) == text


def test_skips_fenced_code_with_lang():
    text = "```python\ndata\n| h1 | h2 |\n| --- | --- |\n```"
    assert normalize_zulip_markdown(text) == text


def test_skips_tilde_fence():
    text = "~~~\n| h1 | h2 |\n| --- | --- |\n~~~"
    assert normalize_zulip_markdown(text) == text


def test_fence_close_needs_matching_char():
    text = "```\n| h1 | h2 |\n| --- | --- |\n~~~\n| h1 | h2 |\n| --- | --- |\n```"
    assert normalize_zulip_markdown(text) == text


def test_fence_close_needs_matching_length():
    text = "````\n| h1 | h2 |\n| --- | --- |\n```\nstill inside\n````"
    assert normalize_zulip_markdown(text) == text


def test_fence_close_rejects_content_after():
    text = "```\n| h1 | h2 |\n| --- | --- |\n```python\nstill fenced\n```"
    assert normalize_zulip_markdown(text) == text


def test_table_after_fenced_block():
    text = "```\ncode\n```\nSome text\n| h1 | h2 |\n| --- | --- |"
    assert normalize_zulip_markdown(text) == "```\ncode\n```\nSome text\n\n| h1 | h2 |\n| --- | --- |"


def test_skips_indented_code():
    text = "paragraph\n    | h1 | h2 |\n    | --- | --- |"
    assert normalize_zulip_markdown(text) == text


def test_skips_tab_indented_code():
    text = "paragraph\n\t| h1 | h2 |\n\t| --- | --- |"
    assert normalize_zulip_markdown(text) == text


def test_skips_blockquote():
    text = "paragraph\n> | h1 | h2 |\n> | --- | --- |"
    assert normalize_zulip_markdown(text) == text


def test_consecutive_tables_separated():
    text = "| h1 | h2 |\n| --- | --- |\n| a | b |\n| h3 | h4 |\n| --- | --- |\n| c | d |"
    assert normalize_zulip_markdown(text) == (
        "| h1 | h2 |\n| --- | --- |\n| a | b |\n\n| h3 | h4 |\n| --- | --- |\n| c | d |"
    )


def test_pipe_in_text_no_separator():
    text = "Choose this | that\nNext paragraph"
    assert normalize_zulip_markdown(text) == text


def test_no_next_line():
    text = "Some text\n| h1 | h2 |"
    assert normalize_zulip_markdown(text) == text


def test_alignment_separators():
    text = "Text\n| left | center | right |\n| :--- | :---: | ---: |\n| a | b | c |"
    assert normalize_zulip_markdown(text) == (
        "Text\n\n| left | center | right |\n| :--- | :---: | ---: |\n| a | b | c |"
    )


def test_no_border_pipes():
    text = "Text\nh1 | h2\n--- | ---\na | b"
    assert normalize_zulip_markdown(text) == "Text\n\nh1 | h2\n--- | ---\na | b"


def test_after_heading():
    text = "## Heading\n| h1 | h2 |\n| --- | --- |"
    expected = "## Heading\n\n| h1 | h2 |\n| --- | --- |"
    assert normalize_zulip_markdown(text) == expected


def test_after_hr():
    text = "text\n\n---\n| h1 | h2 |\n| --- | --- |"
    expected = "text\n\n---\n\n| h1 | h2 |\n| --- | --- |"
    assert normalize_zulip_markdown(text) == expected


def test_spoiler_block_skipped():
    text = "```spoiler Details\n| h1 | h2 |\n| --- | --- |\n```"
    assert normalize_zulip_markdown(text) == text


def test_empty_content():
    assert normalize_zulip_markdown("") == ""


def test_no_tables():
    text = "Just some normal text\nwith multiple lines\nand no tables"
    assert normalize_zulip_markdown(text) == text


def test_multiple_tables_in_message():
    text = (
        "First section\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
        "Second section\n| c | d |\n| --- | --- |\n| 3 | 4 |"
    )
    expected = (
        "First section\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
        "Second section\n\n| c | d |\n| --- | --- |\n| 3 | 4 |"
    )
    assert normalize_zulip_markdown(text) == expected


def test_separator_with_minimal_dashes():
    text = "Text\n| h1 | h2 |\n| - | - |\n| a | b |"
    assert normalize_zulip_markdown(text) == "Text\n\n| h1 | h2 |\n| - | - |\n| a | b |"


def test_real_world_llm_output():
    text = (
        "Here are the results:\n"
        "| Name | Status | Score |\n"
        "| --- | --- | --- |\n"
        "| Alice | Active | 95.2 |\n"
        "| Bob | Pending | 87.0 |"
    )
    expected = (
        "Here are the results:\n\n"
        "| Name | Status | Score |\n"
        "| --- | --- | --- |\n"
        "| Alice | Active | 95.2 |\n"
        "| Bob | Pending | 87.0 |"
    )
    assert normalize_zulip_markdown(text) == expected


# ============================================================================
# Bold/link rewrites — [**text**](url) and bare-URL** breakage
# ============================================================================

def test_whole_bold_link_text_moves_outside():
    text = "[**Add skip_mean parameter**](https://example.com/task/123)"
    expected = "**[Add skip_mean parameter](https://example.com/task/123)**"
    assert normalize_zulip_markdown(text) == expected


def test_inline_code_inside_bold_link_text_still_fixed():
    text = "[**Add `skip_mean` parameter**](https://example.com/task/9)"
    expected = "**[Add `skip_mean` parameter](https://example.com/task/9)**"
    assert normalize_zulip_markdown(text) == expected


def test_partial_bold_stripped_from_link_text():
    text = "[fix the **bold** bug](https://example.com)"
    assert normalize_zulip_markdown(text) == "[fix the bold bug](https://example.com)"


def test_multiple_bold_spans_stripped_from_link_text():
    text = "[**a** and **b**](https://example.com)"
    assert normalize_zulip_markdown(text) == "[a and b](https://example.com)"


def test_bold_around_link_untouched():
    text = "**[already good](https://example.com)**"
    assert normalize_zulip_markdown(text) == text


def test_plain_link_untouched():
    text = "[plain link](https://example.com)"
    assert normalize_zulip_markdown(text) == text


def test_bold_and_link_as_separate_elements_untouched():
    text = "**important** see [the docs](https://example.com)"
    assert normalize_zulip_markdown(text) == text


def test_image_with_bold_alt_untouched():
    text = "![**chart**](https://example.com/plot.png)"
    assert normalize_zulip_markdown(text) == text


def test_bold_bare_url_bracketed():
    text = "**https://example.com/pull/219**"
    expected = "**[https://example.com/pull/219](https://example.com/pull/219)**"
    assert normalize_zulip_markdown(text) == expected


def test_bold_label_with_bare_url_bracketed():
    text = "**PR: https://example.com/pull/219**"
    expected = "**PR: [https://example.com/pull/219](https://example.com/pull/219)**"
    assert normalize_zulip_markdown(text) == expected


def test_bare_url_without_bold_untouched():
    text = "See https://example.com/pull/219 for details"
    assert normalize_zulip_markdown(text) == text


def test_url_inside_existing_link_untouched():
    text = "**bold** [title](https://example.com/pull/219)"
    assert normalize_zulip_markdown(text) == text


def test_bold_link_inside_inline_code_untouched():
    text = "`[**keep**](https://example.com)`"
    assert normalize_zulip_markdown(text) == text


def test_bold_url_inside_inline_code_untouched():
    text = "`**https://example.com/pull/1**`"
    assert normalize_zulip_markdown(text) == text


def test_bold_link_inside_backtick_fence_untouched():
    text = "```\n[**keep**](https://example.com)\n**https://example.com/pull/1**\n```"
    assert normalize_zulip_markdown(text) == text


def test_bold_link_inside_tilde_fence_untouched():
    text = "~~~\n[**keep**](https://example.com)\n**https://example.com/pull/1**\n~~~"
    assert normalize_zulip_markdown(text) == text


def test_bold_link_inside_indented_code_untouched():
    text = "paragraph\n\n    [**keep**](https://example.com)\n\t**https://example.com/pull/1**"
    assert normalize_zulip_markdown(text) == text


def test_bold_link_fixed_after_fence_closes():
    text = "```\n[**keep**](https://example.com)\n```\n[**fix**](https://example.com)"
    expected = "```\n[**keep**](https://example.com)\n```\n**[fix](https://example.com)**"
    assert normalize_zulip_markdown(text) == expected


def test_mixed_code_and_link_on_one_line():
    text = "`[**keep**](url)` but [**fix**](https://example.com)"
    expected = "`[**keep**](url)` but **[fix](https://example.com)**"
    assert normalize_zulip_markdown(text) == expected


def test_blockquote_line_still_fixed():
    text = "> [**quoted title**](https://example.com)"
    assert normalize_zulip_markdown(text) == "> **[quoted title](https://example.com)**"


def test_native_mention_and_stream_syntax_untouched():
    text = "@**Full Name** and #**stream>topic** stay as they are"
    assert normalize_zulip_markdown(text) == text


def test_silent_mention_with_link_untouched():
    text = "@_**Someone|170** [said](https://example.com/near/1):"
    assert normalize_zulip_markdown(text) == text


def test_plain_bold_untouched():
    text = "**Status:** all systems nominal"
    assert normalize_zulip_markdown(text) == text


def test_paren_url_whole_bold_link():
    text = "[**Foo**](https://en.wikipedia.org/wiki/Foo_(bar))"
    expected = "**[Foo](https://en.wikipedia.org/wiki/Foo_(bar))**"
    assert normalize_zulip_markdown(text) == expected


def test_paren_url_partial_bold_link():
    text = "[see **x**](https://example.com/a_(1))"
    assert normalize_zulip_markdown(text) == "[see x](https://example.com/a_(1))"


def test_double_backtick_code_span_untouched():
    text = "``[**keep**](https://example.com)`` stays literal"
    assert normalize_zulip_markdown(text) == text


def test_double_backtick_bold_url_untouched():
    text = "``**https://example.com/x**`` stays literal"
    assert normalize_zulip_markdown(text) == text


def test_bold_link_rewrites_idempotent():
    inputs = [
        "[**Add skip_mean parameter**](https://example.com/task/123)",
        "[**Add `skip_mean` parameter**](https://example.com/task/9)",
        "[fix the **bold** bug](https://example.com)",
        "[**a** and **b**](https://example.com)",
        "**https://example.com/pull/219**",
        "**PR: https://example.com/pull/219**",
        "`[**keep**](url)` but [**fix**](https://example.com)",
        "> [**quoted title**](https://example.com)",
        "[**Foo**](https://en.wikipedia.org/wiki/Foo_(bar))",
    ]
    for text in inputs:
        once = normalize_zulip_markdown(text)
        assert normalize_zulip_markdown(once) == once


# ============================================================================
# ZULIPMCP_MARKDOWN_AUTOFIX kill switch
# ============================================================================

def test_kill_switch_disables_all_normalization(monkeypatch):
    table = "Some text\n| h1 | h2 |\n| --- | --- |"
    bold_link = "[**title**](https://example.com)"
    for value in ("0", "false", "FALSE"):
        monkeypatch.setenv("ZULIPMCP_MARKDOWN_AUTOFIX", value)
        assert normalize_zulip_markdown(table) == table
        assert normalize_zulip_markdown(bold_link) == bold_link


def test_kill_switch_removal_restores_rewriting(monkeypatch):
    bold_link = "[**title**](https://example.com)"
    monkeypatch.setenv("ZULIPMCP_MARKDOWN_AUTOFIX", "0")
    assert normalize_zulip_markdown(bold_link) == bold_link
    monkeypatch.delenv("ZULIPMCP_MARKDOWN_AUTOFIX")
    assert normalize_zulip_markdown(bold_link) == "**[title](https://example.com)**"


def test_length_guard_counts_normalized_growth():
    from zulipmcp.mcp import _length_error
    url = "https://example.com/" + "a" * 60
    filler = "x" * (10000 - len(f"**{url}**") - 2)
    content = f"{filler}\n**{url}**"
    assert len(content) <= 10000
    assert _length_error(content) is not None


def test_length_guard_fast_rejects_pathological_input():
    import time
    from zulipmcp.mcp import _length_error
    content = "[" * 200000 + "**"
    start = time.monotonic()
    err = _length_error(content)
    assert err is not None
    assert time.monotonic() - start < 1.0
