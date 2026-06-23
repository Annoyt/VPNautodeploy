"""Offline IP → country lookup for the dashboard and /onlines.

Uses the free, no-registration ``dbip-country-lite-YYYY-MM.mmdb``
published monthly by db-ip.com under CC-BY-4.0 (their lite series
is country-only, ~3 MB). MaxMind's own GeoLite2 would also work
but requires account signup + license key — overkill for just
mapping a real client IP to a flag.

Caching layers
--------------
1. The lookup function caches each IP → (country_code, flag) in a
   tiny in-process dict, so the dashboard's ~10 unique IPs per
   refresh are resolved at memory speed after the first request.
2. The mmdb file lives on disk under
   ``/var/lib/vpn-bot/geoip/dbip-country-lite.mmdb`` and is
   downloaded on first use; subsequent process restarts reuse it.
3. The DB is refreshed once every 7 days by a scheduler job (see
   NotificationService) so we keep up with allocation changes.

Failure modes — all soft, never raise:
- mmdb download blocked → in-memory cache stays empty → callers
  see ``None``.
- IP not in DB (private / unknown) → ``None``.
"""

from __future__ import annotations

import logging
import os
import time
import threading
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


_DB_DIR = os.environ.get("GEOIP_DB_DIR", "/var/lib/vpn-bot/geoip")
_DB_PATH = os.path.join(_DB_DIR, "dbip-country-lite.mmdb")
_ASN_DB_PATH = os.path.join(_DB_DIR, "dbip-asn-lite.mmdb")
_CITY_DB_PATH = os.path.join(_DB_DIR, "dbip-city-lite.mmdb")
# Source URL is templated by year-month (latest snapshot).
_DB_URL_FMT = "https://download.db-ip.com/free/dbip-country-lite-{ym}.mmdb.gz"
_ASN_DB_URL_FMT = "https://download.db-ip.com/free/dbip-asn-lite-{ym}.mmdb.gz"
_CITY_DB_URL_FMT = "https://download.db-ip.com/free/dbip-city-lite-{ym}.mmdb.gz"
# Refresh on disk after this many days
_REFRESH_S = 7 * 24 * 3600

_reader = None
_reader_lock = threading.Lock()
_asn_reader = None
_asn_reader_lock = threading.Lock()
_city_reader = None
_city_reader_lock = threading.Lock()
_cache: dict = {}  # ip -> (cc, flag) or (None, None)
_asn_cache: dict = {}  # ip -> (asn, as_org) or (None, None)
_city_cache: dict = {}  # ip -> (city, region, lat, lon) or all-None tuple


def _flag_emoji(country_code: str) -> str:
    """Two-letter ISO country code → flag emoji (regional indicators).

    "RU" → "🇷🇺", "TR" → "🇹🇷". Returns "" on bad input.
    """
    if not country_code or len(country_code) != 2 or not country_code.isalpha():
        return ""
    base = 0x1F1E6  # regional indicator A
    return chr(base + (ord(country_code[0].upper()) - ord("A"))) + \
           chr(base + (ord(country_code[1].upper()) - ord("A")))


def _download_db(url_fmt: str, target_path: str, kind: str) -> bool:
    """Pull the latest free DB-IP mmdb of the given kind. Returns True on success.

    ``url_fmt`` is the URL template with ``{ym}`` for year-month;
    ``kind`` is just a label for logs ('country' / 'asn').
    """
    import datetime
    import gzip
    import shutil
    import requests

    os.makedirs(_DB_DIR, exist_ok=True)

    # Try the current month first, then walk back up to 3 months —
    # db-ip publishes around the start of each month, so on the 1st
    # this month's slot might still be 404.
    for back in range(0, 3):
        d = datetime.date.today() - datetime.timedelta(days=30 * back)
        ym = d.strftime("%Y-%m")
        url = url_fmt.format(ym=ym)
        try:
            r = requests.get(url, stream=True, timeout=60)
            if r.status_code != 200:
                logger.info(f"GeoIP/{kind}: {url} -> HTTP {r.status_code}, trying older month")
                continue
            tmp = target_path + ".tmp"
            with open(tmp, "wb") as f:
                shutil.copyfileobj(gzip.GzipFile(fileobj=r.raw), f)
            os.replace(tmp, target_path)
            logger.info(f"GeoIP/{kind}: downloaded {url} → {target_path}")
            return True
        except Exception as e:
            logger.warning(f"GeoIP/{kind}: download {url} failed: {e}")
    return False


def _download_mmdb() -> bool:
    """Backward-compat shim: refreshes the country DB."""
    return _download_db(_DB_URL_FMT, _DB_PATH, 'country')


def _download_asn_mmdb() -> bool:
    return _download_db(_ASN_DB_URL_FMT, _ASN_DB_PATH, 'asn')


def _download_city_mmdb() -> bool:
    return _download_db(_CITY_DB_URL_FMT, _CITY_DB_PATH, 'city')


def _open_reader():
    """Open the mmdb (downloading if needed). Returns the reader or None."""
    global _reader
    if _reader is not None:
        return _reader
    try:
        import maxminddb
    except ImportError:
        logger.warning("maxminddb not installed — GeoIP disabled")
        return None
    with _reader_lock:
        if _reader is not None:
            return _reader
        # If file is missing OR older than refresh window, try fresh.
        try:
            st = os.stat(_DB_PATH)
            age_s = time.time() - st.st_mtime
        except OSError:
            age_s = float("inf")
        if age_s > _REFRESH_S:
            _download_mmdb()
        if not os.path.exists(_DB_PATH):
            return None
        try:
            _reader = maxminddb.open_database(_DB_PATH)
            return _reader
        except Exception as e:
            logger.warning(f"GeoIP: open mmdb failed: {e}")
            return None


def lookup(ip: str) -> Optional[Tuple[str, str]]:
    """Resolve an IP to (country_code, flag_emoji).

    Returns ``None`` if the DB isn't available or the IP doesn't
    resolve (typical for RFC1918 / unknown ranges).
    """
    if not ip:
        return None
    cached = _cache.get(ip)
    if cached is not None:
        return cached if cached[0] else None

    reader = _open_reader()
    if reader is None:
        _cache[ip] = (None, None)
        return None

    try:
        rec = reader.get(ip)
    except Exception:
        rec = None
    if not rec:
        _cache[ip] = (None, None)
        return None

    # db-ip lite schema: {"country": {"iso_code": "RU", "names": {...}}}
    iso = ((rec.get("country") or {}).get("iso_code") or "").upper()
    if not iso:
        _cache[ip] = (None, None)
        return None
    flag = _flag_emoji(iso)
    val = (iso, flag)
    _cache[ip] = val
    return val


def _open_asn_reader():
    """Open the ASN mmdb (downloading if needed). Returns the reader or None."""
    global _asn_reader
    if _asn_reader is not None:
        return _asn_reader
    try:
        import maxminddb
    except ImportError:
        return None
    with _asn_reader_lock:
        if _asn_reader is not None:
            return _asn_reader
        try:
            st = os.stat(_ASN_DB_PATH)
            age_s = time.time() - st.st_mtime
        except OSError:
            age_s = float("inf")
        if age_s > _REFRESH_S:
            _download_asn_mmdb()
        if not os.path.exists(_ASN_DB_PATH):
            return None
        try:
            _asn_reader = maxminddb.open_database(_ASN_DB_PATH)
            return _asn_reader
        except Exception as e:
            logger.warning(f"GeoIP/asn: open mmdb failed: {e}")
            return None


def lookup_asn(ip: str) -> Optional[Tuple[str, str]]:
    """Resolve an IP to (asn_number, organization_name).

    Examples:
        ("AS21299", "Kazakhtelecom JSC")
        ("AS9009",  "M247 Ltd")

    Returns ``None`` for private / unknown ranges or when the DB is
    not available. ``asn`` is prefixed with 'AS' for grouping.
    """
    if not ip:
        return None
    cached = _asn_cache.get(ip)
    if cached is not None:
        return cached if cached[0] else None
    reader = _open_asn_reader()
    if reader is None:
        _asn_cache[ip] = (None, None)
        return None
    try:
        rec = reader.get(ip)
    except Exception:
        rec = None
    if not rec:
        _asn_cache[ip] = (None, None)
        return None
    # db-ip asn-lite schema: {"autonomous_system_number": 9009,
    # "autonomous_system_organization": "M247 Ltd"}
    asn_num = rec.get("autonomous_system_number")
    org = rec.get("autonomous_system_organization") or ""
    if not asn_num:
        _asn_cache[ip] = (None, None)
        return None
    asn = f"AS{asn_num}"
    val = (asn, org)
    _asn_cache[ip] = val
    return val


def _open_city_reader():
    """Open the city mmdb (downloading if needed). Returns the reader
    or None. City DB is ~50-150 MB, larger than country/ASN, so we
    accept that the first cold open after a refresh takes a few
    seconds.
    """
    global _city_reader
    if _city_reader is not None:
        return _city_reader
    try:
        import maxminddb
    except ImportError:
        return None
    with _city_reader_lock:
        if _city_reader is not None:
            return _city_reader
        try:
            st = os.stat(_CITY_DB_PATH)
            age_s = time.time() - st.st_mtime
        except OSError:
            age_s = float("inf")
        if age_s > _REFRESH_S:
            _download_city_mmdb()
        if not os.path.exists(_CITY_DB_PATH):
            return None
        try:
            _city_reader = maxminddb.open_database(_CITY_DB_PATH)
            return _city_reader
        except Exception as e:
            logger.warning(f"GeoIP/city: open mmdb failed: {e}")
            return None


def lookup_city(ip: str):
    """Resolve an IP to ``(city, region, latitude, longitude)``.

    Returns ``None`` if the DB is unavailable, the IP doesn't
    resolve (private range etc), or the record carries no city.
    Accuracy is best-effort — db-ip city Lite gets the ~top-N
    Russian cities right and falls back to country-centroid for
    smaller regions. Both lat/lon are floats; the dashboard map
    uses them as marker positions.
    """
    if not ip:
        return None
    cached = _city_cache.get(ip)
    if cached is not None:
        # Sentinel: (None, None, None, None) == miss
        return cached if cached[0] or cached[2] is not None else None
    reader = _open_city_reader()
    if reader is None:
        _city_cache[ip] = (None, None, None, None)
        return None
    try:
        rec = reader.get(ip)
    except Exception:
        rec = None
    if not rec:
        _city_cache[ip] = (None, None, None, None)
        return None
    # db-ip city-lite schema (mmdb):
    #   {"city": {"names": {"en": "Moscow"}},
    #    "subdivisions": [{"names": {"en": "Moscow"}}],
    #    "location": {"latitude": 55.7558, "longitude": 37.6173, ...}}
    city = ((rec.get("city") or {}).get("names") or {}).get("en") or None
    region = None
    subs = rec.get("subdivisions") or []
    if subs:
        region = (subs[0].get("names") or {}).get("en") or None
    loc = rec.get("location") or {}
    lat = loc.get("latitude")
    lon = loc.get("longitude")
    # Coerce to floats, drop record if missing geo
    try:
        lat = float(lat) if lat is not None else None
        lon = float(lon) if lon is not None else None
    except (TypeError, ValueError):
        lat = lon = None
    if not city and lat is None:
        _city_cache[ip] = (None, None, None, None)
        return None
    val = (city, region, lat, lon)
    _city_cache[ip] = val
    return val


def refresh_db_sync() -> bool:
    """Force-download country, ASN and city mmdbs. Called by the
    weekly scheduler. Returns True if ANY succeeded — partial refresh
    is still useful.
    """
    global _reader, _asn_reader, _city_reader
    ok_country = _download_mmdb()
    ok_asn = _download_asn_mmdb()
    ok_city = _download_city_mmdb()
    if ok_country:
        with _reader_lock:
            try:
                if _reader is not None:
                    _reader.close()
            except Exception:
                pass
            _reader = None
            _cache.clear()
    if ok_asn:
        with _asn_reader_lock:
            try:
                if _asn_reader is not None:
                    _asn_reader.close()
            except Exception:
                pass
            _asn_reader = None
            _asn_cache.clear()
    if ok_city:
        with _city_reader_lock:
            try:
                if _city_reader is not None:
                    _city_reader.close()
            except Exception:
                pass
            _city_reader = None
            _city_cache.clear()
    return ok_country or ok_asn or ok_city
