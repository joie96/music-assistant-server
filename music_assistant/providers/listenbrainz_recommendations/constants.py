"""Constants for ListenBrainz recommendations provider."""

from music_assistant_models.enums import ProviderFeature

CONF_USERNAME = "username"
CONF_AUTH_TOKEN = "auth_token"
CONF_API_BASE_URL = "api_base_url"
CONF_LB_RADIO_POP_BEGIN = "lb_radio_pop_begin"
CONF_LB_RADIO_POP_END = "lb_radio_pop_end"
CONF_LB_RADIO_MODE = "lb_radio_mode"

DEFAULT_API_BASE_URL = "https://api.listenbrainz.org"
REFRESH_INTERVAL_HOURS = 6
REFRESH_TASK_ID = "refresh_listenbrainz_recommendations"

SUPPORTED_FEATURES = {ProviderFeature.RECOMMENDATIONS}

GENRE_PLAYLISTS_LIMIT = 15
GENRE_ARTISTS_GENRE_LIMIT = 5
GENRE_ARTISTS_PER_GENRE_LIMIT = 15
ARTIST_PLAYLISTS_LIMIT = 15
ARTIST_PLAYLISTS_FEATURED_ARTISTS_LIMIT = 10
TOP_ARTISTS_LIMIT = 15
TOP_TRACKS_LIMIT = 15
SIMILAR_ARTISTS_SEED_LIMIT = 5
SIMILAR_ARTISTS_PER_SEED_LIMIT = 10

LB_RADIO_TRACK_LIMIT = 30

STATS_RANGE = ["month","this_month"]