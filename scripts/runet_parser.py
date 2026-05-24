from __future__ import annotations

from typing import Any, Callable

from other_runet.http_client import RateLimitedHttpClient
from other_runet.search_parser import OtherRunetSearchParser


RunetOpenWebClient = RateLimitedHttpClient


class RunetOpenWebSearchParser:
    def __init__(self, client: RateLimitedHttpClient | None = None) -> None:
        self._inner = OtherRunetSearchParser(http_client=client)

    def search(
        self,
        query: str,
        *,
        page: int = 1,
        limit: int = 10,
        region_name: str = "RU/Moscow",
        use_discovery: bool = True,
        max_search_pages: int = 8,
        max_product_pages: int = 20,
        item_callback: Callable[[dict[str, Any], int], None] | None = None,
    ) -> dict[str, Any]:
        result = self._inner.search(
            query,
            region_name=region_name,
            limit=limit,
            item_callback=item_callback,
            max_discovery_candidates=max_search_pages * 3,
            max_live_candidates=max_product_pages,
        )
        products = result.get("products") or []
        return {
            "schema_version": "0.2",
            "marketplace": "other_runet",
            "query": query,
            "page": page,
            "limit": limit,
            "total": len(products),
            "items": products,
            "products": products,
            "source_meta": {
                "fetched_at": result.get("took_seconds"),
                "adapter": "other_runet",
                "adapter_version": "0.1",
                "endpoints": [],
                "diagnostics": result.get("diagnostics") or [],
                "stats": result.get("stats") or {},
                "use_discovery": use_discovery,
            },
        }
