from app.services.providers.base_strategy import BaseProviderStrategy, ProviderCapabilities, ProviderCoverage
from app.services.providers.wahoo.coverage import HEALTH_SCORES, SLEEP_FIELDS, TIMESERIES, WORKOUT_FIELDS
from app.services.providers.wahoo.oauth import WahooOAuth
from app.services.providers.wahoo.webhook_handler import WahooWebhookHandler
from app.services.providers.wahoo.workouts import WahooWorkouts


class WahooStrategy(BaseProviderStrategy):
    """Wahoo provider (MyBya fork; upstream Open Wearables 0.9.0 has none).

    Workouts only. Wahoo's Cloud API exposes no daily or sleep data, so there is no
    data_247, and its webhook subscription is configured on the Wahoo developer
    portal (there is no registration API), so there is no webhook_service.
    """

    def __init__(self):
        super().__init__()

        self.oauth = WahooOAuth(
            user_repo=self.user_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
        )
        self.workouts = WahooWorkouts(
            workout_repo=self.workout_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
            oauth=self.oauth,
        )
        self.data_247 = None
        self.webhooks = WahooWebhookHandler(workouts=self.workouts)
        self.webhook_service = None

    @property
    def name(self) -> str:
        return "wahoo"

    @property
    def api_base_url(self) -> str:
        # OAuth (/oauth/...) and the API (/v1/...) share this host.
        return "https://api.wahooligan.com"

    @property
    def coverage(self) -> ProviderCoverage:
        return ProviderCoverage(
            timeseries=TIMESERIES,
            workout_fields=WORKOUT_FIELDS,
            sleep_fields=SLEEP_FIELDS,
            health_scores=HEALTH_SCORES,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        # The webhook carries the complete summary (the FIT file is fetched from its
        # URL, not from the REST API), so it is a stream, not a ping. rest_pull serves
        # the 90-day backfill on connect and the periodic pull, which is also what
        # notices a revocation: Wahoo sends no deauthorization webhook.
        return ProviderCapabilities(rest_pull=True, webhook_stream=True)
