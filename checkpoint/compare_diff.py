"""Pure function: structural diff between two run records."""
from __future__ import annotations


def build_compare_diff(rec_a: dict, rec_b: dict) -> dict:
    sat_a = rec_a.get("satisfaction", 0.0)
    sat_b = rec_b.get("satisfaction", 0.0)
    delta = sat_b - sat_a

    crit_a = {c["text"]: c for c in (rec_a.get("criteria") or [])}
    crit_b = {c["text"]: c for c in (rec_b.get("criteria") or [])}
    all_texts = sorted(set(crit_a) | set(crit_b))

    criterion_diffs: list[dict] = []
    for text in all_texts:
        ca = crit_a.get(text)
        cb = crit_b.get(text)
        pa = ca.get("passed", False) if ca else None
        pb = cb.get("passed", False) if cb else None
        if pa == pb:
            change = "same"
        elif pa is None:
            change = "added"
        elif pb is None:
            change = "removed"
        elif pb and not pa:
            change = "fixed"
        else:
            change = "regressed"
        criterion_diffs.append({
            "text": text,
            "baseline_passed": pa,
            "candidate_passed": pb,
            "change": change,
        })

    return {
        "baseline_score": sat_a,
        "candidate_score": sat_b,
        "delta": round(delta, 1),
        "regressions": [d for d in criterion_diffs if d["change"] == "regressed"],
        "fixes": [d for d in criterion_diffs if d["change"] == "fixed"],
        "same": [d for d in criterion_diffs if d["change"] == "same"],
        "added": [d for d in criterion_diffs if d["change"] == "added"],
        "removed": [d for d in criterion_diffs if d["change"] == "removed"],
        "criteria": criterion_diffs,
    }
