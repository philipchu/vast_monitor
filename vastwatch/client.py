from __future__ import annotations

import json
import os
import random
import time
from typing import Any, Dict, List, Optional

import requests


DEFAULT_BASE_URL = os.environ.get("VAST_BASE_URL", "https://cloud.vast.ai/api/v0")
SEARCH_OFFERS_ENDPOINT = f"{DEFAULT_BASE_URL.rstrip('/')}/bundles/"


class VastAPIError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def _headers() -> Dict[str, str]:
    api_key = os.environ.get("VAST_API_KEY")
    if not api_key:
        raise VastAPIError("VAST_API_KEY is not set in environment")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


VAST_MIN_POLL_INTERVAL_SEC = 60
BACKOFF_BASE_SEC = VAST_MIN_POLL_INTERVAL_SEC * 6  # minimum + 500% cushion


def _backoff_sleep(attempt: int, cap: float = BACKOFF_BASE_SEC * 4) -> None:
    # Align with Vast minimum poll interval plus headroom
    base_delay = BACKOFF_BASE_SEC * max(attempt, 1)
    delay = min(cap, base_delay)
    jitter = random.uniform(0, delay * 0.1)
    time.sleep(delay + jitter)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if not val:
        return default
    if val in {"1", "true", "yes", "on", "all"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    return default


def search_offers(
    rented: bool,
    rentable: Optional[bool] = True,
    extra_filters: Optional[Dict[str, Any]] = None,
    timeout_sec: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Call Vast Search Offers and return the list of offers.

    Retries on 429/5xx with exponential backoff and jitter, then raises VastAPIError.
    """
    url = SEARCH_OFFERS_ENDPOINT
    q: Dict[str, Any] = {
        "rented": {"eq": bool(rented)},
        "external": {"eq": False},
        "limit": 10000,
    }
    if rentable is not None:
        q["rentable"] = {"eq": bool(rentable)}
    include_unverified = _env_flag("VW_INCLUDE_UNVERIFIED", default=True)
    if not include_unverified:
        q["verified"] = {"eq": True}
    if extra_filters:
        # Shallow-merge keys into q, without overwriting core flags unless explicitly provided
        for k, v in extra_filters.items():
            q[k] = v

    payload = dict(q)
    payload.setdefault("type", "on-demand")
    headers = _headers()
    timeout = float(timeout_sec or os.environ.get("VW_TIMEOUT_SEC", 60))

    session = requests.Session()
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            if attempt < max_attempts - 1:
                _backoff_sleep(attempt + 1)
                continue
            raise VastAPIError(f"Network error contacting Vast: {e}")

        if resp.status_code == 200:
            content_type = resp.headers.get('Content-Type', '')
            if 'json' not in content_type.lower():
                snippet = resp.text[:200] if resp.text else ''
                raise VastAPIError(
                    f"Unexpected response content-type {content_type!r} from Vast; body starts with {snippet!r}"
                )
            try:
                data = resp.json()
            except ValueError as e:
                snippet = resp.text[:200] if resp.text else ''
                raise VastAPIError(
                    f"Invalid JSON from Vast (status={resp.status_code}): {e}; body starts with {snippet!r}"
                )
            # Vast responses typically include a top-level key with the list.
            offers = (
                data.get('offers')
                or data.get('matches')
                or data.get('data')
                or data.get('result')
            )
            if isinstance(offers, list):
                return offers
            # If payload structure differs, try to find array-like value
            for v in data.values():
                if isinstance(v, list):
                    return v
            # Otherwise raise a structured error
            raise VastAPIError("Unexpected response structure from Vast (no list of offers)")

        if resp.status_code in (429, 500, 502, 503, 504):
            if attempt < max_attempts - 1:
                # Honor Retry-After if present
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        time.sleep(float(retry_after))
                    except Exception:
                        _backoff_sleep(attempt + 1)
                else:
                    _backoff_sleep(attempt + 1)
                continue
            else:
                raise VastAPIError(
                    f"Vast API rate-limited or unavailable (status={resp.status_code})",
                    status=resp.status_code,
                )

        # Other non-success
        try:
            detail = resp.text[:500]
        except Exception:
            detail = ""
        raise VastAPIError(
            f"Vast API error (status={resp.status_code}): {detail}", status=resp.status_code
        )

    # Should never reach here
    raise VastAPIError("Exhausted retries with Vast API")


def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def _to_int(x: Any) -> int:
    try:
        return int(x)
    except Exception:
        return 0


def _normalize_vram_gb(offer: Dict[str, Any]) -> Optional[float]:
    # Try common fields; convert MB/bytes to GB where obvious
    candidates = [
        offer.get("gpu_total_ram_gb"),
        offer.get("gpu_total_ram"),
        offer.get("gpu_ram"),
        offer.get("gpu_mem"),
    ]
    for val in candidates:
        if val is None:
            continue
        try:
            f = float(val)
        except Exception:
            continue
        # Heuristics: if very large, maybe bytes; if > 200 likely MB
        if f > 1_000_000:  # bytes
            return f / (1024 ** 3)
        if f > 200:  # MB
            return f / 1024.0
        return f  # already GB
    return None


def _normalize_type(offer: Dict[str, Any]) -> Optional[str]:
    t = offer.get("type")
    if isinstance(t, str) and t:
        return t
    # Map booleans to strings if present
    if offer.get("interruptible") is True or offer.get("preemptible") is True:
        return "interruptible"
    if offer.get("interruptible") is False or offer.get("preemptible") is False:
        return "on-demand"
    return None


def _normalize_geo(offer: Dict[str, Any]) -> Optional[str]:
    geo = offer.get("geolocation") or offer.get("country") or offer.get("region")
    if geo:
        return str(geo)
    # Compose from city/country if present
    city = offer.get("city")
    country = offer.get("country_code") or offer.get("country")
    if city or country:
        return ", ".join([p for p in [city, country] if p])
    return None


def _to_bool_int(x: Any) -> Optional[int]:
    if x is None:
        return None
    if isinstance(x, bool):
        return 1 if x else 0
    if isinstance(x, (int, float)):
        return 1 if int(x) != 0 else 0
    if isinstance(x, str):
        lowered = x.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return 1
        if lowered in {"0", "false", "no", "n", "off"}:
            return 0
    return None


def normalize(offer: Dict[str, Any], ts: str, source_state: str | None = None) -> Dict[str, Any]:
    """Normalize Vast offer to the offers_raw schema."""
    offer_id = _to_int(offer.get("id") or offer.get("offer_id"))
    machine_id = _to_int(offer.get("machine_id") or offer.get("machine") or offer.get("machineID"))
    gpu_name = offer.get("gpu_name") or offer.get("gpu_name_short") or offer.get("gpu")
    num_gpus = _to_int(offer.get("num_gpus") or offer.get("numgpus") or offer.get("gpus") or 1)
    gpu_frac = _to_float(offer.get("gpu_frac") or offer.get("gpu_fraction") or 1.0)
    gpu_total_ram_gb = _normalize_vram_gb(offer)
    dph_total_usd = _to_float(offer.get("dph_total") or offer.get("dollars_per_hour") or offer.get("usd_per_hour"))
    reliability2 = _to_float(offer.get("reliability2") or offer.get("reliability"))
    geolocation = _normalize_geo(offer)
    type_str = _normalize_type(offer)
    rentable = _to_bool_int(offer.get("rentable"))
    rented = _to_bool_int(offer.get("rented"))
    src = (source_state or "").strip().lower() or None
    verified_val = offer.get("verified")
    if verified_val is None:
        verified_val = offer.get("is_verified")
    verified = _to_bool_int(verified_val)
    deverified_val = offer.get("deverified")
    deverified = _to_bool_int(deverified_val)

    verification_str = offer.get("verification")
    if isinstance(verification_str, str):
        lowered = verification_str.strip().lower()
        if lowered == "verified":
            verified = 1
            deverified = 0
        elif lowered == "deverified":
            deverified = 1
            verified = 0
    if offer.get("is_vm_deverified") is True:
        deverified = 1
    if verified is None:
        verified = 0
    if deverified is None:
        deverified = 0

    if src in {"available", "rented", "unavailable"}:
        availability_state = src
    else:
        if rentable == 1 and rented == 0:
            availability_state = "available"
        elif rented == 1:
            availability_state = "rented"
        elif rentable == 0 and rented == 0:
            availability_state = "unavailable"
        else:
            availability_state = "unknown"

    return {
        "ts": ts,
        "offer_id": offer_id,
        "machine_id": machine_id,
        "gpu_name": gpu_name,
        "num_gpus": num_gpus,
        "gpu_frac": gpu_frac,
        "gpu_total_ram_gb": gpu_total_ram_gb,
        "dph_total_usd": dph_total_usd,
        "reliability2": reliability2,
        "geolocation": geolocation,
        "type": type_str,
        "rentable": rentable,
        "rented": rented,
        "verified": verified,
        "deverified": deverified,
        "availability_state": availability_state,
    }
