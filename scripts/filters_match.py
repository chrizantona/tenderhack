from __future__ import annotations

from typing import Any, Iterable

from filters_common import similarity


MATCH_THRESHOLD = 0.45


def _index_by_source(filters: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_source: dict[str, list[dict[str, Any]]] = {}
    for f in filters:
        by_source.setdefault(f["source"], []).append(f)
    return by_source


def match_filters(filters_by_source: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    sources = sorted(filters_by_source.keys())
    flat: list[dict[str, Any]] = []
    for source in sources:
        for idx, entry in enumerate(filters_by_source[source]):
            flat.append({"source": source, "index": idx, "filter": entry})

    # Build candidate pairs (different sources, similarity >= threshold)
    pairs: list[tuple[float, int, int]] = []
    for i in range(len(flat)):
        a = flat[i]
        for j in range(i + 1, len(flat)):
            b = flat[j]
            if a["source"] == b["source"]:
                continue
            label_a = a["filter"]["label"]
            label_b = b["filter"]["label"]
            score = similarity(label_a, label_b)
            if score >= MATCH_THRESHOLD:
                pairs.append((score, i, j))

    pairs.sort(reverse=True)

    parent = list(range(len(flat)))
    rank = [0] * len(flat)
    used_score: list[float] = [0.0] * len(flat)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def has_source_clash(root: int, candidate_source: str) -> bool:
        for k in range(len(flat)):
            if find(k) == root and flat[k]["source"] == candidate_source:
                return True
        return False

    def union(x: int, y: int) -> bool:
        rx, ry = find(x), find(y)
        if rx == ry:
            return False
        if rank[rx] < rank[ry]:
            rx, ry = ry, rx
        parent[ry] = rx
        if rank[rx] == rank[ry]:
            rank[rx] += 1
        return True

    pair_scores: dict[tuple[int, int], float] = {}
    for score, i, j in pairs:
        ri, rj = find(i), find(j)
        if ri == rj:
            pair_scores[(min(i, j), max(i, j))] = score
            continue
        # Avoid merging groups that already contain the same source twice
        # Need to check: for each member of group ri, is its source in group rj?
        sources_i = {flat[k]["source"] for k in range(len(flat)) if find(k) == ri}
        sources_j = {flat[k]["source"] for k in range(len(flat)) if find(k) == rj}
        if sources_i & sources_j:
            continue
        if union(i, j):
            pair_scores[(min(i, j), max(i, j))] = score
            used_score[i] = max(used_score[i], score)
            used_score[j] = max(used_score[j], score)

    # Collect groups
    groups_map: dict[int, list[int]] = {}
    for idx in range(len(flat)):
        root = find(idx)
        groups_map.setdefault(root, []).append(idx)

    matched_groups: list[dict[str, Any]] = []
    unmatched: dict[str, list[dict[str, Any]]] = {source: [] for source in sources}
    matched_indices: set[int] = set()

    for members in groups_map.values():
        if len(members) < 2:
            continue
        member_filters = [flat[m]["filter"] for m in members]
        present_sources = sorted({flat[m]["source"] for m in members})
        # Compute average pairwise score among members
        scores: list[float] = []
        for i_idx in range(len(members)):
            for j_idx in range(i_idx + 1, len(members)):
                a, b = members[i_idx], members[j_idx]
                key = (min(a, b), max(a, b))
                if key in pair_scores:
                    scores.append(pair_scores[key])
                else:
                    scores.append(similarity(
                        flat[a]["filter"]["label"],
                        flat[b]["filter"]["label"],
                    ))
        avg_score = sum(scores) / len(scores) if scores else 0.0
        canonical_label = sorted(
            member_filters,
            key=lambda f: (-len(f["content_tokens"]), len(f["label"])),
        )[0]["label"]
        matched_groups.append(
            {
                "canonical_label": canonical_label,
                "sources": present_sources,
                "members": member_filters,
                "score": round(avg_score, 3),
            }
        )
        for m in members:
            matched_indices.add(m)

    matched_groups.sort(key=lambda g: (-len(g["sources"]), -g["score"], g["canonical_label"]))

    for idx, entry in enumerate(flat):
        if idx in matched_indices:
            continue
        unmatched[entry["source"]].append(entry["filter"])

    # Coverage stats per source
    coverage: dict[str, dict[str, Any]] = {}
    for source in sources:
        total = len(filters_by_source[source])
        if total == 0:
            coverage[source] = {"total": 0, "matched": 0, "ratio": 0.0}
            continue
        matched = sum(
            1 for idx, entry in enumerate(flat)
            if entry["source"] == source and idx in matched_indices
        )
        coverage[source] = {
            "total": total,
            "matched": matched,
            "ratio": round(matched / total, 3) if total else 0.0,
        }

    # Overall metric: share of filters present in at least one match across all sources
    total_filters = sum(coverage[s]["total"] for s in sources)
    total_matched = sum(coverage[s]["matched"] for s in sources)

    # Per-source-pair stats: for each pair of sources, how many groups contain both
    pair_stats: list[dict[str, Any]] = []
    for i_idx, src_a in enumerate(sources):
        for src_b in sources[i_idx + 1 :]:
            pair_count = sum(
                1 for group in matched_groups
                if src_a in group["sources"] and src_b in group["sources"]
            )
            min_total = min(coverage[src_a]["total"], coverage[src_b]["total"]) or 1
            pair_stats.append(
                {
                    "sources": [src_a, src_b],
                    "matched_groups": pair_count,
                    "ratio_over_min": round(pair_count / min_total, 3),
                }
            )

    return {
        "sources": sources,
        "matched_groups": matched_groups,
        "unmatched": unmatched,
        "coverage": coverage,
        "totals": {
            "total_filters": total_filters,
            "matched_filters": total_matched,
            "matched_ratio": round(total_matched / total_filters, 3) if total_filters else 0.0,
            "matched_groups": len(matched_groups),
        },
        "pair_stats": pair_stats,
        "threshold": MATCH_THRESHOLD,
    }


def match_filter_lists(*lists: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, list[dict[str, Any]]] = {}
    for items in lists:
        for f in items:
            merged.setdefault(f["source"], []).append(f)
    return match_filters(merged)


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) >= 2:
        query = sys.argv[1]
    else:
        query = "холодильник"

    from filters_wb import fetch_wildberries_filters
    from filters_ym import parse_yandex_market_filters_file
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    wb_filters, _wb_meta = fetch_wildberries_filters(query)

    ym_fixture = root / "YM.html"
    ym_filters = parse_yandex_market_filters_file(ym_fixture) if ym_fixture.exists() else []

    ozon_fixture = root / "out" / "ozon_filters.html"
    if ozon_fixture.exists():
        from filters_ozon import parse_ozon_filters_file
        ozon_filters = parse_ozon_filters_file(ozon_fixture)
    else:
        ozon_filters = []

    result = match_filter_lists(ym_filters, wb_filters, ozon_filters)
    print(json.dumps(result, ensure_ascii=False, indent=2))
