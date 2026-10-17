"""WahooWorkouts: normalization, the FIT file, the REST pull and the shared save path."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import DataPointSeries, DataSource, EventRecord
from app.schemas.enums import EntrySource, WorkoutType
from app.schemas.providers.wahoo import WahooWorkoutJSON, WahooWorkoutSummaryJSON
from app.services.providers.factory import ProviderFactory
from app.services.providers.wahoo.workouts import WahooFitData, WahooWorkouts, read_fit_device_and_session
from tests.factories import UserConnectionFactory
from tests.providers.wahoo.conftest import FIT_URL, WAHOO_USER_ID, summary_payload

_WORKOUTS = "app.services.providers.wahoo.workouts"


@pytest.fixture
def workouts() -> WahooWorkouts:
    strategy_workouts = ProviderFactory().get_provider("wahoo").workouts
    assert isinstance(strategy_workouts, WahooWorkouts)
    return strategy_workouts


def _summary(**overrides: Any) -> WahooWorkoutSummaryJSON:
    return WahooWorkoutSummaryJSON.model_validate(summary_payload(**overrides))


class TestReadFit:
    """Pinned against a real ELEMNT BOLT file, not an assumption about one."""

    def test_device_model_is_the_creator_product_name(self, elemnt_bolt_fit: bytes) -> None:
        fit = read_fit_device_and_session(elemnt_bolt_fit)

        # Not "ELEMNT BOLT 143C" (descriptor, has a serial suffix) and not a sensor
        # such as "GPS" or the speed sensor, which are also device_info rows.
        assert fit.device_model == "ELEMNT BOLT"
        assert fit.software_version == "196.28"

    def test_session_start_and_maxima(self, elemnt_bolt_fit: bytes) -> None:
        fit = read_fit_device_and_session(elemnt_bolt_fit)

        assert fit.start_time == datetime(2017, 8, 21, 8, 18, tzinfo=timezone.utc)
        assert fit.elapsed_seconds == 140.0
        assert fit.max_power == 665
        assert fit.max_speed == Decimal("10.596")
        assert fit.max_heart_rate is None  # this ride had no HR strap


class TestNormalize:
    def test_documented_summary(self, workouts: WahooWorkouts) -> None:
        user_id = uuid4()
        record, detail = workouts._normalize_workout(_summary(workout_type_id=15), user_id)

        assert record.source == "wahoo"
        assert record.provider == "wahoo"
        assert record.external_id == "56519"  # the workout id, stable across summary re-sends
        assert record.type == WorkoutType.CYCLING.value
        assert record.start_datetime == datetime(2015, 8, 12, 9, 0, tzinfo=timezone.utc)
        assert record.duration_seconds == 275
        assert (record.end_datetime - record.start_datetime).total_seconds() == 275
        assert record.zone_offset == "-06:00"  # America/Denver in August (MDT)
        assert record.device_model is None  # no FIT file read → no device claimed
        assert record.source_name == "Wahoo"

        assert detail.heart_rate_avg == Decimal("100.00")
        assert detail.distance == Decimal("24909.71")
        assert detail.average_speed == Decimal("10.75")
        assert detail.average_watts == Decimal("94.59")
        assert detail.average_cadence == Decimal("50.00")
        assert detail.total_elevation_gain == Decimal("450.00")
        assert detail.energy_burned == Decimal("1500.00")
        assert detail.moving_time_seconds == 179
        assert detail.entry_source == EntrySource.AUTOMATIC
        assert detail.label == "Easy Ride"

    def test_fit_supplies_device_start_and_maxima(self, workouts: WahooWorkouts) -> None:
        fit = WahooFitData(
            device_model="ELEMNT BOLT",
            software_version="196.28",
            start_time=datetime(2015, 8, 12, 9, 7, 30, tzinfo=timezone.utc),
            max_heart_rate=171,
            max_power=665,
            max_speed=Decimal("10.596"),
        )
        record, detail = workouts._normalize_workout(_summary(), uuid4(), fit)

        # A workout's `starts` is its SCHEDULED time for a planned workout; the FIT
        # session is when it was actually ridden.
        assert record.start_datetime == datetime(2015, 8, 12, 9, 7, 30, tzinfo=timezone.utc)
        assert record.device_model == "ELEMNT BOLT"
        assert record.source_name == "ELEMNT BOLT"
        assert record.software_version == "196.28"
        assert detail.heart_rate_max == 171
        assert detail.max_watts == Decimal("665")
        assert detail.max_speed == Decimal("10.596")

    def test_zero_averages_mean_no_sensor(self, workouts: WahooWorkouts) -> None:
        _, detail = workouts._normalize_workout(
            _summary(heart_rate_avg="0.00", power_avg="0.00", cadence_avg="0.00", speed_avg="0.00"), uuid4()
        )

        assert detail.heart_rate_avg is None
        assert detail.average_watts is None
        assert detail.average_cadence is None
        assert detail.average_speed is None

    def test_manual_entry(self, workouts: WahooWorkouts) -> None:
        _, detail = workouts._normalize_workout(_summary(manual=True, file=None), uuid4())
        assert detail.entry_source == EntrySource.MANUAL

    def test_duration_falls_back_to_fit_then_minutes(self, workouts: WahooWorkouts) -> None:
        record, _ = workouts._normalize_workout(
            _summary(duration_total_accum=None), uuid4(), WahooFitData(elapsed_seconds=140.0)
        )
        assert record.duration_seconds == 140

        record, _ = workouts._normalize_workout(_summary(duration_total_accum=None), uuid4())
        assert record.duration_seconds == 12 * 60

    def test_unknown_time_zone_leaves_offset_unset(self, workouts: WahooWorkouts) -> None:
        record, _ = workouts._normalize_workout(_summary(time_zone="Not/AZone"), uuid4())
        assert record.zone_offset is None

    def test_no_start_time_is_rejected(self, workouts: WahooWorkouts) -> None:
        summary = _summary()
        assert summary.workout is not None
        summary.workout.starts = None
        with pytest.raises(ValueError, match="no_start_time"):
            workouts._normalize_workout(summary, uuid4())


class TestPull:
    @staticmethod
    def _workout(workout_id: int, starts: str, completed: bool = True) -> dict[str, Any]:
        summary = summary_payload(id=workout_id + 1000) if completed else None
        if summary:
            summary.pop("workout")
        return {"id": workout_id, "starts": starts, "workout_type_id": 1, "workout_summary": summary}

    def test_pages_newest_first_until_the_window_start(self, workouts: WahooWorkouts) -> None:
        pages = {
            1: {"workouts": [self._workout(3, "2026-10-15T07:00:00Z"), self._workout(2, "2026-10-10T07:00:00Z")]},
            2: {"workouts": [self._workout(1, "2026-09-01T07:00:00Z")]},
            3: {"workouts": [self._workout(0, "2026-08-01T07:00:00Z")]},
        }
        calls: list[int] = []

        def fake_request(_db: Any, _user: Any, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
            assert endpoint == "/v1/workouts"
            calls.append(params["page"])
            return pages[params["page"]]

        with patch.object(workouts, "_make_api_request", side_effect=fake_request):
            found = workouts.get_workouts(
                MagicMock(),
                uuid4(),
                datetime(2026, 9, 15, tzinfo=timezone.utc),
                datetime(2026, 10, 16, tzinfo=timezone.utc),
            )

        assert [w.id for w in found] == [3, 2]
        assert calls == [1, 2]  # page 2 reaches past the window start; page 3 is never fetched

    def test_summary_fetched_only_when_the_listing_omits_it(self, workouts: WahooWorkouts) -> None:
        inline_null = WahooWorkoutJSON.model_validate(
            {"id": 1, "starts": "2026-10-15T07:00:00Z", "workout_summary": None}
        )
        omitted = WahooWorkoutJSON.model_validate({"id": 2, "starts": "2026-10-15T07:00:00Z"})
        fetched = summary_payload()
        fetched.pop("workout")

        with patch.object(workouts, "_make_api_request", return_value=fetched) as request:
            assert workouts._summary_for(MagicMock(), uuid4(), inline_null) is None
            request.assert_not_called()

            summary = workouts._summary_for(MagicMock(), uuid4(), omitted)
            request.assert_called_once()
            assert request.call_args.args[2] == "/v1/workouts/2/workout_summary"

        assert summary is not None
        assert summary.workout is not None
        assert summary.workout.id == 2


class TestSave:
    """The shared save path, against the test database."""

    @pytest.fixture
    def user_id(self) -> Any:
        connection = UserConnectionFactory(provider="wahoo", provider_user_id=str(WAHOO_USER_ID))
        return connection.user_id

    def test_saves_device_streams_and_workout(
        self, db: Session, workouts: WahooWorkouts, user_id: Any, elemnt_bolt_fit: bytes
    ) -> None:
        with (
            patch(f"{_WORKOUTS}.settings.ingest_workout_samples", True),
            patch.object(WahooWorkouts, "_download_fit", return_value=elemnt_bolt_fit) as download,
        ):
            result = workouts.save_summary(db, user_id, _summary(workout_type_id=15))

        download.assert_called_once_with(FIT_URL)
        assert result["status"] == "saved"
        assert result["device_model"] == "ELEMNT BOLT"
        assert result["samples_saved"] > 0

        record = db.query(EventRecord).filter(EventRecord.external_id == "56519").one()
        source = db.get(DataSource, record.data_source_id)
        assert source is not None
        assert str(source.provider) == "wahoo"
        assert source.device_model == "ELEMNT BOLT"
        assert record.start_datetime == datetime(2017, 8, 21, 8, 18, tzinfo=timezone.utc)

        # Samples live under the SAME data source as the workout, so a timeseries
        # query for this device finds them.
        sample_sources = {
            row.data_source_id for row in db.query(DataPointSeries).filter(DataPointSeries.data_source_id == source.id)
        }
        assert sample_sources == {source.id}

    def test_duplicate_delivery_is_ignored_without_downloading(
        self, db: Session, workouts: WahooWorkouts, user_id: Any, elemnt_bolt_fit: bytes
    ) -> None:
        with patch.object(WahooWorkouts, "_download_fit", return_value=elemnt_bolt_fit) as download:
            assert workouts.save_summary(db, user_id, _summary())["status"] == "saved"
            again = workouts.save_summary(db, user_id, _summary())

        assert again == {"status": "ignored", "reason": "duplicate_workout", "workout_id": 56519}
        assert download.call_count == 1
        assert db.query(EventRecord).filter(EventRecord.external_id == "56519").count() == 1

    def test_failed_download_still_saves_the_summary(self, db: Session, workouts: WahooWorkouts, user_id: Any) -> None:
        with patch.object(WahooWorkouts, "_download_fit", side_effect=RuntimeError("CDN down")):
            result = workouts.save_summary(db, user_id, _summary())

        assert result["status"] == "saved"
        assert result["device_model"] is None
        assert result["samples_saved"] == 0

    def test_samples_written_before_the_workout(
        self, db: Session, workouts: WahooWorkouts, user_id: Any, elemnt_bolt_fit: bytes
    ) -> None:
        """workout.created must find the streams already stored (MyBya stitches on arrival)."""
        order: list[str] = []
        with (
            patch(f"{_WORKOUTS}.settings.ingest_workout_samples", True),
            patch.object(WahooWorkouts, "_download_fit", return_value=elemnt_bolt_fit),
            patch(
                f"{_WORKOUTS}.timeseries_service.bulk_create_samples", side_effect=lambda *_: order.append("samples")
            ),
            patch(f"{_WORKOUTS}.event_record_service.create_detail", side_effect=lambda *_: order.append("detail")),
        ):
            workouts.save_summary(db, user_id, _summary())

        assert order == ["samples", "detail"]

    def test_load_data_saves_completed_workouts_in_the_window(
        self, db: Session, workouts: WahooWorkouts, user_id: Any
    ) -> None:
        listing = {
            "workouts": [
                {
                    "id": 900,
                    "starts": "2026-10-15T07:00:00.000Z",
                    "workout_type_id": 1,
                    "workout_summary": {**summary_payload(id=9000, file=None), "workout": None},
                },
                {"id": 901, "starts": "2026-10-14T07:00:00.000Z", "workout_type_id": 1, "workout_summary": None},
            ],
            "total": 2,
        }
        with patch.object(workouts, "_make_api_request", return_value=listing):
            saved = workouts.load_data(
                db, user_id, start_date="2026-10-01T00:00:00+00:00", end_date="2026-10-16T00:00:00+00:00"
            )

        assert saved == 1
        record = db.query(EventRecord).filter(EventRecord.external_id == "900").one()
        assert record.type == WorkoutType.RUNNING.value
