"""Wahoo workouts: one save path shared by the webhook and the REST pull.

What Wahoo's Cloud API gives us (https://cloud-api.wahooligan.com, read 2026-10-16):

- A *workout* (when it was scheduled/started, its type) and, once completed, its
  *workout_summary* (totals: distance, durations, avg HR/power/cadence/speed,
  calories, ascent) plus a FIT file URL on Wahoo's CDN.
- No stream endpoint, no maxima, no device. Everything per-second, every maximum
  and the device model come ONLY from the FIT file.

So the FIT file is downloaded first, and the order of writes is deliberate:

1. per-second samples (series webhooks),
2. the event record,
3. its detail, which fires ``workout.created``.

MyBya stitches streams onto a workout when ``workout.created`` arrives. Writing the
samples before the workout means they are already queryable by then, instead of
relying on MyBya's delayed re-stitch (as Garmin must, whose series land 15-25 s
after its summary).
"""

import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import fitdecode
import httpx

from app.config import settings
from app.constants.entry_source import get_unified_wahoo_entry_source
from app.constants.workout_types import get_unified_wahoo_workout_type
from app.database import DbSession
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordMetrics,
    TimeSeriesSampleCreate,
)
from app.schemas.providers.wahoo import WahooWorkoutJSON, WahooWorkoutListJSON, WahooWorkoutSummaryJSON
from app.services.event_record_service import event_record_service
from app.services.fit_parser import parse_fit_file
from app.services.providers.templates.base_workouts import BaseWorkoutsTemplate
from app.services.timeseries_service import timeseries_service
from app.utils.dates import offset_to_iso
from app.utils.sentry_helpers import log_and_capture_error

# Wahoo's own name for itself, used when a workout has no FIT file to name a device.
_SOURCE_NAME = "Wahoo"


@dataclass
class WahooFitData:
    """What the FIT file adds to a Wahoo summary."""

    device_model: str | None = None
    software_version: str | None = None
    start_time: datetime | None = None
    elapsed_seconds: float | None = None
    max_heart_rate: int | None = None
    max_power: int | None = None
    max_speed: Decimal | None = None
    samples: list[TimeSeriesSampleCreate] = field(default_factory=list)


def read_fit_device_and_session(data: bytes) -> WahooFitData:
    """Device model and session maxima from a Wahoo FIT file.

    Measured on real ELEMNT files (python-fitparse's test fixtures): Wahoo writes a
    ``device_info`` message with ``device_index="creator"`` and
    ``product_name="ELEMNT BOLT"`` (or "ELEMNT"), alongside ``file_id.product`` 31 /
    28. Other ``device_info`` rows are the sensors (GPS, power meter, speed sensor),
    so only the creator counts. ``descriptor`` ("ELEMNT BOLT 143C") carries a
    serial-number suffix and is not a model name. Wahoo product ids are not in the
    FIT profile, so with no ``product_name`` the model is left unknown rather than
    guessed.
    """
    result = WahooFitData()
    with fitdecode.FitReader(io.BytesIO(data)) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "device_info" and result.device_model is None:
                if _value(frame, "device_index") == "creator":
                    name = _value(frame, "product_name")
                    if isinstance(name, str) and name.strip():
                        result.device_model = name.strip()
                    version = _value(frame, "software_version")
                    if version is not None:
                        result.software_version = str(version)
            elif frame.name == "session" and result.start_time is None:
                start = _value(frame, "start_time")
                if isinstance(start, datetime):
                    result.start_time = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
                result.elapsed_seconds = _as_float(_value(frame, "total_elapsed_time"))
                result.max_heart_rate = _positive_int(_value(frame, "max_heart_rate"))
                result.max_power = _positive_int(_value(frame, "max_power"))
                max_speed = _value(frame, "enhanced_max_speed")
                if max_speed is None:
                    max_speed = _value(frame, "max_speed")
                result.max_speed = _positive_decimal(max_speed)
    return result


class WahooWorkouts(BaseWorkoutsTemplate):
    """Wahoo implementation of the workouts template."""

    @property
    def events_per_page(self) -> int:
        return settings.wahoo_events_per_page

    # ------------------------------------------------------------------
    # REST pull
    # ------------------------------------------------------------------

    def get_workouts(
        self,
        db: DbSession,
        user_id: UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> list[WahooWorkoutJSON]:
        """Workouts that started in [start_date, end_date].

        GET /v1/workouts has no date filter; it pages newest-first by ``starts``. So
        page until a page reaches past ``start_date`` or the list runs out.
        """
        start_date = _aware(start_date)
        end_date = _aware(end_date)
        found: list[WahooWorkoutJSON] = []
        page = 1
        seen = 0

        while True:
            response = self._make_api_request(
                db, user_id, "/v1/workouts", params={"page": page, "per_page": self.events_per_page}
            )
            listing = WahooWorkoutListJSON.model_validate(response or {})
            if not listing.workouts:
                break

            reached_window_start = False
            for workout in listing.workouts:
                starts = _parse_iso(workout.starts)
                if starts is None:
                    continue
                if starts < start_date:
                    reached_window_start = True
                    continue
                if starts <= end_date:
                    found.append(workout)

            seen += len(listing.workouts)
            if reached_window_start or (listing.total is not None and seen >= listing.total):
                break
            page += 1

        return found

    def get_workouts_from_api(self, db: DbSession, user_id: UUID, **kwargs: Any) -> Any:
        params = {"page": kwargs.get("page", 1), "per_page": kwargs.get("per_page", self.events_per_page)}
        return self._make_api_request(db, user_id, "/v1/workouts", params=params)

    def get_workout_detail_from_api(self, db: DbSession, user_id: UUID, workout_id: str, **kwargs: Any) -> Any:
        return self._make_api_request(db, user_id, f"/v1/workouts/{workout_id}")

    def load_data(self, db: DbSession, user_id: UUID, **kwargs: Any) -> int:
        """Pull completed workouts in a window and save the new ones. Returns how many were saved."""
        start_dt = _kwarg_datetime(kwargs.get("start") or kwargs.get("start_date"), timedelta(days=30))
        end_dt = _kwarg_datetime(kwargs.get("end") or kwargs.get("end_date"), timedelta(0))

        saved = 0
        for workout in self.get_workouts(db, user_id, start_dt, end_dt):
            summary = self._summary_for(db, user_id, workout)
            if summary is None:
                continue  # scheduled or in progress: nothing to save yet
            result = self.save_summary(db, user_id, summary)
            if result["status"] == "saved":
                saved += 1
        return saved

    def _summary_for(self, db: DbSession, user_id: UUID, workout: WahooWorkoutJSON) -> WahooWorkoutSummaryJSON | None:
        """The workout's summary, with the workout attached as the webhook delivers it.

        The documented list sample shows ``workout_summary`` inline (null when not yet
        completed). If a listing ever omits the key, fetch the summary instead of
        reading the omission as "not completed".
        """
        if "workout_summary" in workout.model_fields_set:
            summary = workout.workout_summary
        else:
            raw = self._make_api_request(db, user_id, f"/v1/workouts/{workout.id}/workout_summary")
            summary = WahooWorkoutSummaryJSON.model_validate(raw) if raw else None
        if summary is None:
            return None
        return summary.model_copy(update={"workout": workout.model_copy(update={"workout_summary": None})})

    # ------------------------------------------------------------------
    # Shared save path (webhook + pull)
    # ------------------------------------------------------------------

    def save_summary(self, db: DbSession, user_id: UUID, summary: WahooWorkoutSummaryJSON) -> dict[str, Any]:
        """Save one completed workout. Idempotent on the Wahoo workout id.

        Wahoo documents duplicate webhooks "when files are updated or deleted", and
        the pull overlaps the webhook. The workout id is the dedupe key; a later
        edit to an already-saved workout is not applied.
        """
        workout = summary.workout
        if workout is None:
            return {"status": "ignored", "reason": "summary_without_workout", "summary_id": summary.id}

        external_id = str(workout.id)
        if self.workout_repo.get_by_external_id(db, user_id, external_id, provider=self.provider_name):
            return {"status": "ignored", "reason": "duplicate_workout", "workout_id": workout.id}

        fit = self._fit_data(user_id, summary)
        try:
            record, detail = self._normalize_workout(summary, user_id, fit)
        except ValueError as exc:
            return {"status": "ignored", "reason": str(exc), "workout_id": workout.id}

        samples = self._save_samples(db, fit, record)
        created_record = event_record_service.create(db, record)
        event_record_service.create_detail(db, detail.model_copy(update={"record_id": created_record.id}))

        return {
            "status": "saved",
            "workout_id": workout.id,
            "record_id": str(created_record.id),
            "device_model": record.device_model,
            "samples_saved": samples,
        }

    def _normalize_workout(
        self,
        raw_workout: WahooWorkoutSummaryJSON,
        user_id: UUID,
        fit: WahooFitData | None = None,
    ) -> tuple[EventRecordCreate, EventRecordDetailCreate]:
        """Summary (+ FIT) → event record and detail.

        Start time: the FIT session's ``start_time`` when there is one, because a
        workout's ``starts`` is when it was *scheduled* for a planned workout ("The
        parent workout object contains information about when a workout is
        scheduled"), not when it was ridden. Duration: Wahoo's own
        ``duration_total_accum`` (elapsed), else the FIT session's.
        """
        summary = raw_workout
        workout = summary.workout
        if workout is None:
            raise ValueError("summary_without_workout")
        fit = fit or WahooFitData()

        start = fit.start_time or _parse_iso(workout.starts)
        if start is None:
            raise ValueError("no_start_time")

        duration = _as_float(summary.duration_total_accum)
        if not duration or duration <= 0:
            duration = fit.elapsed_seconds
        if not duration or duration <= 0:
            duration = float(workout.minutes * 60) if workout.minutes else 0.0
        end = start + timedelta(seconds=duration)

        record_id = uuid4()
        record = EventRecordCreate(
            category="workout",
            type=get_unified_wahoo_workout_type(workout.workout_type_id).value,
            source_name=fit.device_model or _SOURCE_NAME,
            device_model=fit.device_model,
            software_version=fit.software_version,
            duration_seconds=int(round(duration)),
            start_datetime=start,
            end_datetime=end,
            zone_offset=_zone_offset(summary.time_zone, start),
            id=record_id,
            external_id=str(workout.id),
            source=self.provider_name,
            provider=self.provider_name,
            user_id=user_id,
        )
        detail = EventRecordDetailCreate(record_id=record_id, **self._build_metrics(summary, fit))
        return record, detail

    @staticmethod
    def _build_metrics(summary: WahooWorkoutSummaryJSON, fit: WahooFitData) -> EventRecordMetrics:
        """Detail metrics. A zero average means "no sensor" and is dropped, not stored."""
        metrics: EventRecordMetrics = {}

        if (hr_avg := _positive_decimal(summary.heart_rate_avg)) is not None:
            metrics["heart_rate_avg"] = hr_avg
        if fit.max_heart_rate is not None:
            metrics["heart_rate_max"] = fit.max_heart_rate
        if (distance := _non_negative_decimal(summary.distance_accum)) is not None:
            metrics["distance"] = distance
        if (speed := _positive_decimal(summary.speed_avg)) is not None:
            metrics["average_speed"] = speed
        if fit.max_speed is not None:
            metrics["max_speed"] = fit.max_speed
        if (power := _positive_decimal(summary.power_avg)) is not None:
            metrics["average_watts"] = power
        if fit.max_power is not None:
            metrics["max_watts"] = Decimal(fit.max_power)
        if (cadence := _positive_decimal(summary.cadence_avg)) is not None:
            metrics["average_cadence"] = cadence
        if (ascent := _non_negative_decimal(summary.ascent_accum)) is not None:
            metrics["total_elevation_gain"] = ascent
        if (calories := _non_negative_decimal(summary.calories_accum)) is not None:
            metrics["energy_burned"] = calories
        if (active := _as_float(summary.duration_active_accum)) is not None and active > 0:
            metrics["moving_time_seconds"] = int(round(active))

        entry_source = get_unified_wahoo_entry_source(summary.manual)
        if entry_source is not None:
            metrics["entry_source"] = entry_source

        name = summary.name or (summary.workout.name if summary.workout else None)
        if name:
            metrics["label"] = name
        return metrics

    # ------------------------------------------------------------------
    # FIT file
    # ------------------------------------------------------------------

    def _fit_data(self, user_id: UUID, summary: WahooWorkoutSummaryJSON) -> WahooFitData | None:
        """Download and read the summary's FIT file. Failure-isolated: None on any error.

        A manual entry has no file, and a failed download must not lose the workout;
        it is then saved from the summary alone (no device model, no streams).
        """
        url = summary.file.url if summary.file else None
        if not url:
            return None
        try:
            data = self._download_fit(url)
            fit = read_fit_device_and_session(data)
            if settings.ingest_workout_samples:
                fit.samples = parse_fit_file(data, user_id, source=self.provider_name).samples
            return fit
        except Exception as exc:
            log_and_capture_error(
                exc,
                self.logger,
                "Failed to read Wahoo FIT file; saving the summary without it",
                extra={"provider": self.provider_name, "summary_id": summary.id, "user_id": str(user_id)},
            )
            return None

    @staticmethod
    def _download_fit(url: str) -> bytes:
        """GET the FIT file from Wahoo's CDN.

        No Authorization header: the docs say to "paste it into your browser or make a
        GET call", the URL is not on Wahoo's API host, and sending a bearer token to a
        CDN would leak it. Not an API call, so it neither counts against the rate limit
        nor revokes any token.
        """
        response = httpx.get(url, timeout=settings.provider_request_timeout_seconds, follow_redirects=True)
        response.raise_for_status()
        return response.content

    def _save_samples(self, db: DbSession, fit: WahooFitData | None, record: EventRecordCreate) -> int:
        """Persist the FIT samples under the workout's own data source, before the workout.

        Failure-isolated like Strava's streams: a savepoint rolls back only the samples.
        """
        if not fit or not fit.samples:
            return 0
        samples = [
            sample.model_copy(
                update={
                    "provider": self.provider_name,
                    "device_model": record.device_model,
                    "software_version": record.software_version,
                    "zone_offset": record.zone_offset,
                }
            )
            for sample in fit.samples
        ]
        nested = db.begin_nested()
        try:
            timeseries_service.bulk_create_samples(db, samples)
            nested.commit()
            return len(samples)
        except Exception as exc:
            nested.rollback()
            log_and_capture_error(
                exc,
                self.logger,
                "Wahoo workout sample ingestion failed; continuing",
                extra={"provider": self.provider_name, "external_id": record.external_id, "samples": len(samples)},
            )
            return 0


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _value(frame: fitdecode.FitDataMessage, name: str) -> Any:
    try:
        return frame.get_value(name)
    except KeyError:
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(round(number)) if number is not None and number > 0 else None


def _positive_decimal(value: Any) -> Decimal | None:
    number = _as_float(value)
    return Decimal(str(value)) if number is not None and number > 0 else None


def _non_negative_decimal(value: Any) -> Decimal | None:
    number = _as_float(value)
    return Decimal(str(value)) if number is not None and number >= 0 else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _kwarg_datetime(value: Any, default_age: timedelta) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str) and (parsed := _parse_iso(value)):
        return parsed
    return datetime.now(timezone.utc) - default_age


def _zone_offset(time_zone: str | None, at: datetime) -> str | None:
    """'+HH:MM' for an IANA zone at an instant (Wahoo gives the zone, not the offset)."""
    if not time_zone:
        return None
    try:
        offset = ZoneInfo(time_zone).utcoffset(at)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return offset_to_iso(int(offset.total_seconds())) if offset is not None else None
