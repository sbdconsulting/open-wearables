"""Wahoo Cloud API workout and workout-summary payloads.

Field names are copied from https://cloud-api.wahooligan.com ("Workouts", "Workout
Summaries", read 2026-10-16). Wahoo returns decimals as JSON strings ("24909.71");
pydantic's lax Decimal parsing accepts them as-is.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel


class WahooFile(BaseModel):
    """The summary's FIT file. The URL is on Wahoo's CDN and needs no bearer token."""

    url: str | None = None


class WahooWorkoutSummaryJSON(BaseModel):
    """GET /v1/workouts/:id/workout_summary, and the `workout_summary` webhook object.

    Every metric is optional: Wahoo leaves a summary empty until the workout is
    completed, and a manual entry has no FIT file.
    """

    id: int
    name: str | None = None
    ascent_accum: Decimal | None = None  # metres
    cadence_avg: Decimal | None = None  # rpm
    calories_accum: Decimal | None = None  # kcal
    distance_accum: Decimal | None = None  # metres
    duration_active_accum: Decimal | None = None  # seconds
    duration_paused_accum: Decimal | None = None  # seconds
    duration_total_accum: Decimal | None = None  # seconds
    heart_rate_avg: Decimal | None = None  # bpm
    power_bike_np_last: Decimal | None = None  # watts
    power_bike_tss_last: Decimal | None = None  # unitless
    power_avg: Decimal | None = None  # watts
    speed_avg: Decimal | None = None  # m/s
    work_accum: Decimal | None = None  # joules
    time_zone: str | None = None  # IANA, e.g. "America/Denver"
    manual: bool | None = None
    edited: bool | None = None
    # < 1000 is a Wahoo app; > 1000 a third-party app (Wahoo no longer shares those).
    fitness_app_id: int | None = None
    file: WahooFile | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # Present only in the webhook, where the summary carries its parent workout.
    workout: WahooWorkoutJSON | None = None


class WahooWorkoutJSON(BaseModel):
    """GET /v1/workouts/:id and each entry of GET /v1/workouts."""

    id: int
    starts: str | None = None  # ISO 8601 UTC, e.g. "2015-08-12T09:00:00.000Z"
    minutes: int | None = None
    name: str | None = None
    plan_id: int | None = None
    plan_ids: list[int] | None = None
    route_id: int | None = None
    workout_token: str | None = None
    workout_type_id: int | None = None
    day_code: int | None = None
    workout_summary: WahooWorkoutSummaryJSON | None = None
    created_at: str | None = None
    updated_at: str | None = None


class WahooWorkoutListJSON(BaseModel):
    """GET /v1/workouts. Always sorted by `starts`, newest first."""

    workouts: list[WahooWorkoutJSON] = []
    total: int | None = None
    page: int | None = None
    per_page: int | None = None
    order: str | None = None
    sort: str | None = None


WahooWorkoutSummaryJSON.model_rebuild()
