"""Tests for the Last.fm Enhanced scaffold and mandatory custom credentials."""

import logging
import runpy
from unittest.mock import AsyncMock, MagicMock, Mock, create_autospec

import pytest
from aiohttp import ClientConnectionError
from music_assistant_models.enums import ConfigEntryType
from music_assistant_models.errors import (
    AuthenticationFailed,
    InvalidDataError,
    ResourceTemporarilyUnavailable,
)
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track

from music_assistant.controllers.webserver.helpers import auth_middleware
from music_assistant.helpers.throttle_retry import ThrottlerManager
from music_assistant.providers import lastfm_recommendations_enhanced as provider_module
from music_assistant.providers.lastfm_recommendations_enhanced import (
    SUPPORTED_FEATURES,
    LastFMRecommendationsEnhancedProvider,
)
from music_assistant.providers.lastfm_recommendations_enhanced.api_client import LastFMAPIClient
from music_assistant.providers.lastfm_recommendations_enhanced.catalog import (
    EXTRA_FOLDERS,
    GenreProfile,
)
from music_assistant.providers.lastfm_recommendations_enhanced.constants import (
    CONF_RECOMMENDATION_USER_ID,
)
from music_assistant.providers.lastfm_recommendations_enhanced.setup_flow import run_setup


@pytest.fixture(autouse=True)
def isolated_throttler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a simulated server-imposed cooldown from affecting independent test cases."""
    monkeypatch.setattr(LastFMAPIClient, "throttler", ThrottlerManager(rate_limit=100))


@pytest.fixture
def http_session() -> MagicMock:
    """Return a shared HTTP session with a successful Last.fm response."""
    session = MagicMock()
    response = session.get.return_value.__aenter__.return_value
    response.status = 200
    response.headers = {}
    response.raise_for_status = Mock()
    response.json = AsyncMock(return_value={"artists": {"artist": []}, "tags": {"tag": []}})
    return session


@pytest.fixture
def setup_data() -> dict[str, str | None]:
    """Return the credentials and MA user collected by the setup flow."""
    return {"api_key": "custom-test-key", CONF_RECOMMENDATION_USER_ID: "selected-user"}


@pytest.fixture
def provider(
    http_session: MagicMock, setup_data: dict[str, str | None]
) -> LastFMRecommendationsEnhancedProvider:
    """Return a provider configured with an explicit test API key."""
    mass = Mock()
    mass.http_session = http_session
    mass.statistics.get_top_items = AsyncMock(return_value=[])
    mass.music.get_controller.return_value.get_library_items_by_query = AsyncMock(return_value=[])
    selected_user = Mock(
        user_id="selected-user", display_name="Selected user", username="selected", enabled=True
    )
    mass.webserver.auth.get_user = AsyncMock(return_value=selected_user)
    mass.webserver.auth.list_users = AsyncMock(return_value=[selected_user])
    mass.tasks.register_scheduled_task.return_value = Mock(schedule=None)
    mass.config.get.return_value = setup_data
    mass.config.decrypt_string.side_effect = lambda value: value
    config = Mock()
    config.name = "Last.fm Enhanced Test"
    config.instance_id = "lastfm_recommendations_enhanced--test"
    config.values = {}
    config.get_value.side_effect = lambda key, default=None: {
        "log_level": "INFO",
    }.get(key, default)
    manifest = Mock()
    manifest.domain = "lastfm_recommendations_enhanced"
    result = LastFMRecommendationsEnhancedProvider(mass, manifest, config, SUPPORTED_FEATURES)
    result._is_unloading = False
    result._recommendation_user_id = "selected-user"
    result._recommendation_folders = []
    result._resolved_virtual_playlists = {}
    return result


async def test_custom_key_is_required_and_sent_to_lastfm(
    provider: LastFMRecommendationsEnhancedProvider, http_session: MagicMock
) -> None:
    """Read the setup key without declaring mandatory credentials as runtime options."""
    entries = await provider.get_config_entries()
    assert not entries
    await provider.handle_async_init()
    assert http_session.get.call_args.kwargs["params"] == {
        "method": "chart.getTopArtists",
        "api_key": "custom-test-key",
        "format": "json",
        "limit": 50,
    }


def test_provider_import_does_not_require_newer_core_user_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import the provider with the same missing helper as Music Assistant 2.11.0b7."""
    monkeypatch.delattr(auth_middleware, "user_auth_context", raising=False)
    assert provider_module.__file__ is not None
    namespace = runpy.run_path(provider_module.__file__)
    assert callable(namespace["_recommendation_user_context"])
    assert namespace["SUPPORTED_FEATURES"] == SUPPORTED_FEATURES


@pytest.mark.parametrize("api_key", [None, "", "   "])
async def test_missing_key_never_uses_bundled_credentials(
    provider: LastFMRecommendationsEnhancedProvider,
    http_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    setup_data: dict[str, str | None],
    api_key: str | None,
) -> None:
    """Reject absent credentials even when an application key is available."""
    monkeypatch.setenv("MASS_APP_VAR_LASTFM_API_KEY", "must-not-be-used")
    setup_data["api_key"] = api_key
    with pytest.raises(AuthenticationFailed):
        await provider.handle_async_init()
    http_session.get.assert_not_called()


@pytest.mark.parametrize("error_code", [4, 10, 26, "10"])
async def test_invalid_key_prevents_initialization(
    provider: LastFMRecommendationsEnhancedProvider,
    http_session: MagicMock,
    error_code: int | str,
) -> None:
    """Map Last.fm credential errors without echoing its response message."""
    response = http_session.get.return_value.__aenter__.return_value
    response.json.return_value = {"error": error_code, "message": "custom-test-key"}
    with pytest.raises(AuthenticationFailed, match="rejected") as error:
        await provider.handle_async_init()
    assert "custom-test-key" not in str(error.value)


@pytest.mark.parametrize("error_code", [11, 16, 29])
async def test_temporary_failures_do_not_report_invalid_credentials(
    http_session: MagicMock, error_code: int
) -> None:
    """Distinguish a service failure or rate limit from a bad API key."""
    response = http_session.get.return_value.__aenter__.return_value
    response.json.return_value = {"error": error_code}
    with pytest.raises(ResourceTemporarilyUnavailable):
        await LastFMAPIClient(http_session, "custom-test-key").validate_api_key()


@pytest.mark.parametrize("payload", [[], {}, {"artists": []}])
async def test_malformed_validation_response(http_session: MagicMock, payload: object) -> None:
    """Do not accept an unrelated successful HTTP response as key validation."""
    response = http_session.get.return_value.__aenter__.return_value
    response.json.return_value = payload
    with pytest.raises(InvalidDataError):
        await LastFMAPIClient(http_session, "custom-test-key").validate_api_key()


async def test_transport_error_does_not_expose_request_credentials(
    http_session: MagicMock,
) -> None:
    """Hide transport exception text because request URLs can contain the API key."""
    http_session.get.side_effect = ClientConnectionError("url?api_key=custom-test-key")
    with pytest.raises(ResourceTemporarilyUnavailable) as error:
        await LastFMAPIClient(http_session, "custom-test-key").validate_api_key()
    assert "custom-test-key" not in str(error.value)
    assert error.value.__suppress_context__


async def test_provider_only_publishes_implemented_folders(
    provider: LastFMRecommendationsEnhancedProvider, http_session: MagicMock
) -> None:
    """Publish the implemented MA toplists but no future Last.fm discovery folders."""
    folders = await provider._get_recommendation_folders(genre_profile=GenreProfile(()))
    assert [folder.item_id for folder in folders] == [
        "user_top_artists_monthly",
        "user_top_tracks_monthly",
        "user_top_albums_monthly",
        "global_top_tracks",
        "de_top_tracks",
        "global_top_artists",
        "de_top_artists",
        "user_recommendations",
        "user_artist_recommendations",
        "user_album_recommendations",
        "user_rediscovery_tracks",
        *(folder_id for folder_id, _name in EXTRA_FOLDERS),
    ]
    assert all(folder.provider == provider.instance_id for folder in folders)
    assert all(folder.translation_key and not folder.items for folder in folders)
    assert not await provider.get_recommendation_items("unknown_folder")
    http_session.get.assert_not_called()


@pytest.mark.parametrize("prefill_user", [None, "selected-user"])
async def test_setup_collects_required_api_key_and_ma_user(
    provider: LastFMRecommendationsEnhancedProvider, prefill_user: str | None
) -> None:
    """Keep credentials and the chosen MA user in setup data supported by older servers."""
    session = Mock()
    session.context.setup_data = {CONF_RECOMMENDATION_USER_ID: prefill_user} if prefill_user else {}
    session.mass.webserver.auth.list_users = AsyncMock(
        return_value=[Mock(user_id="selected-user", display_name="Selected", enabled=True)]
    )
    session.form = AsyncMock(
        return_value={"api_key": "custom-test-key", CONF_RECOMMENDATION_USER_ID: "selected-user"}
    )

    async def legacy_finish(values: dict[str, str | None]) -> dict[str, str]:
        """Apply setup data through the finish signature available in MA 2.11.0b7."""
        provider.mass.config.get.return_value = values
        await provider.handle_async_init()
        return {"instance_id": provider.instance_id}

    session.finish = create_autospec(legacy_finish, side_effect=legacy_finish)
    await run_setup(session)
    entries = session.form.call_args.args[0]
    assert len(entries) == 2
    assert entries[0].key == "api_key"
    assert entries[0].required
    assert entries[0].type == ConfigEntryType.SECURE_STRING
    assert entries[1].key == CONF_RECOMMENDATION_USER_ID
    assert entries[1].required
    assert entries[1].value == prefill_user
    session.finish.assert_awaited_once_with(
        {"api_key": "custom-test-key", CONF_RECOMMENDATION_USER_ID: "selected-user"}
    )
    assert provider._recommendation_user_id == "selected-user"


@pytest.mark.parametrize(
    ("folder_id", "item_class"),
    [
        ("user_top_artists_monthly", Artist),
        ("user_top_tracks_monthly", Track),
        ("user_top_albums_monthly", Album),
    ],
)
async def test_atomic_statistics_rows_do_not_wrap_media_in_containers(
    provider: LastFMRecommendationsEnhancedProvider,
    folder_id: str,
    item_class: type[Artist | Track | Album],
) -> None:
    """Expose the actual media and its artwork instead of an enclosing folder."""
    item = item_class(
        item_id="1",
        provider="library",
        name="Original media",
        provider_mappings={
            ProviderMapping(item_id="1", provider_domain="test", provider_instance="test")
        },
    )
    provider.mass.statistics.get_top_items.return_value = [Mock(item=item)]
    items = await provider._build_recommendation_items(folder_id)
    assert list(items) == [item]
    assert items[0] is item


async def test_initialization_debug_log_uses_provider_logger(
    provider: LastFMRecommendationsEnhancedProvider, caplog: pytest.LogCaptureFixture
) -> None:
    """Trace initialization and request timing through the configured instance logger."""
    with caplog.at_level(logging.DEBUG, logger=provider.logger.name):
        await provider.handle_async_init()
        await provider._get_recommendation_folders(genre_profile=GenreProfile(()))
        await provider._build_recommendation_items("unknown_folder")
    assert provider.api.logger is provider.logger
    assert "initialization start" in caplog.text
    assert "method=chart.getTopArtists status=200" in caplog.text
    assert "elapsed=" in caplog.text
    assert "validation successful" in caplog.text
    assert "folders ready count=19" in caplog.text
    assert "reason=unknown_folder" in caplog.text
    assert "custom-test-key" not in caplog.text
    assert all(record.name == provider.logger.name for record in caplog.records)


@pytest.mark.parametrize("error_code", [10, 29, "custom-test-key"])
async def test_api_error_debug_log_does_not_expose_credentials(
    http_session: MagicMock, caplog: pytest.LogCaptureFixture, error_code: int | str
) -> None:
    """Log known API error codes, never arbitrary Last.fm messages or error values."""
    response = http_session.get.return_value.__aenter__.return_value
    response.json.return_value = {"error": error_code, "message": "custom-test-key"}
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises((AuthenticationFailed, ResourceTemporarilyUnavailable, InvalidDataError)),
    ):
        await LastFMAPIClient(http_session, "custom-test-key").validate_api_key()
    assert "Last.fm API error method=chart.getTopArtists" in caplog.text
    assert "custom-test-key" not in caplog.text


async def test_transport_debug_log_excludes_exception_text(
    http_session: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Keep credential-bearing transport URLs out of DEBUG output and tracebacks."""
    http_session.get.side_effect = ClientConnectionError("url?api_key=custom-test-key")
    with caplog.at_level(logging.DEBUG), pytest.raises(ResourceTemporarilyUnavailable):
        await LastFMAPIClient(http_session, "custom-test-key").validate_api_key()
    assert "error_type=ClientConnectionError" in caplog.text
    assert "elapsed=" in caplog.text
    assert "custom-test-key" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("user_id", [None, "", "missing", "disabled"])
async def test_initialization_requires_an_enabled_selected_user(
    provider: LastFMRecommendationsEnhancedProvider,
    http_session: MagicMock,
    setup_data: dict[str, str | None],
    user_id: str | None,
) -> None:
    """Invalid user choices must not start network requests or background work."""
    setup_data[CONF_RECOMMENDATION_USER_ID] = user_id
    provider.mass.webserver.auth.get_user.return_value = (
        Mock(user_id="disabled", enabled=False) if user_id == "disabled" else None
    )
    with pytest.raises(AuthenticationFailed):
        await provider.handle_async_init()
    http_session.get.assert_not_called()
    provider.mass.tasks.register_scheduled_task.assert_not_called()


@pytest.mark.parametrize("invalid_user", [None, "unknown-user", "disabled-user"])
async def test_setup_reprompts_for_invalid_user(invalid_user: str | None) -> None:
    """Only an enabled id from the offered MA users may be persisted."""
    session = Mock()
    session.context.setup_data = {}
    session.mass.webserver.auth.list_users = AsyncMock(
        return_value=[
            Mock(user_id="selected-user", display_name="Selected", enabled=True),
            Mock(user_id="disabled-user", display_name="Disabled", enabled=False),
        ]
    )
    session.form = AsyncMock(
        side_effect=[
            {"api_key": "custom-test-key", CONF_RECOMMENDATION_USER_ID: invalid_user},
            {"api_key": "custom-test-key", CONF_RECOMMENDATION_USER_ID: "selected-user"},
        ]
    )
    session.finish = AsyncMock()
    await run_setup(session)
    assert session.form.await_count == 2
    assert CONF_RECOMMENDATION_USER_ID in session.form.call_args.kwargs["errors"]
    session.finish.assert_awaited_once_with(
        {"api_key": "custom-test-key", CONF_RECOMMENDATION_USER_ID: "selected-user"}
    )
