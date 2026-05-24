from __future__ import annotations

import json
import tempfile
from pathlib import Path

from search_quality import build_query_plan, score_json_result


def main() -> int:
    plan = build_query_plan("Kingston DDR4 16GB SO-DIMM 3200MHz CL22 1.2V")
    assert plan.detected_category == "memory"
    assert plan.variants[0].level == "strict"
    assert plan.variants[1].level == "core"
    assert "cl22" in plan.variants[1].relaxed_terms

    typo_plan = build_query_plan("принтер hp lazerjet")
    assert "laserjet" in typo_plan.normalized_query
    assert typo_plan.detected_category == "office_equipment"

    payload = {
        "items": [
            {
                "title": "Креатин моногидрат 300 г",
                "brand": {"name": "Prime Kraft"},
                "category": {"name": "Креатины"},
                "price": {"current": 630},
                "media": {"main_image_url": "https://example.test/a.webp"},
                "attributes": {"raw": {"Форма": "порошок"}, "normalized": {"model": "Креатин моногидрат"}},
                "match_keys": {"semantic_key": "prime kraft креатин моногидрат 300 г"},
            },
            {
                "title": "Шейкер спортивный",
                "brand": {"name": "No Name"},
                "category": {"name": "Шейкеры"},
                "price": {"current": 300},
                "media": {},
                "attributes": {"raw": {}, "normalized": {}},
                "match_keys": {"semantic_key": "шейкер спортивный"},
            },
        ]
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "items.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        scored = score_json_result(path, query="креатин моногидрат", limit=2)

    assert scored["items"][0]["title"].startswith("Креатин")
    assert scored["items"][0]["search_quality"]["features"]["unit_kind"] == "weight_g"
    assert scored["items"][0]["search_quality"]["features"]["log_price"] is not None
    print(json.dumps({"status": "ok", "checked": "search_quality"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
