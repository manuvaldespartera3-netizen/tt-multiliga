"""
mb_rating_vivo.py - Canal 🏅 RATING LIGA PRO (seguimiento, sin dinero hasta confirmarlo).

Estrategia (confirmada en el analisis mb_rating.py): el RATING OFICIAL de la web
de Liga Pro (tt.league-pro.com, el de cada jugador antes del torneo) corrige la
probabilidad que da la cuota de la casa. Si el modelo ve valor >= 3% en un lado,
aviso.

Cada vuelta:
  1. Web oficial: torneos de hoy/manana con el rating de cada jugador.
  2. BetsAPI: proximos partidos de Liga Pro (liga 22742) y su cuota Bet365.
  3. En la ultima hora antes de cada partido (cada 10 min) calcula el valor
     con la formula guardada en resultados/czech_liga_pro/modelo_rating.json.
  4. Apunta el resultado cuando termina y manda el resumen a las 23:45.
Respondiendo a un aviso con tu cuota de BetCris queda apuntada.
Escribe 'estado' o 'resumen' al bot cuando quieras.

Variables: BETSAPI_TOKEN, TELEGRAM_TOKEN_RATING, MY_CHAT_ID, RUN_MIN (14)
Archivos: seguimiento/rating_picks.csv · seguimiento/rating_estado.json
"""
import csv
import json
import math
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import mb_marcas as MK

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Madrid")
except Exception:                                   # pragma: no cover
    TZ = timezone(timedelta(hours=1))

TOKEN = os.getenv("BETSAPI_TOKEN", "").strip()
TG = os.getenv("TELEGRAM_TOKEN_RATING", "").strip()
CHAT = os.getenv("MY_CHAT_ID", "").strip()
RUN_MIN = float(os.getenv("RUN_MIN", "14"))
LIGA = "22742"                                      # Czech Liga Pro en BetsAPI
WEB = "https://tt.league-pro.com"
MODELO = "resultados/czech_liga_pro/modelo_rating.json"
PICKS = "seguimiento/rating_picks.csv"
ESTADO = "seguimiento/rating_estado.json"
VENTANA = 60 * 60          # se mira en la ultima hora antes del partido...
CADA = 10 * 60             # ...cada 10 minutos
CAMPOS = ["fecha", "hora", "pick", "rival", "rating_pick", "rating_rival", "prob_casa",
          "prob_modelo", "cuota_aviso", "valor", "cuota_minima", "cuota_cierre", "cuota_betcris",
          "decision_betcris", "resultado", "marcador", "beneficio", "beneficio_cierre",
          "beneficio_betcris", "aviso", "msg", "id", "no_esta"]


# ================================================================ utilidades
def num(v):
    try:
        x = float(str(v).replace(",", "."))
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", s).split()


def api(path, **params):
    params["token"] = TOKEN
    ultimo = None
    for host in ("https://api.b365api.com", "https://api.betsapi.com"):
        url = f"{host}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "tt-multiliga"})
        for intento in range(3):
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    d = json.loads(r.read().decode())
                if isinstance(d, dict) and str(d.get("success")) == "0":
                    raise RuntimeError(f"BetsAPI: {str(d.get('error', d))[:100]}")
                return d
            except urllib.error.HTTPError as ex:
                ultimo = RuntimeError(f"BetsAPI HTTP {ex.code}")      # sin la URL (lleva el token)
                if ex.code in (401, 403):
                    break
            except Exception as ex:
                ultimo = RuntimeError(f"BetsAPI: {type(ex).__name__} {str(ex)[:60]}")
            time.sleep(1 + intento)
    raise ultimo


def paginas(path, **params):
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


def tg(text, reply_to=None):
    if not TG or not CHAT:
        print("TG>", text)
        return None
    data = {"chat_id": CHAT, "text": text}
    if reply_to:
        data["reply_to_message_id"] = reply_to
    for intento in range(4):
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{TG}/sendMessage",
                                         data=urllib.parse.urlencode(data).encode())
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())["result"]["message_id"]
        except urllib.error.HTTPError as ex:
            if ex.code == 400 and "reply_to_message_id" in data:
                data.pop("reply_to_message_id")
                continue
            if ex.code == 429:
                time.sleep(5)
        except Exception as ex:
            print("WARN telegram", ex)
        time.sleep(2 * (intento + 1))
    return None


def tg_updates(offset):
    if not TG:
        return []
    try:
        url = f"https://api.telegram.org/bot{TG}/getUpdates?timeout=0&offset={offset}"
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode()).get("result", [])
    except Exception as ex:
        print("WARN getUpdates", ex)
        return []


# ===================================================================== web
def nuxt(html):
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    d = json.loads(m.group(1))
    memo = {}
    env = ("Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef", "NuxtError")

    def r(i):
        if i in memo:
            return memo[i]
        v = d[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in env:
                out = r(v[1]) if len(v) > 1 else None
            elif v and isinstance(v[0], str) and v[0] == "Set":
                out = [r(x) for x in v[1:]]
            elif v and isinstance(v[0], str) and v[0] == "Date":
                out = v[1]
            else:
                memo[i] = out = []
                out.extend(r(x) if isinstance(x, int) else x for x in v)
                return out
        elif isinstance(v, dict):
            memo[i] = out = {}
            for k, x in v.items():
                out[k] = r(x) if isinstance(x, int) else x
            return out
        else:
            out = v
        memo[i] = out
        return out
    return r(0)


def bajar(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def ts_iso(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def torneo(tid):
    """{inicio, jugadores: [{ap, ini, rating}]} o None."""
    try:
        pd = nuxt(bajar(f"{WEB}/en/tournaments/{tid}"))["data"]["tournament-page"]["pageData"]
        t, w = pd["tournament"], pd["widgets"]
    except Exception:
        return None
    js = []
    for s in w.get("sides") or []:
        p = s.get("player") or {}
        if p and s.get("rating_before_tournament") is not None:
            pila = norm(p.get("first_name_en", ""))
            js.append(dict(ap=norm(p.get("surname_en", "")), ini=pila[0][0] if pila else "",
                           rating=int(round(float(s["rating_before_tournament"]))), nombre=p.get("surname_en", "")))
    return dict(inicio=ts_iso(t["start_date"]), jugadores=js, nombre=t.get("name_en", ""))


def torneos_web(st, now):
    """Torneos de hoy y manana (se guardan: el rating previo no cambia)."""
    tw = st.setdefault("torneos", {})
    d0 = now.date()
    html = bajar(f"{WEB}/en/tournaments?date_from={d0}&date_to={d0 + timedelta(days=1)}")
    root = nuxt(html)
    texto = html + (json.dumps(root) if root else "")
    ids = {int(x) for x in re.findall(r"/tournaments/(\d{5})(?!\d)", texto)}
    ids |= {int(x) for x in re.findall(r'"id":\s*(\d{5})(?!\d)', texto)}
    if not ids:
        raise RuntimeError("la web oficial no da torneos")
    # la lista de la web puede no traerlos todos: se miran tambien los numeros
    # intermedios y unos cuantos por encima (los torneos van numerados seguidos)
    vacios = st.setdefault("vacios", {})
    nuevos = 0
    for tid in range(min(ids), max(ids) + 16):
        k = str(tid)
        if tw.get(k) or now.timestamp() - vacios.get(k, 0) < 2 * 3600:
            continue
        tr = torneo(tid)
        nuevos += 1
        if tr and tr["jugadores"]:
            tw[k] = tr
            vacios.pop(k, None)
        else:
            vacios[k] = now.timestamp()
        time.sleep(0.8)
    for k in [k for k, v in vacios.items() if now.timestamp() - v > 86400]:
        del vacios[k]
    # fuera los de hace mas de 2 dias
    for k in [k for k, v in tw.items() if not v or v["inicio"] < now.timestamp() - 2 * 86400]:
        del tw[k]
    return nuevos


def es_jugador(j, nombre_bets):
    """'Milan Smrcek' (BetsAPI) frente a {ap:['smrcek'], ini:'m'} (web)."""
    w = norm(nombre_bets)
    if not w or not j["ap"]:
        return False
    if w[-len(j["ap"]):] == j["ap"] and (not j["ini"] or w[0][0] == j["ini"]):
        return True
    # 'Nohejl 2005 Vaclav' u otros ordenes: apellido entero dentro y la inicial
    return all(a in w for a in j["ap"]) and (not j["ini"] or any(x[0] == j["ini"] for x in w
                                                                   if x not in j["ap"]))


def ratings(st, e):
    """(rating_local, rating_visitante) del torneo de ese partido, o None."""
    t = int(e["time"])
    h, a = (e.get("home") or {}).get("name", ""), (e.get("away") or {}).get("name", "")
    mejor = None
    for tr in st.get("torneos", {}).values():
        if not tr or not (tr["inicio"] - 1800 <= t <= tr["inicio"] + 6 * 3600):
            continue
        jh = next((j for j in tr["jugadores"] if es_jugador(j, h)), None)
        ja = next((j for j in tr["jugadores"] if es_jugador(j, a)), None)
        if jh and ja and jh is not ja:
            if mejor is None or abs(tr["inicio"] - t) < mejor[0]:
                mejor = (abs(tr["inicio"] - t), jh["rating"], ja["rating"])
    return (mejor[1], mejor[2]) if mejor else None


# ================================================================== modelo
def cargar_modelo():
    with open(MODELO) as f:
        return json.load(f)


def logit(p):
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def prob_local(mod, ch, ca, rh, ra):
    """Probabilidad de que gane el local segun RATING + CASA."""
    pm = (1 / ch) / (1 / ch + 1 / ca)
    x = [logit(pm), rh - ra]
    z = sum(((x[i] - mod["mu"][i]) / mod["sd"][i]) * mod["w"][i] for i in range(2)) + mod["b"]
    return 1 / (1 + math.exp(-max(-30, min(30, z)))), pm


def cuota365(eid):
    """(local, visitante) a ganador en Bet365 ahora mismo, o None."""
    d = api("/v2/event/odds/summary", event_id=eid)
    b = ((d.get("results") or {}).get("Bet365") or {}).get("odds") or {}
    for k in ("end", "kickoff", "start"):
        o = (b.get(k) or {}).get("92_1") or {}
        h, a = num(o.get("home_od")), num(o.get("away_od"))
        if h and a:
            return h, a
    return None


def cuota_cierre(eid):
    """(local, visitante): ultima cuota de Bet365 ANTES de empezar (sin las de en directo)."""
    d = api("/v2/event/odds", event_id=eid, odds_market=1)
    mejor = None
    for o in ((d.get("results") or {}).get("odds") or {}).get("92_1") or []:
        if o.get("ss"):
            continue
        h, a = num(o.get("home_od")), num(o.get("away_od"))
        t = int(o.get("add_time") or 0)
        if h and a and (mejor is None or t > mejor[2]):
            mejor = (h, a, t)
    return (mejor[0], mejor[1]) if mejor else None


def _es_local(p):
    """True si el pick era el local del partido (None si no se puede saber)."""
    try:
        res = api("/v1/event/view", event_id=p["id"]).get("results") or []
    except Exception:
        return None
    if not res:
        return None
    return (res[0].get("home") or {}).get("name", "") == p["pick"]


def decidir(mod, e, rr, cuotas):
    """El lado con mas valor si llega al umbral; si no, None."""
    ch, ca = cuotas
    ph, pm = prob_local(mod, ch, ca, *rr)
    mejor = None
    for lado, p, c, pc in (("home", ph, ch, pm), ("away", 1 - ph, ca, 1 - pm)):
        v = p * c - 1
        if mod["min_odd"] <= c <= mod["max_odd"] and v >= mod["umbral"]:
            if mejor is None or v > mejor["valor"]:
                mejor = dict(lado=lado, p=p, c=c, pc=pc, valor=v)
    return mejor


# ============================================================= registro
def leer_picks():
    if not os.path.exists(PICKS):
        return []
    with open(PICKS, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def guardar_picks(rows):
    os.makedirs(os.path.dirname(PICKS), exist_ok=True)
    with open(PICKS, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CAMPOS})


def leer_estado():
    try:
        with open(ESTADO) as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_estado(st):
    os.makedirs(os.path.dirname(ESTADO), exist_ok=True)
    with open(ESTADO, "w") as f:
        json.dump(st, f)


# ================================================================= avisos
def nuevos_avisos(mod, proximos, picks, st, now):
    ya = {p["id"] for p in picks}
    mirados = st.setdefault("mirados", {})
    diag = st.setdefault("diag", {})
    ahora = now.timestamp()
    for e in sorted(proximos, key=lambda x: int(x.get("time") or 0)):
        eid = str(e["id"])
        falta = int(e["time"]) - ahora
        if eid in ya or not (0 < falta <= VENTANA) or ahora - mirados.get(eid, 0) < CADA:
            continue
        mirados[eid] = ahora
        diag["mirados"] = diag.get("mirados", 0) + 1
        rr = ratings(st, e)
        if not rr:
            diag["sin_rating"] = diag.get("sin_rating", 0) + 1
            continue
        try:
            cu = cuota365(eid)
        except Exception as ex:
            print("WARN cuota", eid, ex)
            cu = None
        if not cu:
            diag["sin_cuota"] = diag.get("sin_cuota", 0) + 1
            continue
        d = decidir(mod, e, rr, cu)
        if not d:
            continue
        diag["avisos"] = diag.get("avisos", 0) + 1
        h, a = (e.get("home") or {}).get("name", "?"), (e.get("away") or {}).get("name", "?")
        pick, rival = (h, a) if d["lado"] == "home" else (a, h)
        rp, rv = rr if d["lado"] == "home" else (rr[1], rr[0])
        minima = math.ceil((1 + mod["umbral"]) / d["p"] * 100) / 100
        dt = datetime.fromtimestamp(int(e["time"]), TZ)
        txt = (f"🏅 PICK RATING LIGA PRO · {dt:%H:%M}\n"
               f"✅ {pick.upper()}\n"
               f"vs {rival}\n\n"
               f"💰 APUESTA si BetCris ≥ {minima:.2f}\n"
               f"❌ menos de {minima:.2f} → no apostar\n\n"
               f"Rating oficial: {rp} vs {rv} ({rp - rv:+d})\n"
               f"Bet365 ahora: {d['c']:.2f} · casa {d['pc'] * 100:.0f}% → modelo {d['p'] * 100:.0f}% "
               f"· valor {d['valor'] * 100:+.0f}%\n"
               f"↩️ Responde con tu cuota de BetCris para apuntarla\n"
               f"🧪 Seguimiento · sin dinero hasta confirmarlo")
        pk = dict(fecha=dt.strftime("%Y-%m-%d"), hora=dt.strftime("%H:%M"), pick=pick,
                  rival=rival, rating_pick=rp, rating_rival=rv,
                  prob_casa=round(d["pc"], 3), prob_modelo=round(d["p"], 3),
                  cuota_aviso=d["c"], valor=round(d["valor"], 3), cuota_minima=minima,
                  resultado="pendiente", aviso=now.strftime("%Y-%m-%d %H:%M"),
                  msg="", id=eid, lado=d["lado"])
        pk["msg"] = MK.enviar(TG, CHAT, "rating", txt, pk) or ""
        picks.append(pk)
        ya.add(eid)
    for k in [k for k, v in mirados.items() if ahora - v > 86400]:
        del mirados[k]


def resolver(picks, now):
    """Apunta el resultado de los avisos cuyo partido ya ha tenido tiempo de acabar."""
    hechos = 0
    for p in picks:
        if p.get("resultado") != "pendiente":
            continue
        t = datetime.strptime(f"{p['fecha']} {p['hora']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        if now.timestamp() - t.timestamp() < 20 * 60:
            continue
        try:
            res = api("/v1/event/view", event_id=p["id"]).get("results") or []
        except Exception as ex:
            print("WARN resultado", p["id"], ex)
            continue
        if not res:
            continue
        e = res[0]
        st_ = str(e.get("time_status"))
        if st_ in ("4", "5", "7", "8", "9", "10"):           # aplazado, cancelado...
            p["resultado"] = "anulado"
            continue
        if st_ != "3":
            if now.timestamp() - t.timestamp() > 6 * 3600:
                p["resultado"] = "anulado"
            continue
        try:
            sh, sa = (int(x) for x in str(e.get("ss")).split("-"))
        except ValueError:
            continue
        local = (e.get("home") or {}).get("name", "") == p["pick"]
        gana = (sh > sa) == local
        p["resultado"] = "ganado" if gana else "perdido"
        p["marcador"] = f"{sh}-{sa}" if local else f"{sa}-{sh}"
        try:
            cu = cuota_cierre(p["id"])
            if cu:
                p["cuota_cierre"] = cu[0] if local else cu[1]
        except Exception:
            pass
        for campo, cuota in (("beneficio", p.get("cuota_aviso")),
                             ("beneficio_cierre", p.get("cuota_cierre"))):
            c = num(cuota)
            if c:
                p[campo] = round(c - 1, 3) if gana else -1
        calc_betcris(p)
        hechos += 1
        tg(f"{'✅' if gana else '❌'} {p['pick'].upper()} {p['marcador']} vs {p['rival']} "
           f"({p['hora']})" + (f" · BetCris {p['cuota_betcris']}" if p.get("cuota_betcris") else ""),
           reply_to=p.get("msg") or None)
    return hechos


def calc_betcris(p):
    cb, cm = num(p.get("cuota_betcris")), num(p.get("cuota_minima"))
    if not cb or not cm:
        return
    p["decision_betcris"] = "APUESTA" if cb >= cm else "FLOJO"
    if p.get("resultado") in ("ganado", "perdido") and p["decision_betcris"] == "APUESTA":
        p["beneficio_betcris"] = round(cb - 1, 3) if p["resultado"] == "ganado" else -1


# ============================================================ resumenes
def real(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def linea(rows, campo):
    rs = [r for r in rows if r.get("resultado") in ("ganado", "perdido") and real(r.get(campo)) is not None]
    if not rs:
        return "—"
    g = sum(1 for r in rs if r["resultado"] == "ganado")
    b = sum(real(r[campo]) for r in rs)
    return f"{len(rs)} picks · {g}✅ {len(rs) - g}❌ ({g / len(rs) * 100:.0f}%) · {b:+.2f} u · yield {b / len(rs) * 100:+.1f}%"


def texto_resumen(picks, now, titulo):
    hoy = now.strftime("%Y-%m-%d")
    dia = [p for p in picks if p["fecha"] == hoy]
    ne = sum(1 for p in dia if p.get("no_esta") == "si")
    bl = sum(1 for p in dia if MK.es_blanco(p))
    return (f"{titulo}\n"
            f"HOY ({len(dia)} avisos · {sum(1 for p in dia if p.get('resultado') == 'pendiente')} pendientes"
            + (f" · ⚫ {ne} no estaban en BetCris" if ne else "")
            + (f" · ⚪ {bl} sin tu cuota" if bl else "") + ")\n"
            f"  Bet365 al avisar: {linea(dia, 'beneficio')}\n"
            f"  Tus apuestas BetCris: {linea(dia, 'beneficio_betcris')}\n"
            f"TOTAL desde el inicio\n"
            f"  Bet365 al avisar: {linea(picks, 'beneficio')}\n"
            f"  Bet365 al cierre: {linea(picks, 'beneficio_cierre')}\n"
            f"  Tus apuestas BetCris: {linea(picks, 'beneficio_betcris')}\n"
            f"🧪 Seguimiento sin dinero. El análisis pedía +3% de valor; en los meses de "
            f"confirmación dio entre −2% y +32% de yield.")


def leer_mensajes(picks, st, now):
    off = st.get("offset", 0)
    por_msg = {str(p.get("msg")): p for p in picks if p.get("msg")}
    for u in tg_updates(off):
        st["offset"] = u["update_id"] + 1
        b = MK.boton(u, picks, CHAT)
        if b:                                          # boton [🚫 No está el partido]
            MK.aplicar_boton(TG, CHAT, "rating", *b)
            continue
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != CHAT:
            continue
        txt = (m.get("text") or "").strip()
        low = txt.lower().lstrip("/")
        rep = str((m.get("reply_to_message") or {}).get("message_id", ""))
        c = num(txt.split()[0]) if txt else None
        destino = por_msg.get(rep) if rep in por_msg else None
        if not destino and txt and len(txt.split()) >= 2 and num(txt.split()[-1]):
            # 'kadavy 1,85' sin responder: el pick mas reciente con ese apellido
            c = num(txt.split()[-1])
            pal = [w for w in norm(" ".join(txt.split()[:-1])) if len(w) >= 3]
            cand = [p for p in picks if pal and all(w in norm(p["pick"]) for w in pal)]
            cand = [p for p in cand if MK.es_blanco(p)] or cand
            destino = max(cand, key=lambda p: (p["fecha"], p["hora"])) if cand else None
        if destino and c and 1.01 <= c <= 50:
            p = destino
            p["cuota_betcris"] = c
            p["no_esta"] = ""
            calc_betcris(p)
            MK.actualizar(TG, CHAT, "rating", p)
            tg(f"📝 Apuntado {p['pick'].upper()} ({p['hora']}) · BetCris {c:.2f} · mínima "
               f"{float(p['cuota_minima']):.2f} → "
               + ("💰 APUESTA" if p.get("decision_betcris") == "APUESTA" else "❌ no llega, no apostar"),
               reply_to=m.get("message_id"))
        elif low.startswith(("pendientes", "pendiente", "faltan")):
            tg(MK.texto_pendientes(picks, now.strftime("%Y-%m-%d"),
                                   (now - timedelta(days=1)).strftime("%Y-%m-%d")),
               reply_to=m.get("message_id"))
        elif low.startswith("estado"):
            d = st.get("diag", {})
            tg(f"✅ Funcionando · comprobado a las {now:%H:%M}\n"
               f"Torneos con rating (hoy/mañana): {len(st.get('torneos', {}))}\n"
               f"Partidos próximos en BetsAPI: {st.get('n_proximos', 0)}\n"
               f"Hoy: mirados {d.get('mirados', 0)} · sin rating {d.get('sin_rating', 0)} · "
               f"sin cuota {d.get('sin_cuota', 0)} · avisos {d.get('avisos', 0)}"
               + (f"\n⚠️ Último error: {st['error']}" if st.get("error") else ""),
               reply_to=m.get("message_id"))
        elif low.startswith("resumen"):
            tg(texto_resumen(picks, now, "📊 RESUMEN RATING LIGA PRO"), reply_to=m.get("message_id"))
        elif rep in por_msg:
            tg("❓ Responde al aviso solo con la cuota, por ejemplo 1.85", reply_to=m.get("message_id"))


# ===================================================================== main
def main():
    if not TOKEN:
        raise SystemExit("Falta el secreto BETSAPI_TOKEN")
    mod = cargar_modelo()
    st = leer_estado()
    MK.TEXTOS = st.setdefault("textos", {})     # textos de los avisos (para cambiar su color)
    picks = leer_picks()
    if not st.get("arrancado"):
        st["arrancado"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        tg("🟢 Canal 🏅 RATING LIGA PRO activo\n"
           "Te aviso en la última hora antes de cada partido de Liga Pro cuando el rating "
           "oficial ve valor (≥3%) frente a la cuota de Bet365.\n"
           "↩️ Responde a cada aviso con tu cuota de BetCris · 'estado' · 'resumen'\n"
           "🧪 Seguimiento: sin dinero hasta confirmarlo")
    # arreglo (una vez): la cuota "al cierre" de los primeros avisos se tomo de
    # las cuotas en directo; se vuelve a calcular con la ultima de antes de empezar
    if not st.get("cierre_v2"):
        for p in picks:
            if p.get("resultado") in ("ganado", "perdido"):
                try:
                    cu = cuota_cierre(p["id"])
                except Exception:
                    cu = None
                local = _es_local(p)
                if cu and local is not None:
                    c = cu[0] if local else cu[1]
                    p["cuota_cierre"] = c
                    p["beneficio_cierre"] = round(c - 1, 3) if p["resultado"] == "ganado" else -1
        st["cierre_v2"] = True
    fin = time.time() + RUN_MIN * 60
    proximos, t_prox, t_web, t_res = [], 0, 0, 0
    while True:
        now = datetime.now(TZ)
        if st.get("diag", {}).get("fecha") != now.strftime("%Y-%m-%d"):
            st["diag"] = {"fecha": now.strftime("%Y-%m-%d")}
        try:
            if time.time() - t_web > 30 * 60 or not st.get("torneos"):
                torneos_web(st, now)
                t_web = time.time()
            if time.time() - t_prox > 5 * 60:
                dias = [now.strftime("%Y%m%d")]
                if now.hour >= 22:
                    dias.append((now + timedelta(days=1)).strftime("%Y%m%d"))
                proximos = [e for d in dias for e in paginas("/v3/events/upcoming", sport_id=92,
                                                              league_id=LIGA, day=d)]
                st["n_proximos"] = len(proximos)
                t_prox = time.time()
            nuevos_avisos(mod, proximos, picks, st, now)
            if time.time() - t_res > 10 * 60:
                resolver(picks, now)
                t_res = time.time()
            st.pop("error", None)
        except Exception as ex:
            print("ERROR", ex)
            st["error"] = f"{now:%H:%M} {str(ex)[:120]}"
        leer_mensajes(picks, st, now)
        if now.hour == 23 and now.minute >= 45 and st.get("resumen") != now.strftime("%Y-%m-%d"):
            resolver(picks, now)
            tg(texto_resumen(picks, now, f"🌙 RESUMEN RATING LIGA PRO · {now:%d/%m}"))
            st["resumen"] = now.strftime("%Y-%m-%d")
        guardar_picks(picks)
        guardar_estado(st)
        if time.time() > fin:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
