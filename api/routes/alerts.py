"""
api/routes/alerts.py — Currently alert-worthy stocks as JSON.

Reuses alerts.alert_manager's existing rule set (AlertManager.build_alerts)
against the latest cached market overview + watchlist metrics — this
endpoint only returns the list, it never prints to a console.
"""
import logging
from typing import List

from fastapi import APIRouter

from alerts.alert_manager import AlertManager
from api.routes.market import get_cached_overview
from api.routes.watchlist import get_cached_watchlist
from models.schemas import AlertResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/alerts", tags=["alerts"])

_manager = AlertManager()


@router.get("", response_model=List[AlertResponse])
def get_alerts() -> List[AlertResponse]:
    all_metrics = {}

    try:
        overview = get_cached_overview()
        all_metrics.update(overview.all_metrics)
    except RuntimeError:
        pass
    except Exception as exc:  # noqa: BLE001 - alerts must degrade, not 500, on a scan hiccup
        logger.error("Alerts: market overview failed: %s", exc)

    try:
        _symbols, wl_metrics = get_cached_watchlist()
        all_metrics.update(wl_metrics)
    except RuntimeError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.error("Alerts: watchlist failed: %s", exc)

    alerts = _manager.build_alerts(all_metrics.values())
    return [
        AlertResponse(
            symbol=a.symbol,
            signal=a.signal,
            pct_change=a.pct_change,
            price=a.price,
            attention_score=a.attention_score,
            message=a.message(),
        )
        for a in alerts
    ]
