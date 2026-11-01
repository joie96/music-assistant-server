"""Setup flow for ListenBrainz recommendations."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.constants import CONF_PROVIDERS
from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.listenbrainz_recommendations.constants import (
    CONF_AUTH_TOKEN,
    CONF_RECOMMENDATION_USER_ID,
    CONF_USERNAME,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig

    from music_assistant.mass import MusicAssistant
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Collect ListenBrainz credentials and create or reconfigure the provider."""
    account_entries = await get_account_config_entries(session.mass)
    user_entry = account_entries[2]
    valid_user_ids = {option.value for option in user_entry.options or []}
    errors: dict[str, str | SetupFlowError] | None = None
    setup_data = {
        **{
            entry.key: session.context.values[entry.key]
            for entry in account_entries
            if entry.key in session.context.values
        },
        **session.context.setup_data,
    }
    while True:
        entries = [
            replace(entry, value=setup_data.get(entry.key, entry.value))
            for entry in account_entries
        ]
        submitted = await session.form(entries, step_id="user", errors=errors, last_step=True)
        setup_data.update(submitted)
        user_id = setup_data.get(CONF_RECOMMENDATION_USER_ID)
        if not isinstance(user_id, str) or user_id not in valid_user_ids:
            errors = {
                CONF_RECOMMENDATION_USER_ID: SetupFlowError(
                    "Select an enabled Music Assistant user"
                )
            }
            continue
        try:
            await session.finish(setup_data)
            return
        except SetupFlowError as err:
            errors = {"base": err}


async def get_account_config_entries(mass: MusicAssistant) -> tuple[ConfigEntry, ...]:
    """Return the account fields shared by setup and the provider configuration page."""
    users = await mass.webserver.auth.list_users()
    return (
        ConfigEntry(key=CONF_USERNAME, type=ConfigEntryType.STRING, required=True),
        ConfigEntry(key=CONF_AUTH_TOKEN, type=ConfigEntryType.SECURE_STRING, required=True),
        ConfigEntry(
            key=CONF_RECOMMENDATION_USER_ID,
            type=ConfigEntryType.STRING,
            required=True,
            options=[
                ConfigValueOption(user.user_id, title=user.display_name or user.username)
                for user in users
                if user.enabled
            ],
        ),
    )


def apply_account_setup_values(mass: MusicAssistant, config: ProviderConfig) -> None:
    """Consume newly submitted account setup values into the normal provider configuration."""
    keys = (CONF_USERNAME, CONF_AUTH_TOKEN, CONF_RECOMMENDATION_USER_ID)
    pending = {
        key: mass.config.get_provider_setup_value(config.instance_id, key)
        for key in keys
        if key in config.setup_data
    }
    if not pending:
        return
    for key, value in pending.items():
        mass.config.set_raw_provider_config_value(
            config.instance_id, key, value, encrypted=key == CONF_AUTH_TOKEN
        )
    for key in pending:
        mass.config.remove(f"{CONF_PROVIDERS}/{config.instance_id}/setup_data/{key}")
        config.setup_data.pop(key)
    mass.config.save(immediate=True)
