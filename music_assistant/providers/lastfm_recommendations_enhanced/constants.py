"""Constants for the independent Last.fm Recommendations Enhanced provider."""

from typing import Final

CONF_API_KEY: Final[str] = "api_key"
CONF_RECOMMENDATION_USER_ID: Final[str] = "recommendation_user_id"
FAVORITE_BONUS: Final[float] = 0.2
REFRESH_INTERVAL_HOURS: Final[int] = 24
REFRESH_TASK_ID: Final[str] = "refresh_lastfm_recommendations_enhanced"
