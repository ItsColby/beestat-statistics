"""Object collection for independent-hour history at rest.

Only a completed operation whose durable root equals the adopted cache is
collected. Proof and operation generations outside the root are swept, fences
are kept, and of sources only unreachable integration deltas built on a
superseded baseline are deleted.
"""

from __future__ import annotations

from typing import Any, cast

from .hourly_history_contract import digest, require_digest
from .hourly_history_io import WriterIO
from .hourly_history_root import _ROOT_VERSIONS
from .hourly_import_core import HISTORY_FENCE_PREFIX, HourlyImportError


def _stale_delta(sealed: dict[str, Any], revision: str | None) -> bool:
    """Report an integration delta built on a root revision other than this one."""
    delta = sealed.get("delta")
    return (
        isinstance(delta, dict)
        and sealed.get("confidence") == "integration_delta_v1"
        and delta.get("format") == "integration_delta_v1"
        and delta.get("baseline_source_revision") != revision
    )


class WriterCollection(WriterIO):
    """Delete Store objects the verified root no longer reaches or can admit."""

    async def collect(self, context: dict[str, Any]) -> int:
        """Delete objects the verified root no longer reaches or can admit.

        Runs only at rest: a completed operation with no intent, whose durable
        root equals the adopted cache. Proof and operation generations outside
        the root are swept; fences are kept. Of sources, only integration deltas
        that are unreachable and built on a superseded baseline are deleted.
        """
        state = await self.root(context)
        if (
            state is None
            or state["version"] not in _ROOT_VERSIONS
            or state["phase"] != "hourly"
            or state.get("pending")
            or state["operation"]["status"] != "completed"
            or self.manager._state is None
            or digest(self.manager._state) != digest(state)
        ):
            return 0
        history = state["history"]
        manifest = await self.read_object(
            "operation", state["operation"]["object"], context
        )
        if digest(manifest) != state["operation"]["plan_digest"]:
            raise HourlyImportError("history_operation_object_invalid")
        operation = {
            state["operation"]["object"],
            *(batch["object"] for batch in manifest["batches"]),
            *manifest.get("source_catalog", {}).values(),
            *history.get("source_catalog", {}).values(),
            *history.get("invalidation_fences", ()),
            *(
                reference
                for months in history.get("source_holds", {}).values()
                for reference in months.values()
            ),
        }
        proof = {
            reference
            for selected in history["selections"].values()
            for reference in selected["proofs"].values()
        }
        for reference in operation | proof:
            require_digest(reference)
        retired = await self.unreachable_deltas(history, manifest, context)
        if digest(await self.root(context)) != digest(state):
            raise HourlyImportError("history_root_changed_before_collection")
        removed = await self.manager._store.async_collect_objects(
            {"operation": operation, "proof": proof},
            retained_prefix=HISTORY_FENCE_PREFIX,
            closed=lambda: self.manager._closed,
            retired=retired,
        )
        await self.guard(context)
        return cast(int, removed)

    async def unreachable_deltas(
        self,
        history: dict[str, Any],
        operation: dict[str, Any],
        context: dict[str, Any],
    ) -> list[tuple[str, str]]:
        """Name integration deltas that no root reference or future refresh admits.

        A delta binds the root source revision it was built on, so once the root
        has moved past it and nothing reaches it, refresh can never accept it.
        Staged archive/provider originals are never named.
        """
        reachable = await self.reachable_sources(history, operation, context)
        candidates: list[tuple[str, str]] = []
        kept: set[str] = set()
        for source_id in sorted(await self.manager._store.async_object_ids("manifest")):
            if source_id in reachable:
                continue
            try:
                sealed = await self.read_object("manifest", source_id, context)
            except OSError, ValueError:
                # An unreadable unreferenced manifest is inert; keep it as is.
                continue
            original = sealed.get("sha256")
            if not isinstance(original, str):
                continue
            if _stale_delta(sealed, history.get("source_revision")):
                candidates.append((source_id, original))
            else:
                kept.add(original)
        if not candidates:
            return []
        # Originals are content-addressed; keep any another manifest still names.
        for source_id in reachable:
            sealed = await self.read_object("manifest", source_id, context)
            kept.add(sealed.get("sha256", ""))
        retired: list[tuple[str, str]] = []
        for source_id, original in candidates:
            if original not in kept:
                retired.append(("source", original))
            retired.append(("manifest", source_id))
        return retired

    async def reachable_sources(
        self,
        history: dict[str, Any],
        operation: dict[str, Any],
        context: dict[str, Any],
    ) -> set[str]:
        """Collect every source ID the root's lists, catalogs and holds name."""
        reachable = {*history["source_ids"], *operation.get("source_ids", ())}
        for reference in {
            *operation.get("source_catalog", {}).values(),
            *history.get("source_catalog", {}).values(),
        }:
            catalog = await self.read_object("operation", reference, context)
            reachable.update(catalog.get("source_ids", ()))
        for months in history.get("source_holds", {}).values():
            for reference in months.values():
                hold = await self.read_object("operation", reference, context)
                for interval in hold.get("intervals", ()):
                    reachable.update(interval.get("source_ids", ()))
        return reachable
