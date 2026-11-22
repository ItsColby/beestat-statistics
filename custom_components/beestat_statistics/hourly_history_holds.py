"""Source holds: hours a routine source dispute keeps out of Recorder.

A hold names each disputed hour and the sources behind it in a
content-addressed monthly object. Recording one replaces the root without
creating a Recorder intent; a verified batch clears the holds inside its range.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

from .hourly_history_contract import bounds, digest, require_digest, utc
from .hourly_history_io import WriterIO
from .hourly_history_root import status
from .hourly_import_core import MARKER, HourlyImportError

_HOUR = timedelta(hours=1)


class WriterHolds(WriterIO):
    """Read, record and clear the source holds an adopted root names."""

    async def hold(
        self, history: dict[str, Any], key: str, month: str, context: dict[str, Any]
    ) -> dict[str, Any]:
        reference = history.get("source_holds", {}).get(key, {}).get(month)
        if reference is None:
            return {
                "kind": "history_source_hold",
                "quantity_id": key,
                "month": month,
                "intervals": [],
            }
        value = await self.read_object("operation", reference, context)
        if (value.get("kind"), value.get("quantity_id"), value.get("month")) != (
            "history_source_hold",
            key,
            month,
        ):
            raise HourlyImportError("history_source_hold_invalid")
        for interval in value["intervals"]:
            start, end = bounds(interval["start"], interval["end"])
            if (
                start.strftime("%Y-%m") != month
                or end - start > _HOUR
                or interval["reason"]
                not in {"source_conflict", "source_timestamp_unplaced"}
            ):
                raise HourlyImportError("history_source_hold_invalid")
            for source_id in interval["source_ids"]:
                require_digest(source_id)
        return value

    async def block_disputed_source(
        self,
        root: dict[str, Any],
        batches: list[dict[str, Any]],
        source_ids: list[str],
        context: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Fence routine source disputes without creating a Recorder intent."""
        reasons: set[str] = set()
        after = None
        for batch in batches:
            if not {"source_conflict", "source_timestamp_unplaced"}.intersection(
                batch["dispositions"]
            ):
                continue
            committed_values = {
                row["start"] for row in batch["before"] if row["mean"] is not None
            }
            first, end = bounds(batch["start"], batch["end"])
            disputed = {
                hour["start"]: hour["status"]
                for hour in batch["proof"]["hours"]
                if first <= utc(hour["start"]) < end
                and (
                    hour["status"] == "source_conflict"
                    or (
                        hour["status"] == "source_timestamp_unplaced"
                        and hour["start"] in committed_values
                    )
                )
            }
            if not disputed:
                continue
            reasons.update(disputed.values())
            key, month = batch["quantity_id"], batch["month"]
            hold = await self.hold(root["history"], key, month, context)
            intervals = {row["start"]: row for row in hold["intervals"]}
            for stamp, reason in disputed.items():
                prior = intervals.get(stamp, {})
                intervals[stamp] = {
                    "start": stamp,
                    "end": (utc(stamp) + _HOUR).isoformat(),
                    "reason": reason,
                    "source_ids": sorted(
                        set(source_ids) | set(prior.get("source_ids", ()))
                    ),
                }
            updated = [intervals[stamp] for stamp in sorted(intervals)]
            if updated != hold["intervals"]:
                reference = await self.write_object(
                    "operation", {**hold, "intervals": updated}, context
                )
                if after is None:
                    after = deepcopy(root)
                index = after["history"].setdefault("source_holds", {})
                index.setdefault(key, {})[month] = reference
        if not reasons:
            return None
        if after is not None:
            await self.commit_source_holds(root, after, context)
        return {
            **status(root if after is None else self.manager._state),
            "status": "blocked",
            "error": "source_conflict"
            if "source_conflict" in reasons
            else "source_timestamp_unplaced",
            "has_pending": False,
        }

    async def commit_source_holds(
        self, root: dict[str, Any], after: dict[str, Any], context: dict[str, Any]
    ) -> None:
        if digest(await self.root(context)) != digest(root):
            raise HourlyImportError("history_root_changed_before_hold")
        after["revision"] += 1
        after["token"] = digest(
            {
                "previous_token": root["token"],
                "source_holds": after["history"]["source_holds"],
                "revision": after["revision"],
            }
        )
        # The deterministic immutable fence survives even if HA's independently
        # delayed entry marker and the replacement root both fail to persist.
        fence = await self.manager._invalidate_history_root(root["token"])
        after["history"]["invalidation_fences"] = sorted(
            set(after["history"].get("invalidation_fences", ())) | {fence}
        )
        await self.guard(context)
        self.manager._hass.config_entries.async_update_entry(
            self.manager._entry,
            data={
                **self.manager._entry.data,
                MARKER: {"version": after["version"], "token": after["token"]},
            },
        )
        self.manager._state, self.manager._loaded = root, True
        await self.manager._save(after)
        await self.guard(context)

    async def clear_holds(
        self, root: dict[str, Any], batch: dict[str, Any], context: dict[str, Any]
    ) -> None:
        key, month = batch["quantity_id"], batch["month"]
        index = root["history"].get("source_holds", {})
        if month not in index.get(key, {}):
            return
        hold = await self.hold(root["history"], key, month, context)
        first, end = utc(batch["start"]), utc(batch["end"])
        hold["intervals"] = [
            row for row in hold["intervals"] if not first <= utc(row["start"]) < end
        ]
        if hold["intervals"]:
            index[key][month] = await self.write_object("operation", hold, context)
        else:
            del index[key][month]
            if not index[key]:
                del index[key]
