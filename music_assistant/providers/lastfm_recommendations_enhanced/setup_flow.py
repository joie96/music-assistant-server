"""Collect the mandatory API key without a Last.fm account login."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError
from music_assistant.providers.lastfm_recommendations_enhanced import get_recommendation_user_entry
from music_assistant.providers.lastfm_recommendations_enhanced.constants import (
    CONF_API_KEY,
    CONF_RECOMMENDATION_USER_ID,
)

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Collect the Last.fm key and the MA user whose listening data builds recommendations."""
    user_entry = await get_recommendation_user_entry(session.mass)
    user_entry = replace(user_entry, value=session.context.values.get(CONF_RECOMMENDATION_USER_ID))
    valid_user_ids = {option.value for option in user_entry.options or []}
    errors: dict[str, str | SetupFlowError] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(key=CONF_API_KEY, type=ConfigEntryType.SECURE_STRING, required=True),
                user_entry,
            ],
            step_id="user",
            errors=errors,
            last_step=True,
        )
        user_id = values.get(CONF_RECOMMENDATION_USER_ID)
        if not isinstance(user_id, str) or user_id not in valid_user_ids:
            errors = {
                CONF_RECOMMENDATION_USER_ID: SetupFlowError(
                    "Select an enabled Music Assistant user"
                )
            }
            continue
        try:
            await session.finish(
                {CONF_API_KEY: values.get(CONF_API_KEY)},
                options={CONF_RECOMMENDATION_USER_ID: user_id},
            )
            return
        except SetupFlowError as err:
            errors = {"base": err}
