"""
tt_betsapi.py - Lector de BetsAPI (api.b365api.com) para el avisador.

Convierte los partidos de BetsAPI al mismo formato que usa el resto del bot
(el de Sofascore), para que reglas, avisos y registro no cambien nada.
  - terminados de hoy   /v3/events/ended?sport_id=92&league_id=X&day=AAAAMMDD
  - en juego            /v3/events/inplay?sport_id=92&league_id=X
  - proximos de hoy     /v3/events/upcoming?sport_id=92&league_id=X&day=AAAAMMDD
  - un partido          /v1/event/view?event_id=N
Necesita el secreto BETSAPI_TOKEN. Los ids llevan delante una "b" (b13442667)
para no confundirlos nunca con los de Sofascore.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.getenv("BETSAPI_TOKEN", "").strip()
HOSTS = ["https://api.b365api.com", "https://api.betsapi.com"]
SPORT = "92"                                  # tenis de mesa en BetsAPI
LIGAS = {"15005": "29097",                    # TT Cup
         "19039": "22742",                    # Czech Liga Pro
         "15004": "22307",                    # Setka Cup
         "19041": "29128"}                    # TT Elite Series
ESTADO = {"0": "notstarted", "1": "inprogress", "3": "finished"}
CACHE_S = int(os.getenv("BETSAPI_CACHE_S", "120"))
_CACHE = {}                                   # (tipo, liga, dia) -> (hora, lista)
_ENJUEGO = {}                                 # liga -> ids vistos en juego aun sin terminar
_VISTO = {}                                   # id -> ultimo estado conocido (no retrocede)
_RANGO = {"notstarted": 0, "canceled": 0, "inprogress": 1, "finished": 2}
LLAMADAS = [0]                                # contador (para vigilar el limite por hora)
_HORAS = []                                   # hora de cada consulta (ultima hora)


def llamadas_hora():
    """Consultas hechas por ESTE bot en la ultima hora."""
    limite = time.time() - 3600
    while _HORAS and _HORAS[0] < limite:
        _HORAS.pop(0)
    return len(_HORAS)


def api(path, **params):
    params["token"] = TOKEN
    ultimo = None
    for host in HOSTS:
        url = f"{host}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "tt-avisador"})
        for intento in range(3):
            try:
                LLAMADAS[0] += 1
                _HORAS.append(time.time())
                with urllib.request.urlopen(req, timeout=25) as r:
                    data = json.loads(r.read().decode())
                if isinstance(data, dict) and str(data.get("success")) == "0":
                    raise RuntimeError(f"BetsAPI: {str(data.get('error', data))[:120]}")
                return data
            except urllib.error.HTTPError as ex:
                ultimo = RuntimeError(f"BetsAPI HTTP {ex.code}")   # sin la URL (lleva el token)
                if ex.code in (401, 403):
                    break                      # token: no se arregla reintentando
            except Exception as ex:
                ultimo = RuntimeError(f"BetsAPI: {type(ex).__name__}")
            time.sleep(1 + intento)
    raise ultimo


def _paginas(path, **params):
    out = []
    for page in range(1, 11):
        d = api(path, page=page, **params)
        res = d.get("results") or []
        out += res
        pg = d.get("pager") or {}
        tot, per = int(pg.get("total", 0) or 0), int(pg.get("per_page", 0) or 0)
        if not res or not per or page * per >= tot:
            break
    return out


def nombre(n):
    """'Rene Kowal' -> 'Kowal R.' (como Sofascore: el bot usa el 1.er trozo como apellido)."""
    t = str(n or "?").split()
    if len(t) < 2 or t[-1].endswith("."):
        return " ".join(t) or "?"
    return f"{' '.join(t[1:])} {t[0][0].upper()}."


# Nombre FIJO de cada jugador (por su numero en BetsAPI). Hay jugadores distintos
# con el mismo nombre corto (p. ej. dos "Cernik J."): el primero que aparece se
# queda "Cernik J." y el otro pasa a llamarse con el nombre entero ("Cernik Jiri").
# Una vez puesto, el nombre de un jugador NUNCA cambia (se guarda en la memoria).
NOMBRES = {}        # id de jugador -> nombre que usa el bot
_DUENO = {}         # nombre que usa el bot -> id de jugador


def cargar_nombres(d):
    for pid, n in (d or {}).items():
        if pid not in NOMBRES and n not in _DUENO:
            NOMBRES[pid] = n
            _DUENO[n] = pid


def _largo(n):
    """'Jiri Cernik' -> 'Cernik Jiri' (el apellido sigue siendo el 1.er trozo)."""
    t = str(n or "?").split()
    if len(t) < 2 or t[-1].endswith("."):
        return " ".join(t) or "?"
    return f"{' '.join(t[1:])} {t[0]}"


def nombre_jugador(p):
    p = p or {}
    pid = str(p.get("id") or "")
    if not pid:
        return nombre(p.get("name"))
    if pid in NOMBRES:
        return NOMBRES[pid]
    n = nombre(p.get("name"))
    if n in _DUENO:                       # ese nombre ya es de OTRO jugador
        n = _largo(p.get("name"))
        if n in _DUENO:
            n = f"{n} {pid}"              # hasta el nombre entero coincide
    NOMBRES[pid] = n
    _DUENO[n] = pid
    return n


def _registrar(lista):
    """Da nombre a los jugadores nuevos en orden de hora (siempre igual)."""
    for e in sorted(lista, key=lambda e: (int(e.get("time") or 0), str(e.get("id")))):
        nombre_jugador(e.get("home"))
        nombre_jugador(e.get("away"))


def _set_hecho(a, b):
    return max(a, b) >= 11 and abs(a - b) >= 2


def convertir(e):
    """Partido de BetsAPI -> formato Sofascore que entiende tt_feed.parse()."""
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
        # en juego: 'ss' no sirve (a veces son los puntos del set); se cuentan solo
        # los sets YA terminados; el ultimo, si no ha acabado, es el set en curso
        hechos = per if per and _set_hecho(*per[-1]) else per[:-1]
        sa = sum(1 for a, b in hechos if a > b)
        sb = sum(1 for a, b in hechos if b > a)
        if max(sa, sb) >= 3:
            # PARECE acabado pero BetsAPI aun NO lo da por terminado: NUNCA se da por
            # terminado por cuenta propia (un dato de set erroneo mando un pick falso).
            # El ultimo set se trata como el set en juego hasta que BetsAPI lo cierre.
            hechos = hechos[:-1]
            sa = sum(1 for a, b in hechos if a > b)
            sb = sum(1 for a, b in hechos if b > a)
        elif len(hechos) == len(per):
            n = len(per) + 1                  # set nuevo, aun 0-0
            hs[f"period{n}"], as_[f"period{n}"] = 0, 0
    else:
        sa = sb = 0
    hs["current"], as_["current"] = sa, sb
    lg = e.get("league") or {}
    ev = {"id": "b" + str(e.get("id")), "status": {"type": st},
          "homeTeam": {"name": nombre_jugador(e.get("home"))},
          "awayTeam": {"name": nombre_jugador(e.get("away"))},
          "homeScore": hs, "awayScore": as_,
          "startTimestamp": int(e.get("time") or 0),
          "tournament": {"name": lg.get("name"),
                         "uniqueTournament": {"id": lg.get("id"), "name": lg.get("name")}},
          "bet365_id": e.get("bet365_id")}
    if st == "finished" and sa != sb:
        ev["winnerCode"] = 1 if sa > sb else 2
    return ev


def _sin_retroceso(ev, aceptar=True):
    """Un partido terminado nunca vuelve a 'en juego' ni a 'sin empezar' porque
    una lista vaya con retraso. Con aceptar=False un 'terminado' NO se da por
    bueno todavia (lo confirma luego quien lo pidio)."""
    prev = _VISTO.get(ev["id"])
    if prev and _RANGO[prev["status"]["type"]] > _RANGO.get(ev["status"]["type"], 0):
        return prev
    if ev["status"]["type"] == "finished" and not aceptar:
        return ev
    _VISTO[ev["id"]] = ev
    return ev


def aceptado(mid):
    """¿Este partido ya se ha dado por TERMINADO con todas las comprobaciones?"""
    v = _VISTO.get(str(mid))
    return bool(v and v["status"]["type"] == "finished")


def aceptar(ev):
    _VISTO[ev["id"]] = ev


def _sets_de(e):
    """Los sets juego a juego tal cual los da BetsAPI (para comparar dos lecturas)."""
    sc = e.get("scores") or {}
    out = []
    for k in sorted(sc, key=lambda x: int(x) if str(x).isdigit() else 99):
        try:
            out.append((int(sc[k].get("home") or 0), int(sc[k].get("away") or 0)))
        except (TypeError, ValueError, AttributeError):
            pass
    return tuple(out)


def sigue_en_juego(mid, sofascore_id):
    """Comprobacion cruzada con la lista de partidos EN JUEGO de BetsAPI: True si el
    partido sale ahi con OTRO marcador juego a juego (o sea, aun se esta jugando y
    el 'terminado' no cuadra). Si sale con el mismo marcador final es solo que la
    lista tarda en quitarlo: eso no frena el aviso. Si no se puede leer: True."""
    liga = LIGAS.get(str(sofascore_id), str(sofascore_id))
    try:
        res = api("/v3/events/inplay", sport_id=SPORT, league_id=liga).get("results") or []
    except Exception:
        return True
    vivo = next((e for e in res if str(e.get("id")) == str(mid).lstrip("b")), None)
    if vivo is None:
        return False
    ref = CRUDO.get(str(mid)) or {}
    return _sets_de(vivo) != _sets_de(ref)


def eventos(sofascore_id, days, vivos=True):
    """Todos los partidos de la liga en esos dias (AAAA-MM-DD), ya convertidos.
    Terminados y proximos se piden como mucho cada CACHE_S segundos (salvo que
    acabe un partido: entonces los terminados se piden al momento); los que
    estan en juego, siempre frescos."""
    liga = LIGAS.get(str(sofascore_id), str(sofascore_id))
    ahora = time.time()
    vivos_ahora = []
    if vivos:
        vivos_ahora = [e for e in (api("/v3/events/inplay", sport_id=SPORT, league_id=liga)
                                   .get("results") or [])
                       if str((e.get("league") or {}).get("id")) == liga]
    ids_vivos = {str(e.get("id")) for e in vivos_ahora}
    vivos_por_id = {str(e.get("id")): e for e in vivos_ahora}
    evs = {}
    for tipo in ("upcoming", "ended"):        # orden: proximos < en juego < terminados
        if tipo == "ended":
            for e in vivos_ahora:              # en juego (fresco) pisa a 'proximos'
                evs[str(e.get("id"))] = e
        hoy_utc = time.strftime("%Y%m%d", time.gmtime(ahora))
        for d in days:
            dd = d.replace("-", "")
            key = (tipo, liga, dd)
            # un dia UTC ya cerrado (hace mas de 1 h) casi no cambia: cada 30 min
            cerrado = dd < hoy_utc and time.gmtime(ahora).tm_hour >= 1
            viejo = key not in _CACHE or ahora - _CACHE[key][0] > (1800 if cerrado else CACHE_S)
            if tipo == "ended" and not viejo and not cerrado:
                # un partido que estaba en juego ya no lo esta y aun no sale entre
                # los terminados guardados: pedirlos YA (resultado al instante)
                hechos = {str(e.get("id")) for e in _CACHE[key][1]}
                viejo = bool(_ENJUEGO.get(liga, set()) - ids_vivos - hechos)
            if viejo:
                _CACHE[key] = (ahora, _paginas(f"/v3/events/{tipo}", sport_id=SPORT,
                                               league_id=liga, day=dd))
            for e in _CACHE[key][1]:
                v = vivos_por_id.get(str(e.get("id")))
                if tipo == "ended" and v is not None and _sets_de(v) != _sets_de(e):
                    continue                  # sale EN JUEGO con otro marcador: las dos
                                              # listas no cuadran, aun no se da por terminado
                evs[str(e.get("id"))] = e     # 'terminados' (va al final) pisa a todo
    _registrar(evs.values())
    if vivos:
        # los que dejan de verse en juego se siguen esperando hasta que salgan terminados
        hechos = {k for k, e in evs.items() if str(e.get("time_status")) == "3"}
        _ENJUEGO[liga] = ids_vivos | ((_ENJUEGO.get(liga, set()) - hechos) & set(evs))
    return [_sin_retroceso(convertir(e)) for e in evs.values()]


CRUDO = {}          # id -> ultimo dato tal cual llega de BetsAPI (para el registro)


def evento(mid):
    """Un partido concreto, fresco (para vigilar el partido decisivo)."""
    d = api("/v1/event/view", event_id=str(mid).lstrip("b"))
    res = d.get("results") or []
    if res:
        CRUDO[str(mid)] = {k: res[0].get(k) for k in ("id", "time_status", "ss", "scores",
                                                      "home", "away", "time")}
    # un 'terminado' leido aqui aun NO se da por bueno: lo acepta watch_hot tras
    # leerlo 2 veces y ver que ya no esta en la lista de partidos en juego
    return _sin_retroceso(convertir(res[0]), aceptar=False) if res else None


def cuota_prepartido(mid):
    """(local, visitante): la ultima cuota a ganador de Bet365 ANTES de empezar el
    partido (las de en directo no cuentan). None si no hay."""
    d = api("/v2/event/odds", event_id=str(mid).lstrip("b"), odds_market=1)
    lista = ((d.get("results") or {}).get("odds") or {}).get("92_1") or []
    mejor = None
    for o in lista:
        if o.get("ss"):                       # tiene marcador: es cuota en directo
            continue
        try:
            h, a, t = float(o["home_od"]), float(o["away_od"]), int(o.get("add_time") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if h > 1 and a > 1 and (mejor is None or t > mejor[2]):
            mejor = (h, a, t)
    return (mejor[0], mejor[1]) if mejor else None
