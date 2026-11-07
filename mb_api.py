"""
mb_api.py - Conexion con "SofaScore - Live API" (Zyla Labs) para el bot
multiliga. Independiente del TT Avisador: solo comparte la clave de Zyla.

Ligas (unique_tournament_id de Sofascore):
  15005 TT Cup · 19039 Czech Liga Pro · 19041 TT Elite Series
"""
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

KEY = os.getenv("ZYLA_API_KEY", "").strip()

LIGAS = {"15005": "TT Cup", "19039": "Czech Liga Pro", "19041": "TT Elite Series"}

URL_TOURNAMENT = os.getenv(
    "ZYLA_URL_TOURNAMENT",
    "https://zylalabs.com/api/12787/sofascore+-+live+api/31095/get+scheduled+matches+from+unique+tournament")
URL_ODDS = os.getenv(
    "ZYLA_URL_ODDS",
    "https://zylalabs.com/api/12787/sofascore+-+live+api/25097/get+match+odds")
UA = {"User-Agent": "Mozilla/5.0 tt-multiliga"}

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Madrid"))
except Exception:                                   # pragma: no cover
    from datetime import timezone, timedelta
    TZ = timezone(timedelta(hours=1))


def _call(url, **params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={**UA, "Authorization": f"Bearer {KEY}"})
    for intento in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            break
        except (http.client.IncompleteRead, http.client.RemoteDisconnected,
                ConnectionResetError, json.JSONDecodeError):
            if intento == 2:
                raise
            time.sleep(2)
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"Zyla: {data.get('message', '')[:150]}")
    return data


def events_in(obj):
    """Encuentra la lista de partidos (dicts con homeTeam) en cualquier JSON."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "homeTeam" in obj[0]:
            return obj
        for x in obj:
            r = events_in(x)
            if r:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = events_in(v)
            if r:
                return r
    return []


def day_events(tournament_id, date):
    """Partidos de una liga en un dia (YYYY-MM-DD)."""
    return events_in(_call(URL_TOURNAMENT, unique_tournament_id=tournament_id, date=date))


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse(e):
    """Partido -> dict simple. Devuelve None si esta cancelado o es dudoso."""
    st = (e.get("status") or {}).get("type", "")
    hs, as_ = e.get("homeScore") or {}, e.get("awayScore") or {}
    sa = _int(hs.get("current", hs.get("display")))
    sb = _int(as_.get("current", as_.get("display")))
    final = None
    if st == "finished":
        if max(sa, sb) != 3 or sa == sb:
            return None
        wc = e.get("winnerCode")
        if wc in (1, 2) and (wc == 1) != (sa > sb):
            return None                      # ganador oficial y marcador no cuadran
        final = (sa, sb)
    elif st not in ("notstarted", "inprogress"):
        return None                          # cancelado, aplazado, retirado...
    home, away = e.get("homeTeam") or {}, e.get("awayTeam") or {}
    return dict(id=str(e.get("id")),
                a=home.get("name", "?"), b=away.get("name", "?"),
                ida=str(home.get("id", home.get("name", "?"))),
                idb=str(away.get("id", away.get("name", "?"))),
                t=_int(e.get("startTimestamp") or e.get("timestamp")),
                final=final, status=st,
                # puntos totales de cada jugador (suma de los sets); 0,0 si no vienen
                pts=(sum(_int(hs.get(f"period{i}")) for i in range(1, 8)),
                     sum(_int(as_.get(f"period{i}")) for i in range(1, 8))))


# --------------------------------------------------------------------- cuotas
def _dec(v):
    """'4/9' -> 1.44 · 1.44 -> 1.44 · {'decimal': 1.44} -> 1.44."""
    if v in (None, ""):
        return None
    if isinstance(v, dict):
        v = v.get("decimal") or v.get("fractional")
        if v in (None, ""):
            return None
    try:
        v = str(v)
        if "/" in v:
            a, b = v.split("/")
            return round(1 + float(a) / float(b), 3)
        f = float(v)
        return round(f, 3) if f > 1 else None
    except (ValueError, ZeroDivisionError):
        return None


def _markets(o):
    if isinstance(o, dict):
        if isinstance(o.get("markets"), list):
            return o["markets"]
        for v in o.values():
            r = _markets(v)
            if r:
                return r
    elif isinstance(o, list):
        if o and isinstance(o[0], dict) and "choices" in o[0]:
            return o
        for v in o:
            r = _markets(v)
            if r:
                return r
    return []


def odds_raw(match_id):
    """Respuesta cruda de cuotas ({} si el partido no tiene)."""
    try:
        return _call(URL_ODDS, match_id=match_id)
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            return {}
        raise


def odds_from(data):
    """(local_apertura, local_final, visit_apertura, visit_final) o None."""
    best = None
    for m in _markets(data or {}):
        names = sorted(str(c.get("name", "")) for c in m.get("choices", []))
        if names == ["1", "2"]:
            best = m
            if "full" in str(m.get("name", m.get("marketName", ""))).lower():
                break
    if not best:
        return None
    ch = {str(c.get("name")): c for c in best["choices"]}

    def pair(c):
        fin = _dec(c.get("fractionalValue")) or _dec(c.get("decimalValue")) or _dec(c.get("value"))
        ini = (_dec(c.get("initialFractionalValue")) or _dec(c.get("initialDecimalValue"))
               or _dec(c.get("initialValue")) or fin)
        return ini, fin
    hi, hf = pair(ch["1"])
    ai, af = pair(ch["2"])
    if not (hf and af):
        return None
    return hi, hf, ai, af
