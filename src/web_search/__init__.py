"""Web search providers used to supplement ViecLamBot's own job database."""

from src.web_search.you_search import YouSearchClient, YouSearchError

__all__ = ["YouSearchClient", "YouSearchError"]
