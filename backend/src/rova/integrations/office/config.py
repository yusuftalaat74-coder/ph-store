"""The four PH Office variables (SPEC 5.9.2), read from rova.config.Settings:

    ROVA_OFFICE_ENABLED           false by default. true = PH Office manages
                                  vendor fulfilment and finance: events flow both
                                  ways and the vendor routes of SPEC 1.3 are locked
    ROVA_OFFICE_URL               base URL of PH Office, e.g. http://phoffice-api:8100
    ROVA_OFFICE_OUTBOUND_SECRET   Store -> Office signing secret (= PHOFFICE_INBOUND_SECRET)
    ROVA_OFFICE_INBOUND_SECRET    Office -> Store signing secret (= PHOFFICE_OUTBOUND_SECRET)

plus ROVA_OFFICE_EMIT (false by default): emit events while NOT enabled — the
SHADOW phase of the rollout (SPEC 1.4 phase 1), where Office listens while the
vendor still works in PH Store. Emitting never changes Store behaviour."""
from rova.config import get_settings


def enabled() -> bool:
    return bool(get_settings().office_enabled)


def emitting() -> bool:
    s = get_settings()
    return bool(s.office_enabled or s.office_emit)


def url() -> str:
    return (get_settings().office_url or "").rstrip("/")


def outbound_secret() -> str:
    return get_settings().office_outbound_secret


def inbound_secret() -> str:
    return get_settings().office_inbound_secret
