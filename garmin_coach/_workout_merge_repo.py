"""Workout deduplication and cross-source merging.

Extracted from :class:`garmin_coach.database.Database` as a mixin to keep the
merge subsystem — which is really its own concern — in one focused module. The
pure algorithms live in :mod:`garmin_coach.workout_merge` and
:mod:`garmin_coach.strength_merge`; this mixin is the DB-facing orchestration
around them.

Three distinct operations live here, deliberately kept separate:

* ``dedupe_workouts`` — row-level: a walk recorded by two sources becomes one
  kept row plus others marked ``duplicate_of``.
* ``merge_garmin_strength_fragments`` — folds several *Garmin* fragments of one
  gym session into a single ``garmin_merged`` row.
* ``merge_workout_sources`` — *field-level*: a manual strength log and a Garmin
  activity of the same session are linked into a ``merged`` canonical whose
  every field records which source won.

The mixin relies on methods provided by the composed ``Database`` class
(``session``, ``upsert_workout``, ``get_profile``, ``recent_workouts``,
``get_strength_session``, ``_cutoff``); it is never instantiated on its own.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from sqlmodel import select

from ._db_common import _as_day, synthetic_activity_id
from .models import StrengthSession, Workout, WorkoutSourceLink
from .strength_merge import is_strength_like
from .training_load import estimate_training_load
from .workout_merge import (
    best_strength_match,
    fields_from_source,
    merge_fields,
    normalize_source,
)


class WorkoutMergeMixin:
    """Workout dedupe + cross-source merge methods for :class:`Database`."""

    # ── Workout deduplication ──────────────────────────────────────────────────

    # "merged" (a field-level canonical) ranks first, then garmin_merged (a
    # Garmin-fragment canonical): both are deliberately curated sessions, so a
    # same-day Apple/manual import that looks like a duplicate must never win
    # against them and hide the merged summary.
    _DEFAULT_SOURCE_PRIORITY = ("merged", "garmin_merged", "garmin", "apple", "manual")

    def _source_priority(self) -> list[str]:
        profile = self.get_profile() or {}
        raw = profile.get("activity_source_priority") or ""
        order = [p.strip() for p in raw.split(",") if p.strip()]
        return order or list(self._DEFAULT_SOURCE_PRIORITY)

    @staticmethod
    def _workout_source(w: dict[str, Any]) -> str:
        if w.get("source"):
            return w["source"]
        return "garmin" if w["activity_id"] > 0 else "manual"

    @staticmethod
    def _close(a: float | None, b: float | None, tolerance: float) -> bool | None:
        """True/False when both present, None when incomparable."""
        if a is None or b is None:
            return None
        biggest = max(abs(a), abs(b))
        if biggest == 0:
            return True
        return abs(a - b) / biggest <= tolerance

    @classmethod
    def _looks_duplicate(cls, a: dict[str, Any], b: dict[str, Any]) -> bool:
        """Same-day activities that look like one physical workout recorded
        twice (Garmin + Apple/manual import). Requires comparable duration,
        and rejects on any clearly-different measurable."""
        duration = cls._close(a.get("duration_s"), b.get("duration_s"), 0.15)
        if duration is not True:
            return False
        distance = cls._close(a.get("distance_m"), b.get("distance_m"), 0.15)
        if distance is False:
            return False
        calories = cls._close(
            float(a["calories"]) if a.get("calories") is not None else None,
            float(b["calories"]) if b.get("calories") is not None else None,
            0.25,
        )
        if calories is False:
            return False
        return True

    def find_duplicate_workouts(self, days: int = 60) -> list[list[dict[str, Any]]]:
        """Groups of same-day activities that look like one workout recorded
        by multiple sources. Detection only — nothing is modified."""
        workouts = self.recent_workouts(days=days, include_duplicates=False)
        by_day: dict[str, list[dict[str, Any]]] = {}
        for w in workouts:
            by_day.setdefault(w["day"], []).append(w)

        groups: list[list[dict[str, Any]]] = []
        for day_workouts in by_day.values():
            remaining = list(day_workouts)
            while remaining:
                seed = remaining.pop(0)
                group = [seed]
                still = []
                for other in remaining:
                    if self._looks_duplicate(seed, other):
                        group.append(other)
                    else:
                        still.append(other)
                remaining = still
                if len(group) > 1:
                    groups.append(group)
        return groups

    def dedupe_workouts(self, days: int = 60) -> dict[str, Any]:
        """Reconcile same-workout duplicates across sources.

        First, field-level merge: a manual strength log and a Garmin activity
        of the same session are *linked* into a canonical (manual exercise
        details + Garmin physiology/load) — both source rows are kept, not
        discarded. Then the remaining duplicates (a walk recorded by two
        sources, say) get the classic row-level treatment: the highest-priority
        source is kept and the rest marked ``duplicate_of`` it. Either way the
        session counts once in summaries/training load while every source row
        stays in the database for traceability. Reversible via update_workout /
        unmerge_workout_sources."""
        priority = self._source_priority()

        # Field-level strength merge happens before row-level dedupe: it links
        # the manual↔Garmin pair so find_duplicate_workouts (which reads
        # non-duplicate rows) then sees one canonical, not two competitors.
        merged_sessions = self._merge_strength_window(days)

        def rank(w: dict[str, Any]) -> tuple:
            source = self._workout_source(w)
            idx = priority.index(source) if source in priority else len(priority)
            has_real_load = 0 if w.get("load_source") == "garmin" else 1
            return (idx, has_real_load, -w["activity_id"])

        groups = self.find_duplicate_workouts(days=days)
        marked = []
        with self.session() as s:
            for group in groups:
                keeper, *dupes = sorted(group, key=rank)
                for dupe in dupes:
                    row = s.get(Workout, dupe["activity_id"])
                    row.duplicate_of = keeper["activity_id"]
                    s.add(row)
                    marked.append(
                        {
                            "marked_duplicate": dupe["activity_id"],
                            "kept": keeper["activity_id"],
                            "day": dupe["day"],
                            "name": dupe.get("name"),
                        }
                    )
            s.commit()
        return {
            "groups_found": len(groups),
            "marked": marked,
            "priority": priority,
            "merged_sessions": merged_sessions,
        }

    # ── Garmin strength-fragment merging ────────────────────────────────────────

    def _plan_strength_merge(
        self,
        day_str: str,
        group: list[dict[str, Any]],
        existing_fragment_sets: dict[int, set[int]],
        dry_run: bool,
    ) -> dict[str, Any]:
        """Compute (and, unless ``dry_run``, apply) the merge of one group of
        same-day fragments. ``existing_fragment_sets`` maps a previously
        merged row's activity_id to the fragment ids it currently covers —
        matched by *overlap* rather than exact equality, so a merge stays
        idempotent even if a delayed Garmin sync adds one more fragment to an
        already-merged session on a later call.
        """
        from .strength_merge import weighted_avg_hr

        before_load = round(sum(f.get("training_load") or 0 for f in group), 1)
        total_duration = sum(f.get("duration_s") or 0 for f in group) or None
        total_calories = round(sum(f.get("calories") or 0 for f in group)) or None
        avg_hr = weighted_avg_hr(group)
        max_hr = max(
            (f["max_hr"] for f in group if f.get("max_hr") is not None), default=None
        )
        load_after = estimate_training_load("strength_training", total_duration, avg_hr=avg_hr)
        if load_after is None:
            load_after = before_load
        fragment_ids = sorted(f["activity_id"] for f in group)

        if dry_run:
            return {
                "merged": False, "would_merge": True,
                "fragment_ids": fragment_ids,
                "total_duration_s": total_duration,
                "total_calories": total_calories,
                "avg_hr": avg_hr,
                "max_hr": max_hr,
                "training_load_before": before_load,
                "training_load_after": load_after,
            }

        fragment_id_set = set(fragment_ids)
        merged_id = next(
            (
                activity_id for activity_id, ids in existing_fragment_sets.items()
                if ids & fragment_id_set
            ),
            None,
        )
        if merged_id is None:
            merged_id = synthetic_activity_id()

        self.upsert_workout(
            merged_id, day_str,
            name="Merged Garmin strength session",
            type="strength_training",
            duration_s=total_duration,
            calories=total_calories,
            avg_hr=avg_hr,
            max_hr=max_hr,
            training_load=load_after,
            source="garmin_merged",
            load_source="estimated",
            meta_json=json.dumps({"fragment_ids": fragment_ids}),
        )
        with self.session() as s:
            for fragment_id in fragment_ids:
                fragment = s.get(Workout, fragment_id)
                if fragment is not None:
                    fragment.duplicate_of = merged_id
                    s.add(fragment)
            s.commit()

        return {
            "merged": True, "merged_activity_id": merged_id,
            "fragment_ids": fragment_ids,
            "total_duration_s": total_duration,
            "total_calories": total_calories,
            "avg_hr": avg_hr,
            "max_hr": max_hr,
            "training_load_before": before_load,
            "training_load_after": load_after,
        }

    def merge_garmin_strength_fragments(
        self,
        day: str | date,
        dry_run: bool = False,
        min_fragments: int = 2,
        max_gap_minutes: float = 90.0,
    ) -> dict[str, Any]:
        """Merge same-day Garmin strength-training fragments into one
        canonical workout so training load/recovery math isn't inflated by
        a session the watch recorded as several short activities.

        Only ``source="garmin"`` activities of a strength-like type are
        considered — manual logs and already-merged rows are untouched.
        Originals are never deleted: they're marked ``duplicate_of`` the
        merged row (the same mechanism ``dedupe_workouts`` uses), so
        summaries/training load/workout counts see the session once while
        the raw Garmin rows stay in the database. Re-running for the same
        day (dry_run or not) is idempotent — it recomputes and updates the
        existing merged row(s) instead of creating new ones. If more than
        one group of fragments qualifies (e.g. two separate gym visits the
        same day), every qualifying group is merged: the largest is
        reported at the top level and the rest under ``other_merges`` — so
        a smaller second session is never left permanently double-counted.
        """
        from .strength_merge import group_fragments, is_strength_like

        day_str = _as_day(day)
        with self.session() as s:
            # Not filtered by duplicate_of: a fragment already merged (by an
            # earlier call, for idempotency) must still be found so it can be
            # regrouped and re-matched to its existing merged row.
            rows = s.exec(
                select(Workout)
                .where(Workout.day == day_str)
                .where(Workout.source == "garmin")
            ).all()
            fragments = [r.model_dump() for r in rows if is_strength_like(r.type)]

        if len(fragments) < max(1, min_fragments):
            return {
                "day": day_str, "dry_run": dry_run, "merged": False,
                "reason": (
                    f"found {len(fragments)} strength fragment(s) — need at "
                    f"least {min_fragments} to merge"
                ),
                "fragment_ids": sorted(f["activity_id"] for f in fragments),
            }

        groups = group_fragments(fragments, max_gap_minutes=max_gap_minutes)
        mergeable = sorted(
            (g for g in groups if len(g) >= min_fragments), key=len, reverse=True
        )
        if not mergeable:
            return {
                "day": day_str, "dry_run": dry_run, "merged": False,
                "reason": "fragments are too far apart in time to be one session",
                "groups": [sorted(f["activity_id"] for f in g) for g in groups],
            }

        existing_fragment_sets: dict[int, set[int]] = {}
        if not dry_run:
            with self.session() as s:
                existing_merged = s.exec(
                    select(Workout).where(
                        Workout.day == day_str, Workout.source == "garmin_merged"
                    )
                ).all()
            for existing in existing_merged:
                try:
                    ids = json.loads(existing.meta_json) if existing.meta_json else {}
                except ValueError:
                    ids = {}
                existing_fragment_sets[existing.activity_id] = set(ids.get("fragment_ids", []))

        merges = [
            self._plan_strength_merge(day_str, group, existing_fragment_sets, dry_run)
            for group in mergeable
        ]
        primary, *others = merges
        result = {"day": day_str, "dry_run": dry_run, **primary}
        if others:
            result["other_merges"] = others
        return result

    # ── Field-level source merge (manual strength ↔ Garmin activity) ────────────
    #
    # Distinct from merge_garmin_strength_fragments (which folds several Garmin
    # rows into one Garmin session). Here two *different sources* of the same
    # workout — a manual strength log and a Garmin activity — are linked into a
    # canonical "merged" row whose every field records which source it came
    # from: manual for exercise details, Garmin for physiology/load. Both source
    # rows are preserved (never hard-deleted) and marked ``duplicate_of`` the
    # canonical so summaries/training load count the session once.

    def _apply_source_merge(
        self,
        source_rows: list[dict[str, Any]],
        confidence: float | None,
        reason: str | None,
        existing_canonical_id: int | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Create or refresh one field-level canonical from its source rows.

        ``existing_canonical_id`` reuses (updates) a prior canonical so a
        repeated call — a re-sync, an exercise edit — refreshes rather than
        duplicates. Field priority is resolved in :mod:`workout_merge`.
        """
        day = source_rows[0]["day"]
        is_strength = any(is_strength_like(r.get("type")) for r in source_rows)
        sources = {normalize_source(r): r for r in source_rows}
        merged_cols, provenance = merge_fields(sources, is_strength)

        result: dict[str, Any] = {
            "canonical_activity_id": existing_canonical_id,
            "day": day,
            "linked_sources": [
                {"source": normalize_source(r), "activity_id": r["activity_id"]}
                for r in source_rows
            ],
            "field_sources": provenance,
            "match_confidence": confidence,
            "match_reason": reason,
            "name": merged_cols.get("name"),
            "duration_s": merged_cols.get("duration_s"),
            "avg_hr": merged_cols.get("avg_hr"),
            "max_hr": merged_cols.get("max_hr"),
            "calories": merged_cols.get("calories"),
            "training_load": merged_cols.get("training_load"),
            "load_source": merged_cols.get("load_source"),
        }
        if dry_run:
            result["merged"] = False
            result["would_merge"] = True
            return result

        canonical_id = existing_canonical_id or synthetic_activity_id()
        source_ids = sorted(r["activity_id"] for r in source_rows)
        self.upsert_workout(
            canonical_id, day,
            name=merged_cols.get("name"),
            type=merged_cols.get("type"),
            duration_s=merged_cols.get("duration_s"),
            distance_m=merged_cols.get("distance_m"),
            calories=merged_cols.get("calories"),
            avg_hr=merged_cols.get("avg_hr"),
            max_hr=merged_cols.get("max_hr"),
            start_time=merged_cols.get("start_time"),
            training_load=merged_cols.get("training_load"),
            source="merged",
            load_source=merged_cols.get("load_source"),
            field_sources=json.dumps(provenance, ensure_ascii=False),
            meta_json=json.dumps({"linked": source_ids}),
        )
        with self.session() as s:
            for r in source_rows:
                row = s.get(Workout, r["activity_id"])
                if row is not None:
                    row.duplicate_of = canonical_id
                    s.add(row)
            for r in source_rows:
                src = normalize_source(r)
                link = s.exec(
                    select(WorkoutSourceLink)
                    .where(WorkoutSourceLink.canonical_activity_id == canonical_id)
                    .where(WorkoutSourceLink.source_activity_id == r["activity_id"])
                ).first()
                if link is None:
                    link = WorkoutSourceLink(
                        canonical_activity_id=canonical_id,
                        source_activity_id=r["activity_id"],
                        source=src,
                    )
                link.source = src
                link.match_confidence = confidence
                link.match_reason = reason
                link.fields_imported = json.dumps(fields_from_source(provenance, src))
                link.updated_at = datetime.now(timezone.utc)
                s.add(link)
            # Repoint any strength session from its manual source row to the
            # canonical, so strength_session.activity_id names the workout that
            # stays counted in summaries (not a soon-hidden duplicate).
            for r in source_rows:
                for sess in s.exec(
                    select(StrengthSession).where(
                        StrengthSession.activity_id == r["activity_id"]
                    )
                ).all():
                    sess.activity_id = canonical_id
                    s.add(sess)
            s.commit()

        result["merged"] = True
        result["canonical_activity_id"] = canonical_id
        return result

    def _refresh_canonical(self, canonical_id: int) -> None:
        """Recompute a canonical from its current source rows (after one of
        them was edited/re-synced), keeping field-level priority."""
        with self.session() as s:
            canonical = s.get(Workout, canonical_id)
            if canonical is None or canonical.source != "merged":
                return
            links = s.exec(
                select(WorkoutSourceLink).where(
                    WorkoutSourceLink.canonical_activity_id == canonical_id
                )
            ).all()
            source_rows: list[dict[str, Any]] = []
            confidence: float | None = None
            reason: str | None = None
            for link in links:
                row = s.get(Workout, link.source_activity_id)
                if row is not None:
                    source_rows.append(row.model_dump())
                if link.match_confidence is not None:
                    confidence = link.match_confidence
                reason = link.match_reason or reason
        if source_rows:
            self._apply_source_merge(source_rows, confidence, reason, canonical_id, dry_run=False)

    def _auto_merge_strength_day(
        self, day_str: str, force: bool, dry_run: bool, min_confidence: float
    ) -> list[dict[str, Any]]:
        """Match manual strength sessions to Garmin strength activities on one
        day and merge each pair. Existing canonicals are refreshed first
        (idempotency); then unlinked manual sessions get their best Garmin
        match. Returns one result per canonical touched or match skipped."""
        with self.session() as s:
            workouts = [r.model_dump() for r in s.exec(
                select(Workout).where(Workout.day == day_str)
            ).all()]
            links = s.exec(select(WorkoutSourceLink)).all()
        link_by_source = {l.source_activity_id: l.canonical_activity_id for l in links}
        reason_by_canonical = {l.canonical_activity_id: l.match_reason for l in links}
        conf_by_canonical = {
            l.canonical_activity_id: l.match_confidence for l in links
            if l.match_confidence is not None
        }

        results: list[dict[str, Any]] = []
        handled_sources: set[int] = set()

        # 1. Refresh existing canonicals so a re-run stays idempotent and picks
        #    up any newly-synced field on a linked source.
        for canonical in workouts:
            if canonical.get("source") != "merged" or canonical.get("duplicate_of") is not None:
                continue
            try:
                linked_ids = json.loads(canonical.get("meta_json") or "{}").get("linked", [])
            except ValueError:
                linked_ids = []
            source_rows = [w for w in workouts if w["activity_id"] in linked_ids]
            if not source_rows:
                continue
            cid = canonical["activity_id"]
            results.append(self._apply_source_merge(
                source_rows,
                conf_by_canonical.get(cid),
                reason_by_canonical.get(cid),
                cid,
                dry_run,
            ))
            handled_sources.update(linked_ids)

        # 2. New matches for still-unlinked manual strength sessions.
        manual_candidates = [
            w for w in workouts
            if normalize_source(w) == "manual"
            and is_strength_like(w.get("type"))
            and w.get("duplicate_of") is None
            and w["activity_id"] not in handled_sources
            and w["activity_id"] not in link_by_source
        ]
        garmin_candidates = [
            w for w in workouts
            if w.get("source") in ("garmin", "garmin_merged")
            and is_strength_like(w.get("type"))
            and w.get("duplicate_of") is None
            and w["activity_id"] not in handled_sources
            and w["activity_id"] not in link_by_source
        ]
        used_garmin: set[int] = set()
        for manual in sorted(manual_candidates, key=lambda w: w["activity_id"]):
            pool = [g for g in garmin_candidates if g["activity_id"] not in used_garmin]
            match = best_strength_match(manual, pool)
            if match is None:
                continue
            garmin, confidence, reason = match
            if confidence < min_confidence and not force:
                results.append({
                    "merged": False, "skipped": True,
                    "reason": f"match confidence {confidence} below {min_confidence}",
                    "manual_activity_id": manual["activity_id"],
                    "garmin_activity_id": garmin["activity_id"],
                    "match_confidence": confidence,
                })
                continue
            used_garmin.add(garmin["activity_id"])
            results.append(
                self._apply_source_merge([manual, garmin], confidence, reason, None, dry_run)
            )
        return results

    def merge_workout_sources(
        self,
        day: str | date | None = None,
        activity_id: int | None = None,
        source_activity_ids: list[int] | None = None,
        force: bool = False,
        dry_run: bool = False,
        min_confidence: float = 0.5,
    ) -> dict[str, Any]:
        """Link a manual strength log and a Garmin activity of the same workout
        into one field-level canonical (manual exercise details + Garmin
        physiology/load), preserving both source rows.

        Pass ``source_activity_ids`` to link specific rows manually (overriding
        a bad auto-match); otherwise pass ``day`` (or ``activity_id``, whose day
        is used) to auto-match strength sessions on that day. ``force`` accepts
        matches below ``min_confidence``; ``dry_run`` previews without writing.
        """
        if source_activity_ids:
            with self.session() as s:
                rows = [s.get(Workout, aid) for aid in source_activity_ids]
                existing = None
                for aid in source_activity_ids:
                    link = s.exec(
                        select(WorkoutSourceLink).where(
                            WorkoutSourceLink.source_activity_id == aid
                        )
                    ).first()
                    if link is not None:
                        existing = link.canonical_activity_id
                        break
            source_rows = [r.model_dump() for r in rows if r is not None]
            if len(source_rows) < 2:
                return {"merged": False, "error": "need at least two existing workouts to link"}
            res = self._apply_source_merge(source_rows, 1.0, "manual link", existing, dry_run)
            return {"dry_run": dry_run, "merged": res.get("merged", False), "merges": [res]}

        if activity_id is not None and day is None:
            with self.session() as s:
                row = s.get(Workout, activity_id)
            if row is None:
                return {"merged": False, "error": f"no workout with activity_id {activity_id}"}
            day = row.day
        if day is None:
            return {"merged": False, "error": "pass day, activity_id, or source_activity_ids"}

        day_str = _as_day(day)
        merges = self._auto_merge_strength_day(day_str, force, dry_run, min_confidence)
        return {
            "day": day_str,
            "dry_run": dry_run,
            "merged": any(m.get("merged") for m in merges),
            "merges": merges,
        }

    def _merge_strength_window(
        self,
        days: int,
        force: bool = False,
        dry_run: bool = False,
        min_confidence: float = 0.5,
    ) -> list[dict[str, Any]]:
        """Run field-level strength merging across every day in the window;
        returns only the merges that actually happened."""
        with self.session() as s:
            day_values = s.exec(
                select(Workout.day).where(Workout.day >= self._cutoff(days)).distinct()
            ).all()
        out: list[dict[str, Any]] = []
        for day in sorted(set(day_values)):
            out.extend(
                m for m in self._auto_merge_strength_day(day, force, dry_run, min_confidence)
                if m.get("merged")
            )
        return out

    def get_merged_workout(self, activity_id: int) -> dict[str, Any] | None:
        """Full picture of a (possibly merged) workout: canonical summary,
        field provenance, each linked source record (with what it contributed),
        the strength exercises, and the Garmin-derived physiology fields."""
        with self.session() as s:
            canonical = s.get(Workout, activity_id)
            if canonical is None:
                return None
            links = s.exec(
                select(WorkoutSourceLink)
                .where(WorkoutSourceLink.canonical_activity_id == activity_id)
                .order_by(WorkoutSourceLink.id)
            ).all()
            linked_sources = []
            for link in links:
                src_row = s.get(Workout, link.source_activity_id)
                try:
                    imported = json.loads(link.fields_imported) if link.fields_imported else []
                except ValueError:
                    imported = []
                linked_sources.append({
                    "source": link.source,
                    "activity_id": link.source_activity_id,
                    "match_confidence": link.match_confidence,
                    "match_reason": link.match_reason,
                    "fields_imported": imported,
                    "workout": src_row.model_dump() if src_row is not None else None,
                })
            session_ids = [
                ss.id for ss in s.exec(
                    select(StrengthSession).where(StrengthSession.activity_id == activity_id)
                ).all()
            ]
        canonical_dump = canonical.model_dump()
        field_sources: dict[str, Any] = {}
        if canonical_dump.get("field_sources"):
            try:
                field_sources = json.loads(canonical_dump["field_sources"])
            except ValueError:
                pass
        return {
            "canonical": canonical_dump,
            "is_merged": canonical_dump.get("source") == "merged",
            "field_sources": field_sources,
            "linked_sources": linked_sources,
            "strength_sessions": [self.get_strength_session(sid) for sid in session_ids],
            "physiology": {
                k: canonical_dump.get(k)
                for k in ("avg_hr", "max_hr", "calories", "duration_s",
                          "training_load", "load_source", "start_time")
            },
        }

    def unmerge_workout_sources(self, canonical_activity_id: int) -> dict[str, Any]:
        """Reverse a field-level merge: restore each source row (clear its
        ``duplicate_of``), reattach the strength session to its manual source,
        drop the links, and delete the canonical. For overriding a bad match —
        no data is lost since source rows were never deleted."""
        with self.session() as s:
            canonical = s.get(Workout, canonical_activity_id)
            if canonical is None or canonical.source != "merged":
                return {
                    "unmerged": False,
                    "error": f"{canonical_activity_id} is not a merged canonical workout",
                }
            links = s.exec(
                select(WorkoutSourceLink).where(
                    WorkoutSourceLink.canonical_activity_id == canonical_activity_id
                )
            ).all()
            source_ids = [l.source_activity_id for l in links]
            manual_link = next((l for l in links if l.source == "manual"), None)
            restore_to = (
                manual_link.source_activity_id if manual_link is not None
                else (source_ids[0] if source_ids else None)
            )
            for sid in source_ids:
                row = s.get(Workout, sid)
                if row is not None and row.duplicate_of == canonical_activity_id:
                    row.duplicate_of = None
                    s.add(row)
            if restore_to is not None:
                for sess in s.exec(
                    select(StrengthSession).where(
                        StrengthSession.activity_id == canonical_activity_id
                    )
                ).all():
                    sess.activity_id = restore_to
                    s.add(sess)
            for link in links:
                s.delete(link)
            s.delete(canonical)
            s.commit()
        return {
            "unmerged": True,
            "canonical_activity_id": canonical_activity_id,
            "restored_sources": source_ids,
            "strength_reattached_to": restore_to,
        }

    def backfill_workout_source_merges(
        self, days: int = 3650, min_confidence: float = 0.6
    ) -> dict[str, Any]:
        """Merge historical manual strength sessions with Garmin strength
        activities where confidence is high. Safe to re-run (idempotent) and
        never deletes source rows. Opt-in — not run automatically at startup."""
        merged = self._merge_strength_window(days, min_confidence=min_confidence)
        return {"days": days, "min_confidence": min_confidence, "merged_sessions": merged}

    @staticmethod
    def _merged_label(w: dict[str, Any], field_sources: dict[str, str]) -> str:
        """A human line for a merged workout, e.g. 'Merged strength training:
        manual exercise log + Garmin HR/calories/duration/load'."""
        detail_src = field_sources.get("exercise_details")
        human = {"avg_hr": "HR", "calories": "calories",
                 "duration_s": "duration", "training_load": "load"}
        grouped: dict[str, list[str]] = {}
        for field, label in human.items():
            src = field_sources.get(field)
            if src:
                grouped.setdefault(src, []).append(label)
        kind = (w.get("type") or "workout").replace("_", " ")
        parts: list[str] = []
        if detail_src:
            parts.append(f"{detail_src} exercise log")
        for src, labels in grouped.items():
            parts.append(f"{src.capitalize()} {'/'.join(labels)}")
        return f"Merged {kind}: " + " + ".join(parts) if parts else f"Merged {kind}"

    def merged_workout_summaries(self, days: int = 28) -> list[dict[str, Any]]:
        """One row per field-level canonical in the window (newest first) with
        its provenance and a human label — for reports/summaries."""
        with self.session() as s:
            rows = s.exec(
                select(Workout)
                .where(Workout.day >= self._cutoff(days))
                .where(Workout.source == "merged")
                .where(Workout.duplicate_of == None)  # noqa: E711
                .order_by(Workout.day.desc(), Workout.activity_id.desc())
            ).all()
        out = []
        for r in rows:
            d = r.model_dump()
            field_sources: dict[str, str] = {}
            if d.get("field_sources"):
                try:
                    field_sources = json.loads(d["field_sources"])
                except ValueError:
                    pass
            out.append({
                "activity_id": d["activity_id"],
                "day": d["day"],
                "name": d.get("name"),
                "label": self._merged_label(d, field_sources),
                "field_sources": field_sources,
                "duration_s": d.get("duration_s"),
                "avg_hr": d.get("avg_hr"),
                "max_hr": d.get("max_hr"),
                "calories": d.get("calories"),
                "training_load": d.get("training_load"),
                "load_source": d.get("load_source"),
            })
        return out
