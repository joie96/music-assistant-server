"""Configure the shared Mashin Stream provider independently of connected devices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import entries

if TYPE_CHECKING:
    from music_assistant.models.setup_flow import SetupSession


async def run_setup(session: SetupSession) -> None:
    """Collect TLS settings through MA; connected clients select their own profiles."""
    values = await session.form(list(entries()), step_id="user", last_step=True)
    await session.finish(values)
