from app.schemas.enums import SeriesType
from app.schemas.enums.health_score_category import HealthScoreCategory
from app.services.fit_parser import _RECORD_FIELD_MAP

# Wahoo's API has no stream endpoint: per-second data exists only in the summary's
# FIT file, which workouts.py downloads and hands to the shared FIT parser. So the
# series Wahoo can emit are exactly the FIT record fields that parser maps; taken
# from its table so the two cannot drift. Gated by settings.ingest_workout_samples.
TIMESERIES: frozenset[SeriesType] = frozenset(mapping.series_type for mapping in _RECORD_FIELD_MAP)

# EventRecordDetail fields set by workouts.py: the summary's own totals, plus the
# maxima that only the FIT session message carries.
WORKOUT_FIELDS: frozenset[str] = frozenset(
    {
        "heart_rate_avg",
        "heart_rate_max",
        "distance",
        "average_speed",
        "max_speed",
        "average_watts",
        "max_watts",
        "average_cadence",
        "total_elevation_gain",
        "energy_burned",
        "moving_time_seconds",
        "entry_source",
        "label",
    }
)

# Wahoo exposes workouts only: no sleep, no daily summaries, no scores.
SLEEP_FIELDS: frozenset[str] = frozenset()

HEALTH_SCORES: frozenset[HealthScoreCategory] = frozenset()
