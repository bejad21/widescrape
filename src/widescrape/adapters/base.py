"""Base adapter contract for WideScrape sources."""

from __future__ import annotations

from abc import ABC, abstractmethod

from widescrape.expander import SearchPlan
from widescrape.models import ScrapeItem


class BaseAdapter(ABC):
    """All source adapters implement fetch() and return normalized ScrapeItems."""

    name: str = "base"

    def __init__(self, proxy: str | None = None) -> None:
        self.proxy = proxy

    @abstractmethod
    async def fetch(self, plan: SearchPlan, limit: int = 30) -> list[ScrapeItem]:
        """Fetch items matching the expanded SearchPlan."""
