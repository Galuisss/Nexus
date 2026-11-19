"""Whole-return retention, exact inclusive anchors, and append-only originals."""

import json
from copy import copy

import tiktoken

OMISSION_MARKER = "\n\n[... omitted original text ...]\n\n"


def text_tokens(text):
    return len(tiktoken.get_encoding("o200k_base").encode_ordinary(text))


def text_body(item):
    """One text view for a whole native result; media remains in its wire item."""
    output = item["output"]
    if isinstance(output, str):
        return output
    return "\n".join(block["text"] for block in output if block.get("type") == "input_text")


def text_projection_supported(item):
    output = item["output"]
    return isinstance(output, str) or all(block.get("type") == "input_text" for block in output)


def render_ranges(raw, ranges):
    parts, previous_end = [], None
    for interval in ranges:
        if previous_end is not None and interval["start"] > previous_end:
            parts.append(OMISSION_MARKER)
        parts.append(raw[interval["start"] : interval["end"]])
        previous_end = interval["end"]
    return "".join(parts)


def locate_spans(raw, anchors):
    """Resolve ordered, nonoverlapping inclusive pairs, requiring uniqueness.

    For each pair enumerate matches at/after the previous interval's end. Both
    full anchors must fit in the interval. More than one (start,end) is ambiguous,
    including one start with repeated end anchors. No nearest/first-match guess
    is made; later pairs do not disambiguate an earlier pair.
    """
    if not isinstance(anchors, list) or not anchors:
        return [], {"type": "invalid_anchors", "detail": "Expected nonempty anchor pairs."}
    ranges, cursor = [], 0
    for number, pair in enumerate(anchors):
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or any(not isinstance(a, str) or not a for a in pair)
        ):
            return [], {
                "type": "invalid_anchors",
                "span": number,
                "detail": "Both anchors must be nonempty strings.",
            }
        left, right = pair
        matches = []
        a = raw.find(left, cursor)
        while a >= 0:
            b = raw.find(right, a)
            while b >= 0:
                end = b + len(right)
                if end >= a + len(left):
                    matches.append({"start": a, "end": end})
                    if len(matches) == 2:
                        return [], {
                            "type": "ambiguous_anchors",
                            "span": number,
                            "candidate_ranges": matches,
                            "detail": "Multiple ordered matches; use longer anchors.",
                        }
                b = raw.find(right, b + 1)
            a = raw.find(left, a + 1)
        if not matches:
            return [], {
                "type": "unmatched_or_out_of_order_anchors",
                "span": number,
                "search_start": cursor,
            }
        ranges.append(matches[0])
        cursor = matches[0]["end"]
    return ranges, None


def parse_decisions(reply, source_ids):
    """Return normalized decisions and diagnostics; invalid entries keep raw."""
    ids = list(source_ids)
    decisions = {sid: {"mode": "keep"} for sid in ids}
    diagnostics = []

    def unique_object(pairs):
        value = {}
        for key, entry in pairs:
            if key in value:
                raise ValueError(f"Duplicate JSON key: {key}")
            value[key] = entry
        return value

    try:
        plan = (
            json.loads(reply, object_pairs_hook=unique_object) if isinstance(reply, str) else reply
        )
    except (ValueError, TypeError) as exc:
        return decisions, [{"type": "invalid_json", "detail": str(exc), "fallback_sources": ids}]
    if not isinstance(plan, dict):
        return decisions, [{"type": "invalid_plan", "fallback_sources": ids}]
    for sid in plan.keys() - set(ids):
        diagnostics.append({"type": "unknown_source_id", "source_id": sid})
    for sid in ids:
        value = plan.get(sid)
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("mode"), str)
            or value["mode"] not in {"keep", "extract", "summary"}
        ):
            diagnostics.append(
                {"type": "missing_or_invalid_decision", "source_id": sid, "fallback": "keep"}
            )
            continue
        mode = value["mode"]
        if mode == "extract" and (not isinstance(value.get("spans"), list) or not value["spans"]):
            diagnostics.append({"type": "invalid_extract", "source_id": sid, "fallback": "keep"})
        elif mode == "summary" and not isinstance(value.get("text"), str):
            diagnostics.append({"type": "invalid_summary", "source_id": sid, "fallback": "keep"})
        else:
            decisions[sid] = (
                {"mode": "keep"}
                if mode == "keep"
                else {"mode": "extract", "spans": value["spans"]}
                if mode == "extract"
                else {"mode": "summary", "text": value["text"]}
            )
    return decisions, diagnostics


def _replace_text(item, retained):
    changed = copy(item)
    if isinstance(item["output"], str):
        changed["output"] = retained
        return changed
    output, first = [], True
    for block in item["output"]:
        if block.get("type") == "input_text":
            output.append({**block, "text": retained if first else ""})
            first = False
        else:
            output.append(block)
    changed["output"] = output
    return changed


def format_tool_output(item, source_id):
    """Label a return while preserving its original text and media blocks."""
    marker = f"Tool output (source: {source_id}):\n"
    changed = copy(item)
    if not text_projection_supported(item):
        changed["output"] = [{"type": "input_text", "text": marker}, *item["output"]]
        return changed
    if isinstance(item["output"], str):
        changed["output"] = marker + item["output"]
    elif item["output"]:
        changed["output"] = [
            {**item["output"][0], "text": marker + item["output"][0]["text"]},
            *item["output"][1:],
        ]
    else:
        changed["output"] = [{"type": "input_text", "text": marker}]
    return changed


def apply_decision(original, source_id, decision):
    """Project one archived native output; an invalid selection keeps its body."""
    marked = format_tool_output(original, source_id)
    if not text_projection_supported(original):
        return marked, None
    raw = text_body(original)
    mode = decision.get("mode", "keep")
    if mode == "extract":
        ranges, diagnostic = locate_spans(raw, decision.get("spans"))
        if diagnostic:
            return marked, diagnostic
        retained = render_ranges(raw, ranges)
        label = "Extracted"
    elif mode == "summary":
        retained = decision["text"]
        label = "Summarized"
    else:
        return marked, None
    if retained == raw:
        return marked, None
    body = (
        f"Tool output (source: {source_id}):\n{label} tool output (source: {source_id}):\n"
        + retained
    )
    original_tokens = text_tokens(text_body(marked))
    projected_tokens = text_tokens(body)
    if projected_tokens > original_tokens:
        return marked, {
            "type": "token_growth",
            "original_tokens": original_tokens,
            "projected_tokens": projected_tokens,
        }
    return _replace_text(original, body), None
