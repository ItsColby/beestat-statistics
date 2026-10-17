"""Keep a Home Assistant backup's hourly journal and its objects consistent."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .hourly_storage import async_pause_object_collection, resume_object_collection


async def async_pre_backup(hass: HomeAssistant) -> None:
    """Hold object collection until the backup has read the journal's objects."""
    await async_pause_object_collection(hass)


async def async_post_backup(hass: HomeAssistant) -> None:
    """Resume object collection after the backup finishes."""
    resume_object_collection(hass)
