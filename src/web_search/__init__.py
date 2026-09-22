"""Fresh web and live-board search providers for ViecLamBot."""

from src.web_search.live_boards import search_live_job_boards
from src.web_search.you_search import YouSearchClient, YouSearchError

__all__ = ["YouSearchClient", "YouSearchError", "search_live_job_boards"]
