"""
mb_bets.py - Lector de BetsAPI para el bot multiliga (sustituye a Zyla).

Da lo mismo que mb_api (formato Sofascore), para que el resto del bot no cambie:
  day_events(liga, "AAAA-MM-DD")  -> partidos de ese dia (hora de Madrid)
  odds_raw(id) + odds_from(...)   -> (local_apertura, local_final,
                                      visitante_apertura, visitante_final)
Cuotas: Bet365 a ganador; "final" = la ultima ANTES de empezar el partido.
Ids de partido con "b" delante (b13442667) y de jugador con "p" (p257688), para
no mezclarlos nunca con los de Sofascore.

Limite de BetsAPI: 3.600 consultas por hora entre TODOS los bots. Este lector
lleva la cuenta (LLAMADAS) y deja que el bot frene la descarga del historico.

Variables: BETSAPI_TOKEN
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Madrid"))
except Exception:                                   # pragma: no cover
    TZ = timezone(timedelta(hours=1))

TOKEN = os.getenv("BETSAPI_TOKEN", "").strip()
KEY = TOKEN                                         # (como mb_api.KEY: "hay fuente")
SPORT = "92"
LIGAS = {"15005": "29097",                          # TT Cup
         "19039": "22742",                          # Czech Liga Pro
         "19041": "29128"}                          # TT Elite Series
ESTADO = {"0": "notstarted", "1": "inprogress", "3": "finished"}
LLAMADAS = [0]
CACHE_HOY_S = int(os.getenv("BETS_CACHE_HOY_S", "240"))   # hoy/manana: refresco cada 4 min
CACHE_AYER_S = 1800                                 # dia UTC anterior: casi todo ya terminado
_UTC = {}                                           # (liga, dia_utc) -> (hora, [eventos crudos])
_VIVO = {}                                          # liga -> (hora, en juego)


def api(path, **params):
    params["token"] = TOKEN
    ultimo = None
    for host in ("https://api.b365api.com", "https://api.betsapi.com"):
        url = f"{host}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "tt-multiliga"})
        for intento in range(3):
            try:
                LLAMADAS[0] += 1
                with urllib.request.urlopen(req, timeout=25) as r:
                    d = json.loads(r.read().decode())
                if isinstance(d, dict) and str(d.get("success")) == "0":
                    raise RuntimeError(f"BetsAPI: {str(d.get('error', d))[:100]}")
                return d
            except urllib.error.HTTPError as ex:
                ultimo = RuntimeError(f"BetsAPI HTTP {ex.code}")   # sin la URL (lleva el token)
                if ex.code in (401, 403, 429):
                    break
            except Exception as ex:
                ultimo = RuntimeError(f"BetsAPI: {type(ex).__name__}")
            time.sleep(1 + intento)
    raise ultimo


def paginas(path, **params):
    out = []
    for page in range(1, 31):
        d = api(path, page=page, **params)
        res = d.get("results") or []
        out += res
        pg = d.get("pager") or {}
        tot, per = int(pg.get("total", 0) or 0), int(pg.get("per_page", 0) or 0)
        if not res or not per or page * per >= tot:
            break
    return out


# ---------------------------------------------------------------- conversion
def nombre(n):
    """'Rene Kowal' -> 'Kowal R.' (como Sofascore)."""
    t = str(n or "?").split()
    if len(t) < 2 or t[-1].endswith("."):
        return " ".join(t) or "?"
    return f"{' '.join(t[1:])} {t[0][0].upper()}."


def _hecho(a, b):
    return max(a, b) >= 11 and abs(a - b) >= 2


def convertir(e):
    """Partido de BetsAPI -> formato Sofascore (el que entiende mb_api.parse)."""
    st = ESTADO.get(str(e.get("time_status")), "canceled")
    sc = e.get("scores") or {}
    per = []
    for k in sorted(sc, key=lambda x: int(x) if str(x).isdigit() else 99):
        try:
            per.append((int(sc[k].get("home") or 0), int(sc[k].get("away") or 0)))
        except (TypeError, ValueError, AttributeError):
            pass
    hs, as_ = {}, {}
    for i, (a, b) in enumerate(per, 1):
        hs[f"period{i}"], as_[f"period{i}"] = a, b
    if st == "finished":
        try:
            sa, sb = (int(x) for x in str(e.get("ss") or "").split("-"))
        except ValueError:
            sa = sum(1 for a, b in per if a > b)
            sb = sum(1 for a, b in per if b > a)
    elif st == "inprogress":
        hechos = per if per and _hecho(*per[-1]) else per[:-1]
        sa = sum(1 for a, b in hechos if a > b)
        sb = sum(1 for a, b in hechos if b > a)
        if max(sa, sb) >= 3:
            st = "finished"
    else:
        sa = sb = 0
    hs["current"], as_["current"] = sa, sb
    h, a = e.get("home") or {}, e.get("away") or {}
    ev = {"id": "b" + str(e.get("id")), "status": {"type": st},
          "homeTeam": {"name": nombre(h.get("name")), "id": "p" + str(h.get("id") or h.get("name"))},
          "awayTeam": {"name": nombre(a.get("name")), "id": "p" + str(a.get("id") or a.get("name"))},
          "homeScore": hs, "awayScore": as_, "startTimestamp": int(e.get("time") or 0)}
    if st == "finished" and sa != sb:
        ev["winnerCode"] = 1 if sa > sb else 2
    return ev


# -------------------------------------------------------------------- dias
def _dia_utc(liga, dd, vivo, refresco=CACHE_HOY_S):
    """Partidos (crudos) de un dia UTC: terminados (+ proximos si vivo)."""
    key = (liga, dd)
    if key in _UTC and (not vivo or time.time() - _UTC[key][0] < refresco):
        return _UTC[key][1]
    evs = {}
    tipos = ("upcoming", "ended") if vivo else ("ended",)
    for tipo in tipos:                         # 'ended' despues: pisa a 'upcoming'
        for e in paginas(f"/v3/events/{tipo}", sport_id=SPORT, league_id=liga, day=dd):
            evs[str(e.get("id"))] = e
    _UTC[key] = (time.time(), list(evs.values()))
    return _UTC[key][1]


def day_events(lid, date):
    """Partidos de la liga (id de Sofascore: 15005, 19041...) del dia 'date'
    (hora de Madrid), ya en formato Sofascore."""
    liga = LIGAS.get(str(lid), str(lid))
    d = datetime.strptime(date, "%Y-%m-%d").date()
    hoy_utc = datetime.now(timezone.utc).date()
    evs = {}
    for k in (-1, 0):                          # el dia de Madrid cae en dos dias UTC
        du = d + timedelta(days=k)
        vivo = du >= hoy_utc - timedelta(days=1)
        ref = CACHE_HOY_S if du >= hoy_utc else CACHE_AYER_S
        for e in _dia_utc(liga, du.strftime("%Y%m%d"), vivo, ref):
            evs[str(e.get("id"))] = e
    if d >= hoy_utc - timedelta(days=1):
        # en juego ahora mismo (como mucho una consulta por minuto y liga)
        if liga not in _VIVO or time.time() - _VIVO[liga][0] > 50:
            _VIVO[liga] = (time.time(), [e for e in api("/v3/events/inplay", sport_id=SPORT,
                                                        league_id=liga).get("results") or []
                                         if str((e.get("league") or {}).get("id")) == liga])
        for e in _VIVO[liga][1]:
            evs[str(e.get("id"))] = e
        # lo que ya consta como terminado manda sobre lo que va en juego
        for k in (-1, 0):
            du = (d + timedelta(days=k)).strftime("%Y%m%d")
            for e in (_UTC.get((liga, du)) or (0, []))[1]:
                if str(e.get("time_status")) == "3":
                    evs[str(e.get("id"))] = e
    out = []
    for e in evs.values():
        ev = convertir(e)
        if datetime.fromtimestamp(ev["startTimestamp"], TZ).date() == d:
            out.append(ev)
    return out


# ------------------------------------------------------------------ cuotas
def odds_raw(match_id):
    mid = str(match_id)
    if not mid.startswith("b"):
        return {}                              # partido de Zyla: aqui no hay cuota
    try:
        return api("/v2/event/odds", event_id=mid[1:], odds_market=1)
    except RuntimeError as ex:
        if "HTTP 404" in str(ex):
            return {}
        raise


def odds_from(data):
    """(local_apertura, local_final, visit_apertura, visit_final) o None.
    Solo cuotas de ANTES de empezar (las de en directo llevan marcador y no cuentan)."""
    lista = (((data or {}).get("results") or {}).get("odds") or {}).get("92_1") or []
    pre = []
    for o in lista:
        if o.get("ss"):
            continue
        try:
            h, a, t = float(o["home_od"]), float(o["away_od"]), int(o.get("add_time") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if h > 1 and a > 1:
            pre.append((t, h, a))
    if not pre:
        return None
    pre.sort()
    return pre[0][1], pre[-1][1], pre[0][2], pre[-1][2]
