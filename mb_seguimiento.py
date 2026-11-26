"""
mb_seguimiento.py - Bot de seguimiento: REGLA EN CALIENTE en TT Cup y, en
prueba, dos señales de ELITE SERIES (favorito en caliente / Elo mejor que la casa).

Cada ejecucion (GitHub la lanza cada 5 minutos y dura ~14 minutos, con una
vuelta por minuto):
  1. Lee los partidos de TT Cup (historico en cache + hoy y manana en directo).
  2. Calcula Elo, forma, H2H y lo que lleva hoy cada jugador, con el mismo corte
     de 2 horas que el backtest validado.
  3. Para cada partido que empieza en las proximas ~2 horas, si un jugador ya ha
     jugado hoy (hace mas de 2 h) y el rival todavia no -> PICK.
     Avisa por Telegram con la cuota de BetCris a partir de la que hay que apostar:
       💰 APUESTA     valor 0-10%   (cuota ≥ 1/prob)        validado: yield ~+7%
       ⭐⭐ FUERTE     valor 10-30%  (cuota ≥ 1,10/prob)     validado: yield ~+26%
       ⚠️ SOSPECHOSO  valor >30%    (cuota ≥ 1,30/prob)     desconfiar
       ❌ NO          valor <0%
  4. Lee tus respuestas en Telegram con la cuota de BetCris (respondiendo al aviso
     o escribiendo "apellido cuota") y lo apunta todo con tu cuota real.
     "borrar apellido" quita la cuota apuntada de ese pick.
     "estado" -> te contesta si esta funcionando y cuantos avisos lleva hoy.
  5. Cuando el partido termina apunta el resultado. Si apuntaste cuota, te
     avisa del resultado con tu beneficio.
  6. Sobre las 23:45 manda el resumen del dia y el acumulado.

Todo queda en seguimiento/picks.csv, se apueste o no.

Variables: ZYLA_API_KEY, TELEGRAM_TOKEN, MY_CHAT_ID
           TRAIN_DAYS (120) · WARMUP_DAYS (45) · AVISO_MIN (125: avisa desde
           2 h 05 min antes) · TOURNAMENT (15005 = TT Cup)
"""
import csv
import json
import os
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import mb_api
import mb_marcas as MK
from mb_api import TZ, parse
import mb_bets

# Fuente de datos: "betsapi" (recomendada) o "zyla". Cada una con su propio
# historico en cache: los numeros de partido y de jugador no se mezclan nunca.
FUENTE = os.getenv("FUENTE_DATOS", "zyla").strip().lower()
SRC = mb_bets if FUENTE == "betsapi" else mb_api
day_events, odds_raw, odds_from = SRC.day_events, SRC.odds_raw, SRC.odds_from
HIST_SEG = int(os.getenv("HIST_SEG", "420"))       # segundos por vuelta para bajar historico
HIST_HILOS = int(os.getenv("HIST_HILOS", "4"))     # descargas a la vez (BetsAPI va lento)
from mb_backtest import (Estado, vector, entrenar, predecir, MATCH_LEN, CUTOFF,
                         MIN_PLAYED, MIN_ODD, MAX_ODD)

LIGAS_SEG = [x.strip() for x in os.getenv("LIGAS", "15005,19041,19039").split(",") if x.strip()]
TTCUP, ELITE = "15005", "19041"
LPRO = "19039"
NOMBRE = {TTCUP: "TT Cup", ELITE: "Elite Series", LPRO: "Liga Pro"}
ELO_DIF = 0.08            # Elite: el Elo ve al favorito ≥8 puntos mejor que la casa
ELITE_MAX = 2.00          # Elite 🔥 sin cuota en Sofascore: se apuesta si BetCris paga 1,25-2,00
                          # (en el historico: regla + cuota ≤2,00 → +9% en los meses de validacion)
REVISAR_CUOTA = 15 * 60   # Elite: si aun no hay cuota, se vuelve a mirar cada 15 min
TRAIN_DAYS = int(os.getenv("TRAIN_DAYS", "120"))
WARMUP_DAYS = int(os.getenv("WARMUP_DAYS", "45"))
AVISO = int(os.getenv("AVISO_MIN", "125")) * 60
VALOR_FUERTE = 0.10
VALOR_SOSPECHA = 0.30
PACE = float(os.getenv("PACE", "1.0"))
RUN_MIN = float(os.getenv("RUN_MIN", "14"))      # minutos que dura cada ejecucion
TG = os.getenv("TELEGRAM_TOKEN", "").strip()
# Canal propio de Elite Series: un bot de Telegram aparte. Sin el, Elite no funciona
# (asi el canal de TT Cup solo recibe TT Cup).
TG_ELITE = os.getenv("TELEGRAM_TOKEN_ELITE", "").strip()
# Canal de Liga Pro: el bot del antiguo Rating Liga Pro (sin el, Liga Pro no se usa)
TG_LP = os.getenv("TELEGRAM_TOKEN_LP", "").strip()
CHAT = os.getenv("MY_CHAT_ID", "").strip()

DIR = "seguimiento"
CACHE = os.path.join(DIR, "cache")
PICKS = os.path.join(DIR, "picks.csv")
ESTADO = os.path.join(DIR, "estado.json")
COLS = ["liga", "canal", "fecha", "hora", "nivel", "pick", "rival", "prob", "cuota_minima", "cuota_fuerte",
        "cuota_sospecha", "cuota_maxima", "cuota_2h", "valor_2h", "cuota_betcris", "valor_betcris",
        "decision_betcris", "cuota_final", "pick_jugados_hoy", "resultado", "marcador",
        "beneficio", "beneficio_betcris", "aviso", "msg", "estado_sofa", "id", "no_esta",
        "modo_ref", "cuota_ref", "fuente_ref", "decision_ref", "beneficio_ref",
        "regla", "cuota_apertura", "estrella", "forma_pick", "forma_rival"]

# TT Cup desde el 21/11: la decision la marca la CUOTA DEL BOT (la que da BetsAPI
# antes del partido). Si el bot no tiene cuota, vale la tuya. Verde = apostar,
# rojo = no apostar, blanco = sin cuota. El resumen cuenta desde esta fecha.
INICIO_REF = os.getenv("INICIO_REF", "2026-11-21")
REVISAR_REF = 10 * 60      # pick sin cuota del bot: se vuelve a mirar cada 10 min hasta que empiece
_REV_REF = {}
# TT Cup desde el 24/11: REGLA COMBINADA (12 meses reales con BetsAPI: +13% yield,
# peor caida -19 u). Solo se AVISA si cumple todo; el resto se apunta en silencio.
#   🟢 cuota del bot >= minimo  y  ya ha jugado 2 o mas hoy
#   o  cuota cayendo >= 3% desde que abrio  y  ya ha jugado 3 o mas hoy   (⭐)
INICIO_COMB = os.getenv("INICIO_COMB", "2026-11-24")
# Desde el 25/11: REGLA A en TT Cup y Elite (12 meses reales con BetsAPI, las dos ligas:
# +15% yield, peor caida -12 u). Se AVISA solo si cumple todo:
#   🔥 ya ha jugado 3 o mas hoy y el rival aun no
#   📈 llega en mejor forma que el rival (ultimos 10 partidos frente a lo esperado)
#   💰 la cuota del bot llega al minimo
# Partidos de 00:00 a 07:59: ni se avisan ni cuentan (estas durmiendo).
INICIO_A = os.getenv("INICIO_A", "2026-11-25")
NOCHE_HASTA = "08:00"
# Liga Pro (desde el 26/11): la regla A y ademas MAS MARGEN (12 meses reales: +19% yield):
#   la cuota paga al menos un 20% mas que el minimo  o  la cuota esta subiendo desde que abrio
VALOR_LP = 1.20
CAIDA = -0.03

NIVEL_TXT = {"SOSPECHOSO": "⚠️ SOSPECHOSO (valor >30%, desconfiar)",
             "FUERTE": "⭐⭐ FUERTE", "APUESTA": "💰 APUESTA",
             "FLOJO": "❌ NO APOSTAR", "SIN CUOTA": ""}


# ===================================================================== datos
def cache_dir(lid):
    if FUENTE == "betsapi":
        return os.path.join(DIR, f"cache_b{lid}")
    return CACHE if lid == TTCUP else os.path.join(DIR, f"cache_{lid}")


LLAMADAS_INICIO = [0]
LOCK = threading.RLock()    # picks/estado: los usan a la vez la vuelta de datos y el lector de mensajes
FALTAN = {}                 # liga -> dias del historico que faltan por bajar


def fetch_day(d, now, lid=TTCUP):
    """Partidos de un dia. Solo se guarda en cache cuando el dia ya ha acabado
    hace mas de 3 h (para no guardar un dia a medias)."""
    path = os.path.join(cache_dir(lid), f"{d}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    if FUENTE == "betsapi" and d < (now - timedelta(days=1)).strftime("%Y-%m-%d"):
        return None             # historico que aun no esta: lo baja precargar() poco a poco
    for intento in range(2 if FUENTE == "betsapi" else 4):
        try:
            evs = day_events(lid, d)
            break
        except Exception as ex:
            print("WARN dia", d, ex)
            time.sleep(5 * (intento + 1))
    else:
        return []
    fin_dia = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ) + timedelta(days=1, hours=3)
    if now >= fin_dia:
        os.makedirs(cache_dir(lid), exist_ok=True)
        with open(path, "w") as f:
            json.dump(evs, f)
        time.sleep(PACE)
    return evs


def precargar(now):
    """BetsAPI: baja los dias del historico que faltan, varios a la vez y como
    mucho HIST_SEG segundos por vuelta (asi avanza siempre, aunque BetsAPI vaya
    lento, y el bot sigue atendiendo tus mensajes el resto de la vuelta)."""
    if FUENTE != "betsapi":
        return
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    limite = time.time() + HIST_SEG
    hoy = now.date()
    for lid in LIGAS_SEG:
        liga = mb_bets.LIGAS.get(lid, lid)
        faltan = []
        d = hoy - timedelta(days=TRAIN_DAYS + WARMUP_DAYS)
        while d <= hoy - timedelta(days=2):
            if not os.path.exists(os.path.join(cache_dir(lid), f"{d:%Y-%m-%d}.json")):
                faltan.append(d)
            d += timedelta(days=1)
        if not faltan:
            continue
        faltan.reverse()                       # primero los mas recientes (los que mas pesan)
        utc = []
        for d in faltan:
            for k in (-1, 0):
                du = (d + timedelta(days=k)).strftime("%Y%m%d")
                if du not in utc and (liga, du) not in mb_bets._UTC:
                    utc.append(du)
        with ThreadPoolExecutor(HIST_HILOS) as ex:
            pend = set()
            for du in utc:
                while len(pend) >= HIST_HILOS:
                    _, pend = wait(pend, return_when=FIRST_COMPLETED)
                if time.time() > limite:
                    break
                pend.add(ex.submit(mb_bets._dia_utc, liga, du, False))
            wait(pend)
        hechos = 0
        for d in faltan:
            dus = [(d + timedelta(days=k)).strftime("%Y%m%d") for k in (-1, 0)]
            if all((liga, du) in mb_bets._UTC for du in dus):
                evs = mb_bets.day_events(lid, d.strftime("%Y-%m-%d"))   # ya en memoria: sin consultas
                os.makedirs(cache_dir(lid), exist_ok=True)
                with open(os.path.join(cache_dir(lid), f"{d:%Y-%m-%d}.json"), "w") as f:
                    json.dump(evs, f)
                hechos += 1
        print(f"historico {NOMBRE.get(lid, lid)}: {hechos} dias nuevos · faltaban {len(faltan)}")
        if time.time() > limite:
            break


def cargar(now, lid=TTCUP):
    hoy = now.date()
    evs = {}
    d = hoy - timedelta(days=TRAIN_DAYS + WARMUP_DAYS)
    faltan = 0
    while d <= hoy + timedelta(days=1):
        dia = fetch_day(d.strftime("%Y-%m-%d"), now, lid)
        if dia is None:
            faltan += 1
            dia = []
        for e in dia:
            evs[str(e.get("id"))] = e
        d += timedelta(days=1)
    FALTAN[lid] = faltan
    return evs


def cuotas(mid):
    try:
        return odds_from(odds_raw(mid))
    except Exception as ex:
        print("WARN cuotas", mid, ex)
        return None


# ============================================================ caracteristicas
def calcular(evs, now, incluir_todos=False):
    """Entrenamiento (partidos terminados) y candidatos (partidos por empezar)."""
    ms = [p for p in (parse(e) for e in evs.values()) if p]
    fin = sorted([m for m in ms if m["final"]], key=lambda x: x["t"] + MATCH_LEN)
    todos = sorted([m for m in ms if m["final"] or m["status"] == "notstarted"],
                   key=lambda x: (x["t"], x["id"]))
    train_desde = (now - timedelta(days=TRAIN_DAYS)).timestamp()
    ahora = now.timestamp()
    est = Estado()
    i = 0
    train, cands = [], []
    for m in todos:
        corte = m["t"] - CUTOFF
        while i < len(fin) and fin[i]["t"] + MATCH_LEN <= corte:
            est.aplicar(fin[i])
            i += 1
        es_cand = (not m["final"]) and m["t"] - AVISO <= ahora < m["t"] - 180
        if not (es_cand or (m["final"] and m["t"] >= train_desde)):
            continue
        f = est.feats(m)
        if f["n_a"] < MIN_PLAYED or f["n_b"] < MIN_PLAYED:
            continue
        f.update(id=m["id"], t=m["t"], a=m["a"], b=m["b"])
        if m["final"]:
            f["gana_a"] = int(m["final"][0] > m["final"][1])
            train.append(f)
        else:
            f["caliente"] = ("a" if f["hoy_j_a"] > 0 and f["hoy_j_b"] == 0 else
                             "b" if f["hoy_j_b"] > 0 and f["hoy_j_a"] == 0 else None)
            if f["caliente"] or incluir_todos:   # TT Cup: solo la regla · Elite: todos
                cands.append(f)
    return train, cands


# ================================================================== telegram
def tg(text, reply_to=None, token=None):
    token = token or TG
    if not (token and CHAT):
        print(text)
        return None
    params = {"chat_id": CHAT, "text": text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                    urllib.parse.urlencode(params).encode(), timeout=30) as r:
            return json.loads(r.read().decode()).get("result", {}).get("message_id")
    except Exception as ex:
        print("WARN telegram", ex)
        return None


def tg_canal(text, reply_to=None, token=None):
    return tg(text, reply_to, token=token)


def tg_updates(offset, token=None):
    """Mensajes nuevos que le has escrito a ese bot."""
    token = token or TG
    if not (token and CHAT):
        return []
    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=0&offset={offset}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode()).get("result", [])
    except Exception as ex:
        print("WARN getUpdates", ex)
        return []


# =================================================================== estado
def leer_picks():
    if not os.path.exists(PICKS):
        return []
    with open(PICKS, newline="") as f:
        return list(csv.DictReader(f))


def guardar_picks(rows):
    os.makedirs(DIR, exist_ok=True)
    tmp = PICKS + ".tmp"                     # se escribe aparte y se cambia de golpe:
    with open(tmp, "w", newline="") as f:     # si GitHub corta a medias, el csv no se rompe
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLS})
    os.replace(tmp, PICKS)


def leer_estado():
    if os.path.exists(ESTADO):
        with open(ESTADO) as f:
            return json.load(f)
    return {}


def guardar_estado(st):
    os.makedirs(DIR, exist_ok=True)
    tmp = ESTADO + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, ESTADO)


# =================================================================== niveles
def nivel_valor(valor):
    if valor is None:
        return "SIN CUOTA"
    if valor > VALOR_SOSPECHA:
        return "SOSPECHOSO"
    if valor >= VALOR_FUERTE:
        return "FUERTE"
    return "APUESTA" if valor >= 0 else "FLOJO"


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def nivel_de(r):
    """Nivel con la cuota de Sofascore de 2 h antes."""
    return nivel_valor(num(r.get("valor_2h")))


def limpio(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", s)


# ==================================================================== pasos
def texto_comb(pk):
    """Aviso de un pick que cumple TODO (regla combinada)."""
    c, ca = num(pk.get("cuota_2h")), num(pk.get("cuota_apertura"))
    caida = f"📉 abrió a {ca:.2f} · ha caído {(1 - c / ca) * 100:.0f}%\n" if ca and c and c < ca else ""
    if pk.get("regla") in ("A", "LP"):
        caida = "📈 llega en mejor forma que el rival\n"
    if pk.get("regla") == "LP":
        pr = num(pk.get("prob")) or 0
        caida += (f"💎 paga un {(pr * c - 1) * 100:.0f}% por encima de su valor\n" if pr * c >= VALOR_LP
                  else f"⬆️ la cuota sube: abrió a {ca:.2f}\n")
    liga = ("🇵🇱 PICK ELITE SERIES" if es_elite(pk) else "🇨🇿 PICK LIGA PRO" if es_lp(pk)
            else "🔥 PICK TT CUP")
    return (f"{'⭐ ' if pk.get('estrella') else ''}{liga} · {pk['hora']}\n"
            f"✅ {pk['pick'].upper()}\n"
            f"vs {pk['rival']}\n"
            f"\n"
            f"💰 Cuota del bot {c:.2f} · mínimo {float(pk['cuota_minima']):.2f}\n" + caida +
            f"🔥 ya ha jugado {pk['pick_jugados_hoy']} hoy, rival aún no\n"
            f"\n"
            f"↩️ Responde con tu cuota de BetCris para tu yield")


def avisar_comb(pk):
    """Manda el aviso solo si cumple todo y aun no se ha mandado."""
    if pk.get("decision_ref") == "SI" and not pk.get("msg"):
        pk["msg"] = MK.enviar(token_de(pk), CHAT, pk.get("canal") or "tt", texto_comb(pk), pk) or ""


def nuevos_picks(cands, mod, picks, st, now, elite=False, lp=False):
    descartados = st.setdefault("descartados", [])
    ya = {p["id"] for p in picks} | set(descartados)
    for f in sorted(cands, key=lambda x: x["t"]):
        if f["id"] in ya:
            continue
        lado = f["caliente"]
        if lado not in ("a", "b"):
            continue                       # seguridad: en TT Cup solo picks de la regla
        p = float(predecir(mod, [vector(f)])[0])
        if lado == "b":
            p = 1 - p
        o = cuotas(f["id"])
        time.sleep(PACE)
        c = ca = None
        if o:
            c = o[1] if lado == "a" else o[3]
            ca = o[0] if lado == "a" else o[2]
        c_apuesta = max(MIN_ODD, round(1 / p, 2))                          # valor 0%
        c_fuerte = max(MIN_ODD, round((1 + VALOR_FUERTE) / p, 2))          # valor 10%
        c_sospecha = round((1 + VALOR_SOSPECHA) / p, 2)                    # valor 30%
        if (c and not (MIN_ODD <= c <= MAX_ODD)) or c_apuesta > MAX_ODD:
            descartados.append(f["id"])    # fuera de lo validado (cuotas 1,25-4,00)
            del descartados[:-500]
            continue
        valor = (p * c - 1) if c else None
        nv = nivel_valor(valor)
        pick, rival = (f["a"], f["b"]) if lado == "a" else (f["b"], f["a"])
        jh = f["hoy_j_a"] if lado == "a" else f["hoy_j_b"]
        dt = datetime.fromtimestamp(f["t"], TZ)
        txt = (f"🔥 PICK TT CUP · {dt:%H:%M}\n"
               f"✅ {pick.upper()}\n"
               f"vs {rival}\n"
               f"\n"
               f"💰 Se apuesta con cuota ≥ {c_apuesta:.2f}\n"
               f"🟢 la cuota llega · 🔴 no llega · ⚪ sin cuota\n"
               f"↩️ Si sale ⚪, responde con tu cuota de BetCris\n"
               f"\n"
               f"Prob. {p * 100:.0f}% · ya ha jugado {jh} hoy, rival aún no\n"
               f"🧪 Seguimiento")
        pk = dict(liga="Elite Series" if elite else "Czech Liga Pro" if lp else "TT Cup",
                  canal="elite" if elite else "lp" if lp else "tt",
                  fecha=dt.strftime("%Y-%m-%d"), hora=dt.strftime("%H:%M"),
                  nivel=nv, pick=pick, rival=rival, prob=round(p, 3),
                  cuota_minima=c_apuesta, cuota_fuerte=c_fuerte,
                  cuota_sospecha=c_sospecha,
                  cuota_2h=c or "", valor_2h=round(valor, 3) if valor is not None else "",
                  pick_jugados_hoy=jh, aviso=now.strftime("%Y-%m-%d %H:%M"),
                  resultado="pendiente", msg="", id=f["id"])
        if elite or lp or pk["fecha"] >= INICIO_A:
            pk["regla"], pk["cuota_apertura"] = ("LP" if lp else "A"), ca or ""
            pk["forma_pick"] = round(f["forma_a"] if lado == "a" else f["forma_b"], 3)
            pk["forma_rival"] = round(f["forma_b"] if lado == "a" else f["forma_a"], 3)
            calc_ref(pk)
            avisar_comb(pk)                     # solo si cumple todo; si no, en silencio
            picks.append(pk)
            ya.add(f["id"])
            continue
        if pk["fecha"] >= INICIO_COMB:
            pk["regla"], pk["cuota_apertura"] = "COMB", ca or ""
            calc_ref(pk)
            avisar_comb(pk)                     # solo si cumple todo; si no, en silencio
            picks.append(pk)
            ya.add(f["id"])
            continue
        calc_ref(pk)
        pk["msg"] = MK.enviar(TG, CHAT, "tt", txt, pk) or ""
        picks.append(pk)
        ya.add(f["id"])


def nuevos_picks_elite(cands, picks, st, now):
    """Elite Series (seguimiento): dos señales debiles del historico (+3-5%):
       🔥 A: el favorito ya ha jugado hoy y el rival todavia no
       📈 B: el favorito, y el Elo le ve ≥8 puntos mejor de lo que dice la cuota
       Se apuesta si BetCris paga al menos lo que pagaba Sofascore."""
    revisados = st.setdefault("elite_revisados", {})
    hoy = now.strftime("%Y-%m-%d")
    diag = st.setdefault("elite_diag", {})
    if diag.get("fecha") != hoy:
        diag.clear()
        diag.update(fecha=hoy, revisados=0, con_cuota=0, favoritos=0, con_senal=0)
    diag["candidatos_ahora"] = len(cands)
    diag["ultima"] = now.strftime("%H:%M")
    ya = {p["id"] for p in picks}
    ahora = time.time()
    for f in sorted(cands, key=lambda x: x["t"]):
        if f["id"] in ya or ahora - revisados.get(f["id"], 0) < REVISAR_CUOTA:
            continue
        revisados[f["id"]] = ahora
        diag["revisados"] += 1
        o = cuotas(f["id"])
        time.sleep(PACE)
        if not o:
            # Sin cuota en Sofascore: solo la señal 🔥, si el Elo le ve favorito.
            # Decides tu con la cuota de BetCris (1,25 - 2,00).
            lado = f["caliente"]
            if lado in ("a", "b"):
                pe = f["p_elo"] if lado == "a" else 1 - f["p_elo"]
                if pe >= 0.5:
                    diag["sin_cuota_caliente"] = diag.get("sin_cuota_caliente", 0) + 1
                    aviso_elite(f, lado, None, None, pe, True, False, picks, now)
                    ya.add(f["id"])
            continue
        diag["con_cuota"] += 1
        ca, cb = o[1], o[3]
        for lado, c, co in (("a", ca, cb), ("b", cb, ca)):
            if not (MIN_ODD <= c <= MAX_ODD) or c >= co:
                continue                                  # solo favoritos
            diag["favoritos"] += 1
            pm = (1 / c) / (1 / c + 1 / co)
            pe = f["p_elo"] if lado == "a" else 1 - f["p_elo"]
            sen_a = f["caliente"] == lado
            sen_b = pe - pm >= ELO_DIF
            if not (sen_a or sen_b):
                continue
            diag["con_senal"] += 1
            aviso_elite(f, lado, c, pm, pe, sen_a, sen_b, picks, now)
            ya.add(f["id"])
            break
    for k in [k for k, v in revisados.items() if ahora - v > 86400]:
        del revisados[k]


def aviso_elite(f, lado, c, pm, pe, sen_a, sen_b, picks, now):
    """Manda y apunta un aviso de Elite. c = cuota de Sofascore (None si no hay)."""
    pick, rival = (f["a"], f["b"]) if lado == "a" else (f["b"], f["a"])
    jh = f["hoy_j_a"] if lado == "a" else f["hoy_j_b"]
    dt = datetime.fromtimestamp(f["t"], TZ)
    senales = []
    if sen_a:
        senales.append(f"🔥 ya ha jugado {jh} hoy, rival aún no")
    if sen_b:
        senales.append(f"📈 el Elo le ve {(pe - pm) * 100:.0f} puntos mejor que la casa")
    if c:
        minima, maxima = c, None
        regla = (f"💰 APUESTA si BetCris ≥ {c:.2f}\n"
                 f"❌ menos de {c:.2f} → no apostar\n")
        sofa = f"Sofascore: {c:.2f} (favorito)"
    else:
        minima, maxima = MIN_ODD, ELITE_MAX
        regla = (f"💰 APUESTA si BetCris está entre {MIN_ODD:.2f} y {ELITE_MAX:.2f}\n"
                 f"❌ más de {ELITE_MAX:.2f} (no es favorito) → no apostar\n")
        sofa = f"Sofascore: sin cuota · Elo le da {pe * 100:.0f}%"
    txt = (f"🇵🇱 PICK ELITE SERIES · {dt:%H:%M}\n"
           f"✅ {pick.upper()}\n"
           f"vs {rival}\n"
           f"\n" + regla + "\n"
           + "\n".join(senales) + "\n"
           + sofa + "\n"
           "↩️ Responde con tu cuota de BetCris para apuntarla\n"
           "🧪 Seguimiento · sin dinero hasta confirmarlo")
    pk = dict(liga="Elite Series", canal="elite", fecha=dt.strftime("%Y-%m-%d"),
              hora=dt.strftime("%H:%M"),
              nivel="A+B" if sen_a and sen_b else ("A" if sen_a else "B"),
              pick=pick, rival=rival, prob=round(pm if pm else pe, 3),
              cuota_minima=minima, cuota_maxima=maxima or "",
              cuota_2h=c or "", valor_2h=0 if c else "", pick_jugados_hoy=jh,
              aviso=now.strftime("%Y-%m-%d %H:%M"), resultado="pendiente",
              msg="", id=f["id"])
    pk["msg"] = MK.enviar(TG_ELITE, CHAT, "elite", txt, pk) or ""
    picks.append(pk)


def es_elite(p):
    return p.get("liga") == "Elite Series"


def token_de(p):
    """Bot por el que se mando el aviso de ese pick (y por el que se contesta)."""
    if p.get("canal") == "lp" and TG_LP:
        return TG_LP
    return TG_ELITE if p.get("canal") == "elite" and TG_ELITE else TG


def en_ref(p):
    return es_comb(p) or (not es_elite(p) and p.get("fecha", "") >= INICIO_REF)


def es_comb(p):
    return p.get("regla") in ("COMB", "A", "LP")


def es_lp(p):
    return p.get("liga") == "Czech Liga Pro"


def evaluar_comb(p):
    """SI / NO / NOCHE / '' (sin cuota del bot) y la ⭐, con la cuota del bot del momento del aviso."""
    c, ca, cm = num(p.get("cuota_2h")), num(p.get("cuota_apertura")), num(p.get("cuota_minima"))
    jh = int(num(p.get("pick_jugados_hoy")) or 0)
    if p.get("regla") in ("A", "LP"):
        if p.get("hora", "") < NOCHE_HASTA:
            return "NOCHE", ""                       # durmiendo: ni se avisa ni cuenta
        if not c or not cm:
            return "", ""
        if not (MIN_ODD <= c <= MAX_ODD):
            return "NO", ""
        fp, fr = num(p.get("forma_pick")), num(p.get("forma_rival"))
        pasa = c >= cm and jh >= 3 and fp is not None and fr is not None and fp > fr
        if pasa and p.get("regla") == "LP":          # Liga Pro: hace falta mas margen
            pr = num(p.get("prob")) or 0
            pasa = pr * c >= VALOR_LP or (bool(ca) and c > ca)
        return ("SI" if pasa else "NO"), ""
    if not c or not cm:
        return "", ""
    if not (MIN_ODD <= c <= MAX_ODD):
        return "NO", ""
    cae = bool(ca) and c / ca - 1 <= CAIDA
    estrella = cae and jh >= 3
    pasa = (c >= cm and jh >= 2) or estrella
    return ("SI" if pasa else "NO"), ("si" if estrella else "")


def calc_ref(p):
    """TT Cup (sistema nuevo): cuota de referencia = la del bot; si no hay, la tuya.
    🟢 SI si llega a la cuota minima, 🔴 NO si no llega, ⚪ sin cuota."""
    if es_comb(p):
        p["modo_ref"] = "si"
        if not p.get("decision_ref"):                   # se decide una vez, al avisar
            p["decision_ref"], p["estrella"] = evaluar_comb(p)
        c = num(p.get("cuota_2h"))
        p["cuota_ref"], p["fuente_ref"] = (c, "bot") if c else ("", "")
        res = p.get("resultado")
        p["beneficio_ref"] = ("" if not c or p.get("decision_ref") not in ("SI", "NO") else
                              round(c - 1, 2) if res == "ganado" else -1 if res == "perdido"
                              else 0 if res == "anulado" else "")
        return
    if not en_ref(p):
        return
    p["modo_ref"] = "si"
    c, fuente = num(p.get("cuota_2h")), "bot"
    if not c:
        c, fuente = num(p.get("cuota_betcris")), "tuya"
    cm = num(p.get("cuota_minima"))
    if not c or not cm:
        for k in ("cuota_ref", "fuente_ref", "decision_ref", "beneficio_ref"):
            p[k] = ""
        return
    p["cuota_ref"], p["fuente_ref"] = c, fuente
    p["decision_ref"] = "SI" if c >= cm else "NO"
    res = p.get("resultado")
    p["beneficio_ref"] = (round(c - 1, 2) if res == "ganado" else -1 if res == "perdido"
                          else 0 if res == "anulado" else "")


def revisar_ref(picks, evs, now):
    """Picks del sistema nuevo: si el bot aun no tenia cuota, la vuelve a buscar
    (solo antes de que empiece el partido) y actualiza el color del aviso."""
    for p in picks:
        if not en_ref(p):
            continue
        antes = MK.cabecera(p) if p.get("modo_ref") else ""
        if (p.get("resultado") == "pendiente" and not num(p.get("cuota_2h"))
                and time.time() - _REV_REF.get(p["id"], 0) > REVISAR_REF):
            try:
                inicio = datetime.strptime(f"{p['fecha']} {p['hora']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
            except ValueError:
                inicio = now
            e = evs.get(p["id"])
            if e and now < inicio:
                _REV_REF[p["id"]] = time.time()
                o = cuotas(p["id"])
                time.sleep(PACE)
                if o:
                    local = (e.get("homeTeam") or {}).get("name") == p["pick"]
                    c = o[1] if local else o[3]
                    if c and num(p.get("prob")):
                        p["cuota_2h"] = c
                        p["valor_2h"] = round(float(p["prob"]) * c - 1, 3)
                        if es_comb(p):
                            p["cuota_apertura"] = o[0] if local else o[2]
        calc_ref(p)
        if es_comb(p):
            avisar_comb(p)                      # si ahora cumple todo, se avisa ya
            continue
        if MK.cabecera(p) != antes:
            MK.actualizar(TG, CHAT, "tt", p)


def calc_betcris(p):
    """Decision y beneficio con la cuota de BetCris apuntada."""
    calc_ref(p)
    cb = num(p.get("cuota_betcris"))
    pr = num(p.get("prob"))
    if not cb or not pr:
        return
    if es_elite(p):
        cm = num(p.get("cuota_minima")) or MIN_ODD
        cx = num(p.get("cuota_maxima")) or 99
        v = cb / cm - 1
        p["valor_betcris"] = round(v, 3)
        p["decision_betcris"] = "APUESTA" if cm <= cb <= cx else "FLOJO"
    else:
        v = pr * cb - 1
        p["valor_betcris"] = round(v, 3)
        p["decision_betcris"] = nivel_valor(v)
    if p.get("resultado") == "ganado":
        p["beneficio_betcris"] = round(cb - 1, 2)
    elif p.get("resultado") == "perdido":
        p["beneficio_betcris"] = -1
    elif p.get("resultado") == "anulado":
        p["beneficio_betcris"] = 0


def texto_resultado(p):
    ic = {"ganado": "✅ GANADO", "perdido": "❌ PERDIDO", "anulado": "➖ ANULADO"}[p["resultado"]]
    t = f"🏁 {p['pick'].upper()} vs {p['rival']} · {ic} {p.get('marcador', '')}"
    if es_comb(p):
        if p.get("decision_ref") == "SI":
            b = num(p.get("beneficio_ref")) or 0
            t += f"\nCuota del bot {float(p['cuota_ref']):.2f} · {b:+.2f} u"
            if p.get("cuota_betcris"):
                bb = num(p.get("beneficio_betcris")) or 0
                t += f"\nTu cuota {float(p['cuota_betcris']):.2f} · {bb:+.2f} u"
        return t
    if p.get("modo_ref") == "si":
        if p.get("decision_ref"):
            b = num(p.get("beneficio_ref")) or 0
            t += (f"\n{'🟢 APOSTAR' if p['decision_ref'] == 'SI' else '🔴 NO APOSTAR'} · cuota "
                  f"{float(p['cuota_ref']):.2f} ({p['fuente_ref']}) · {b:+.2f} u")
        return t
    if p.get("cuota_betcris"):
        b = num(p.get("beneficio_betcris")) or 0
        t += (f"\nBetCris {float(p['cuota_betcris']):.2f} · {NIVEL_TXT[p['decision_betcris']]}"
              f" · {b:+.2f} u")
    return t


def buscar_pick(picks, por_msg, m, texto):
    """El pick al que se refiere tu mensaje: el aviso al que respondes o el
    apellido que escribes (el mas reciente, mejor si aun esta pendiente)."""
    rep = str((m.get("reply_to_message") or {}).get("message_id", ""))
    if por_msg.get(rep):
        return por_msg[rep]
    palabras = [w for w in limpio(texto).split()
                if len(w) >= 3 and w not in ("borrar", "borra", "quitar", "quita")]
    cand = [p for p in picks if any(w in limpio(p["pick"]).split() for w in palabras)
            and not (es_comb(p) and not p.get("msg"))]       # los no avisados no cuentan
    pend = [p for p in cand if p.get("resultado") == "pendiente"]
    cand = pend or cand
    return max(cand, key=lambda p: (p["fecha"], p["hora"])) if cand else None


def leer_respuestas(picks, st):
    """Lee los mensajes de cada canal: TT Cup (bot principal) y Elite (su bot)."""
    leer_canal(picks, st, "tt", TG, "offset")
    if TG_ELITE:
        leer_canal(picks, st, "elite", TG_ELITE, "offset_elite")
    if TG_LP and LPRO in LIGAS_SEG:
        leer_canal(picks, st, "lp", TG_LP, "offset_lp")


def leer_canal(todos_picks, st, canal, token, clave_offset):
    """Lo que le escribes al bot:
       - responder al aviso con la cuota ("1,84") o "apellido cuota" ("precek 1,84")
         -> apunta tu cuota de BetCris
       - "borrar apellido" (o responder al aviso con "borrar")
         -> quita tu cuota de ese pick (el aviso sigue en el registro general)"""
    picks = [p for p in todos_picks if (p.get("canal") or "tt") == canal]

    def tg(text, reply_to=None):                       # contesta por el mismo canal
        return tg_canal(text, reply_to, token=token)
    upd = tg_updates(st.get(clave_offset, 0), token=token)
    por_msg = {str(p.get("msg")): p for p in picks if p.get("msg")}
    for u in upd:
        st[clave_offset] = u["update_id"] + 1
        b = MK.boton(u, picks, CHAT)
        if b:                                          # boton [🚫 No está el partido]
            MK.aplicar_boton(token, CHAT, canal, *b)
            continue
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != CHAT:
            continue
        texto = (m.get("text") or "").strip()
        mid = m.get("message_id")
        if limpio(texto).strip() in ("estado", "status", "vivo"):
            ahora = datetime.now(TZ)
            hoy = ahora.strftime("%Y-%m-%d")
            del_dia = [p for p in picks if p["fecha"] == hoy]
            pend = sum(1 for p in del_dia if p.get("resultado") == "pendiente")
            ult = max((p.get("aviso", "") for p in picks), default="")
            extra = ""
            if canal == "elite" and False:      # (diagnostico de las señales antiguas A/B)
                d = st.get("elite_diag", {})
                extra = (f"Partidos de Elite revisados hoy: {d.get('revisados', 0)} · con cuota en "
                         f"Sofascore: {d.get('con_cuota', 0)} · favoritos: {d.get('favoritos', 0)} · "
                         f"con señal: {d.get('con_senal', 0)} · avisos 🔥 sin cuota: "
                         f"{d.get('sin_cuota_caliente', 0)}\n"
                         f"Ahora mismo en la ventana de 2 h: {d.get('candidatos_ahora', 0)} partidos "
                         f"(última revisión {d.get('ultima', '—')})\n")
                if st.get("error_19041"):
                    extra += f"⚠️ Último error: {st['error_19041']}\n"
            hist = st.get(f"historico_{ELITE if canal == 'elite' else LPRO if canal == 'lp' else TTCUP}")
            if hist:
                extra += f"⏳ Cargando historial de BetsAPI: {hist} días (sin avisos hasta terminar)\n"
            if canal == "lp" and st.get(f"error_{LPRO}"):
                extra += f"⚠️ Último error: {st[f'error_{LPRO}']}\n"
            if canal == "tt" and st.get(f"error_{TTCUP}"):
                extra += f"⚠️ Último error: {st[f'error_{TTCUP}']}\n"
            extra += f"Fuente de datos: {'BetsAPI' if FUENTE == 'betsapi' else 'Zyla'}\n"
            tg(f"✅ Funcionando · {'ELITE SERIES' if canal == 'elite' else 'LIGA PRO' if canal == 'lp' else 'TT CUP'} · "
               f"comprobado a las {ahora:%H:%M}\n"
               f"Hoy: {len(del_dia)} avisos · {pend} pendientes de resultado\n" + extra +
               f"Último aviso: {ult[11:] if ult.startswith(hoy) else ult or '—'}\n"
               f"Si no llegan avisos es porque ahora no hay partidos que cumplan la regla.", mid)
            continue
        if limpio(texto).strip() in ("pendientes", "pendiente", "faltan"):
            ahora = datetime.now(TZ)
            tg(MK.texto_pendientes(picks, ahora.strftime("%Y-%m-%d"),
                                   (ahora - timedelta(days=1)).strftime("%Y-%m-%d")), mid)
            continue
        orden, resto = MK.orden(texto)
        if orden:                                     # "no" (no está en BetCris) / "si" (deshacer)
            pk = buscar_pick(picks, por_msg, m, " ".join(resto)) if (
                str((m.get("reply_to_message") or {}).get("message_id", "")) in por_msg or resto) else None
            if not pk:
                tg("❓ Responde al aviso con «no» (no está en BetCris) o «si» (deshacer), "
                   "o escribe: no apellido", mid)
                continue
            if orden == "no":
                pk["no_esta"] = "si"
                for k in ("cuota_betcris", "valor_betcris", "decision_betcris", "beneficio_betcris"):
                    pk[k] = ""
                calc_ref(pk)
                MK.actualizar(token, CHAT, canal, pk)
                if pk.get("fuente_ref") == "bot":
                    tg(f"{MK.color(pk)} Apuntado: {pk['pick'].upper()} ({pk['hora']}) no está en BetCris.\n"
                       "Para el registro sigue contando con la cuota del bot.", mid)
                else:
                    tg(f"⚫⚫ Apuntado: {pk['pick'].upper()} ({pk['hora']}) no está en BetCris · no cuenta.\n"
                       "Si aparece, responde al aviso con la cuota (o «si» para dejarlo pendiente).", mid)
            else:
                pk["no_esta"] = ""
                calc_ref(pk)
                MK.actualizar(token, CHAT, canal, pk)
                tg(f"⚪⚪ {pk['pick'].upper()} ({pk['hora']}) vuelve a pendiente: responde con la cuota.", mid)
            continue
        if limpio(texto).split()[:1] in (["borrar"], ["borra"], ["quitar"], ["quita"]):
            pk = buscar_pick(picks, por_msg, m, texto)
            if not pk or not pk.get("cuota_betcris"):
                tg("❓ No encuentro ningún pick con cuota apuntada con ese nombre. "
                   "Escribe: borrar apellido", mid)
                continue
            for k in ("cuota_betcris", "valor_betcris", "decision_betcris", "beneficio_betcris"):
                pk[k] = ""
            calc_ref(pk)
            MK.actualizar(token, CHAT, canal, pk)          # vuelve a ⚪
            tg(f"🗑️ Borrada tu cuota de {pk['pick'].upper()} ({pk['hora']}). "
               + ("Ahora: " + MK.color(pk) + (" (cuenta la cuota del bot)" if pk.get("fuente_ref") == "bot"
                                              else " sin cuota") if pk.get("modo_ref") == "si"
                  else "Ya no cuenta en tus estadísticas de BetCris."), mid)
            continue
        nums = re.findall(r"\d+[.,]\d+|\d+", texto)
        if not nums:
            continue
        # la cuota es el primer numero con pinta de cuota ("1,85 120" -> 1,85)
        validos = [float(x.replace(",", ".")) for x in nums if 1.01 <= float(x.replace(",", ".")) <= 50]
        cb = validos[0] if validos else float(nums[-1].replace(",", "."))
        if not (1.01 <= cb <= 50):
            tg("❓ No entiendo la cuota. Escríbela así: 1,84", mid)
            continue
        pk = buscar_pick(picks, por_msg, m, texto)
        if not pk:
            tg("❓ No encuentro ese pick. Responde al aviso con la cuota, "
               "o escribe: apellido cuota (ej. precek 1,84)", mid)
            continue
        pk["cuota_betcris"] = cb
        pk["no_esta"] = ""
        calc_betcris(pk)
        calc_ref(pk)
        MK.actualizar(token, CHAT, canal, pk)              # 🟢 / 🟡 / 🔴
        if es_elite(pk) and not es_comb(pk):
            rango = (f"entre {float(pk['cuota_minima']):.2f} y {float(pk['cuota_maxima']):.2f}"
                     if pk.get("cuota_maxima") else f"mínima {float(pk['cuota_minima']):.2f}")
            t = (f"{MK.color(pk)} Apuntado {pk['pick'].upper()} (Elite {pk['hora']}) · BetCris {cb:.2f}\n"
                 f"Hay que apostar {rango} → {NIVEL_TXT[pk['decision_betcris']]}")
        elif es_comb(pk):
            cm = float(pk["cuota_minima"])
            t = (f"{MK.color(pk)} Apuntada tu cuota {cb:.2f} de {pk['pick'].upper()} ({pk['hora']}).\n"
                 + ("Cuenta para tu yield del día." if cb >= cm or pk.get("estrella") else
                    f"⚠️ Ojo: tu cuota no llega al mínimo ({cm:.2f}). Queda apuntada igual."))
        elif pk.get("modo_ref") == "si":
            dec = "🟢 APOSTAR" if pk.get("decision_ref") == "SI" else "🔴 NO APOSTAR"
            if pk.get("fuente_ref") == "bot":
                t = (f"{MK.color(pk)} Apuntada tu cuota {cb:.2f} de {pk['pick'].upper()} ({pk['hora']}).\n"
                     f"Para el registro cuenta la cuota del bot: {float(pk['cuota_ref']):.2f} → {dec}")
            else:
                t = (f"{MK.color(pk)} Apuntado {pk['pick'].upper()} ({pk['hora']}) · tu cuota {cb:.2f}\n"
                     f"Mínimo {float(pk['cuota_minima']):.2f} → {dec}")
        else:
            t = (f"{MK.color(pk)} Apuntado {pk['pick'].upper()} ({pk['hora']}) · BetCris {cb:.2f}\n"
                 f"Valor {float(pk['valor_betcris']) * 100:+.0f}% → {NIVEL_TXT[pk['decision_betcris']]}")
        if pk.get("resultado") in ("ganado", "perdido", "anulado"):
            t += "\n" + texto_resultado(pk)
        tg(t, mid)


def _apellido(n):
    w = limpio(n).split()
    if len(w) >= 2 and len(w[-1]) == 1:
        w = w[:-1]
    return w[-1] if len(w) >= 2 and len(w[0]) > 1 and n and not n.strip().endswith(".") else (w[0] if w else "")


def por_nombres(p, evs):
    """Pick apuntado con datos de Zyla: busca el mismo partido en los de BetsAPI
    (mismos apellidos y misma hora, ±30 min)."""
    if str(p.get("id", "")).startswith("b"):
        return None
    try:
        t = datetime.strptime(f"{p['fecha']} {p['hora']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp()
    except ValueError:
        return None
    quiero = {_apellido(p["pick"]), _apellido(p["rival"])}
    for e in evs.values():
        if abs(int(e.get("startTimestamp") or 0) - t) > 1800:
            continue
        tiene = {_apellido((e.get("homeTeam") or {}).get("name", "")),
                 _apellido((e.get("awayTeam") or {}).get("name", ""))}
        if quiero == tiene:
            # mismo partido: el pick pasa a llevar el nombre que usa BetsAPI
            casa = (e.get("homeTeam") or {}).get("name", "")
            fuera = (e.get("awayTeam") or {}).get("name", "")
            p["pick"], p["rival"] = (casa, fuera) if _apellido(casa) == _apellido(p["pick"]) else (fuera, casa)
            return e
    return None


def resolver(picks, evs, now):
    """Apunta resultados. Un partido solo se da por ANULADO si Sofascore lo marca
    cancelado o si 6 h despues de su hora sigue sin un resultado valido (justo al
    acabar, Sofascore a veces da el partido por terminado sin el marcador
    completo). Los anulados se vuelven a revisar: si luego aparece el resultado,
    se corrigen. Devuelve cuantos anulados se han corregido."""
    corregidos = 0
    for p in picks:
        res = p.get("resultado")
        if res not in ("pendiente", "anulado"):
            continue
        e = evs.get(p["id"]) or (por_nombres(p, evs) if FUENTE == "betsapi" else None)
        if not e:
            continue
        st = (e.get("status") or {}).get("type", "")
        p["estado_sofa"] = st
        m = parse(e) if st == "finished" else None
        if not m or not m["final"]:
            if res == "pendiente":
                try:
                    inicio = datetime.strptime(f"{p['fecha']} {p['hora']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
                except ValueError:
                    inicio = now
                if st in ("canceled", "cancelled") or (now - inicio).total_seconds() > 6 * 3600:
                    p["resultado"], p["beneficio"] = "anulado", 0
                    calc_betcris(p)
            continue
        gana_local = m["final"][0] > m["final"][1]
        pick_local = (e.get("homeTeam") or {}).get("name") == p["pick"]
        gana = gana_local == pick_local
        p["marcador"] = (f"{m['final'][0]}-{m['final'][1]}" if pick_local
                         else f"{m['final'][1]}-{m['final'][0]}")
        o = cuotas(p["id"])
        time.sleep(PACE)
        if o:
            p["cuota_final"] = o[1] if pick_local else o[3]
        c = p.get("cuota_2h") or p.get("cuota_final")
        p["resultado"] = "ganado" if gana else "perdido"
        p["beneficio"] = round(float(c) - 1, 2) if (gana and c) else (-1 if c else "")
        calc_betcris(p)
        if res == "anulado":
            corregidos += 1                      # corregido en silencio
        elif p.get("cuota_betcris") or p.get("decision_ref") == "SI":   # apuntados o 🟢
            tg(texto_resultado(p), p.get("msg") or None, token=token_de(p))
    return corregidos


def resumen(rows, campo="beneficio"):
    hechos = [r for r in rows if r.get("resultado") in ("ganado", "perdido")
              and num(r.get(campo)) is not None]
    if not hechos:
        return "0 picks"
    n = len(hechos)
    g = sum(1 for r in hechos if r["resultado"] == "ganado")
    ben = sum(float(r[campo]) for r in hechos)
    return f"{n} picks · {g}✅ {n - g}❌ · {ben:+.2f} u · yield {ben / n * 100:+.1f}%"


def bloque(rows):
    con_bc = [r for r in rows if r.get("decision_betcris")]

    def bc(niveles):
        return resumen([r for r in con_bc if r["decision_betcris"] in niveles], "beneficio_betcris")

    def so(niveles):
        return resumen([r for r in rows if nivel_de(r) in niveles])
    return (f"🎯 Con tus cuotas BetCris ({len(con_bc)} apuntados):\n"
            f"  ⭐⭐ Fuertes: {bc(('FUERTE',))}\n"
            f"  💰 Apuestas + fuertes: {bc(('FUERTE', 'APUESTA'))}\n"
            f"  ⚠️ Sospechosos: {bc(('SOSPECHOSO',))}\n"
            f"  ❌ No apostar: {bc(('FLOJO',))}\n"
            f"📊 Todos los avisos, cuota Sofascore:\n"
            f"  ⭐⭐ Fuertes: {so(('FUERTE',))}\n"
            f"  💰 Apuestas + fuertes: {so(('FUERTE', 'APUESTA'))}\n"
            f"  ⚠️ Sospechosos: {so(('SOSPECHOSO',))}\n"
            f"  ❌ No apostar: {so(('FLOJO',))}\n")


def bloque_elite(rows):
    con_bc = [r for r in rows if r.get("decision_betcris")]
    return (f"🎯 Con tus cuotas BetCris ({len(con_bc)} apuntados):\n"
            f"  💰 Apuestas: {resumen([r for r in con_bc if r['decision_betcris'] == 'APUESTA'], 'beneficio_betcris')}\n"
            f"  ❌ No apostar: {resumen([r for r in con_bc if r['decision_betcris'] == 'FLOJO'], 'beneficio_betcris')}\n"
            f"📊 Todos los avisos, cuota Sofascore: {resumen(rows)}\n"
            f"  🔥 señal A (en caliente): {resumen([r for r in rows if 'A' in r.get('nivel', '')])}\n"
            f"  📈 señal B (Elo): {resumen([r for r in rows if 'B' in r.get('nivel', '')])}\n")


def bloque_ref(rows):
    si = [r for r in rows if r.get("decision_ref") == "SI"]
    no = [r for r in rows if r.get("decision_ref") == "NO"]
    blancos = [r for r in rows if not r.get("decision_ref") and r.get("resultado") != "anulado"]
    pend = sum(1 for r in rows if r.get("resultado") == "pendiente")
    bot = sum(1 for r in rows if r.get("fuente_ref") == "bot")
    tuya = sum(1 for r in rows if r.get("fuente_ref") == "tuya")
    return (f"🟢 Apostar: {resumen(si, 'beneficio_ref')}\n"
            f"🔴 No apostar: {resumen(no, 'beneficio_ref')}\n"
            + (f"⚪ Sin cuota (no cuentan): {len(blancos)}\n" if blancos else "")
            + (f"⏳ Pendientes: {pend}\n" if pend else "")
            + f"(cuota del bot: {bot} · tuya: {tuya})\n")


def u(rows, campo):
    """'N · G✅ P❌ · +X u · yield Y%' solo con los ya terminados."""
    h = [r for r in rows if r.get("resultado") in ("ganado", "perdido") and num(r.get(campo)) is not None]
    if not h:
        return "0 terminados"
    g = sum(1 for r in h if r["resultado"] == "ganado")
    b = sum(float(r[campo]) for r in h)
    return f"{len(h)} · {g}✅ {len(h) - g}❌ · {b:+.2f} u · yield {b / len(h) * 100:+.1f}%"


def bloque_comb(rows):
    si = [r for r in rows if r.get("decision_ref") == "SI"]
    tuyas = [r for r in si if r.get("cuota_betcris")]
    estr = [r for r in si if r.get("estrella")]
    ctrl = [r for r in rows if r.get("decision_ref") == "NO"]
    pend = sum(1 for r in si if r.get("resultado") == "pendiente")
    return (f"🟢 Picks avisados: {u(si, 'beneficio_ref')} (cuota del bot)\n"
            f"✍️ Con tu cuota ({len(tuyas)} apuntados): {u(tuyas, 'beneficio_betcris')}\n"
            + (f"⭐ De ellos, estrella: {u(estr, 'beneficio_ref')}\n" if estr else "")
            + (f"⏳ Pendientes: {pend}\n" if pend else "")
            + f"🔇 Control, no avisados: {u(ctrl, 'beneficio_ref')}\n")


def resumen_dia(picks, st, now):
    """Resumen a partir de las 23:45; si esa noche no hubo ejecucion, de madrugada."""
    if now.hour == 23 and now.minute >= 45:
        dia = now
    elif now.hour < 6:
        dia = now - timedelta(days=1)
    else:
        return
    hoy = dia.strftime("%Y-%m-%d")
    if st.get("resumen", "") >= hoy:
        return
    ini = datetime.strptime(INICIO_A, "%Y-%m-%d")
    tt = [p for p in picks if p.get("regla") == "A" and not es_elite(p)]   # regla A: desde INICIO_A
    del_dia = [p for p in tt if p["fecha"] == hoy]
    tg(f"📋 RESUMEN TT CUP · {dia:%d/%m}\n" + bloque_comb(del_dia)
       + f"\n📈 ACUMULADO desde el {ini:%d/%m}\n" + bloque_comb(tt) + "(1 u por pick)")
    el = [p for p in picks if p.get("regla") == "A" and es_elite(p)]
    if TG_ELITE and st.get("elite_a"):
        el_dia = [p for p in el if p["fecha"] == hoy]
        ini_e = datetime.strptime(st["elite_a"], "%Y-%m-%d")
        tg(token=TG_ELITE, text=f"🇵🇱 RESUMEN ELITE SERIES · {dia:%d/%m}\n" + bloque_comb(el_dia)
           + f"\n📈 ACUMULADO desde el {ini_e:%d/%m}\n" + bloque_comb(el) + "(1 u por pick)")
    lpp = [p for p in picks if p.get("regla") == "LP"]
    if TG_LP and st.get("lp_a"):
        lp_dia = [p for p in lpp if p["fecha"] == hoy]
        ini_l = datetime.strptime(st["lp_a"], "%Y-%m-%d")
        tg(token=TG_LP, text=f"🇨🇿 RESUMEN LIGA PRO · {dia:%d/%m}\n" + bloque_comb(lp_dia)
           + f"\n📈 ACUMULADO desde el {ini_l:%d/%m}\n" + bloque_comb(lpp) + "(1 u por pick)")
    st["resumen"] = hoy


def main():
    if not SRC.KEY:
        raise SystemExit("Falta BETSAPI_TOKEN" if FUENTE == "betsapi" else "Falta ZYLA_API_KEY")
    LLAMADAS_INICIO[0] = mb_bets.LLAMADAS[0]
    now = datetime.now(TZ)
    fin = time.time() + RUN_MIN * 60
    st = leer_estado()
    MK.TEXTOS = st.setdefault("textos", {})  # textos de los avisos (para cambiar su color)
    picks = leer_picks()
    for p in picks:                          # picks de versiones anteriores
        if not p.get("liga"):
            p["liga"] = "TT Cup"
        if not p.get("canal"):
            p["canal"] = "tt"                  # avisos antiguos: se mandaron por el bot principal
        if es_elite(p):
            continue
        if p.get("nivel") == "REGLA" or not p.get("nivel"):
            p["nivel"] = nivel_de(p)
        if not p.get("cuota_sospecha") and num(p.get("prob")):
            p["cuota_minima"] = max(MIN_ODD, round(1 / float(p["prob"]), 2))
            p["cuota_fuerte"] = max(MIN_ODD, round((1 + VALOR_FUERTE) / float(p["prob"]), 2))
            p["cuota_sospecha"] = round((1 + VALOR_SOSPECHA) / float(p["prob"]), 2)
    if not st.get("limpieza_1111"):
        malos = [p for p in picks if not es_elite(p) and str(p.get("pick_jugados_hoy")) == "0"
                 and p.get("aviso", "") >= "2026-11-10 23:00"]
        if malos:
            ids = {p["id"] for p in malos}
            picks[:] = [p for p in picks if p["id"] not in ids or es_elite(p)]
            tg(f"🧹 Borrados {len(malos)} avisos de TT Cup enviados por error desde anoche "
               "(no cumplían la regla: «ya ha jugado 0 hoy»). Ya está corregido y no "
               "cuentan en las estadísticas.")
        st["limpieza_1111"] = True
    if not st.get("inicio"):
        st["inicio"] = now.strftime("%Y-%m-%d")
    if not TG_ELITE and ELITE in LIGAS_SEG:
        LIGAS_SEG.remove(ELITE)                  # sin canal propio, Elite no se usa
    if not TG_LP and LPRO in LIGAS_SEG:
        LIGAS_SEG.remove(LPRO)                   # sin canal propio, Liga Pro no se usa
    if LPRO in LIGAS_SEG and not st.get("lp_a"):
        st["lp_a"] = now.strftime("%Y-%m-%d")
        tg(token=TG_LP, text="🇨🇿 LIGA PRO · canal nuevo (contador a cero)\n"
           "Solo te llegan los picks que cumplen TODO:\n"
           "🔥 ya ha jugado 3 o más hoy y el rival aún no\n"
           "📈 llega en mejor forma que el rival\n"
           "💰 la cuota llega al mínimo\n"
           "💎 y además: paga un 20% o más por encima de su valor, o la cuota está subiendo\n"
           "Partidos de 00:00 a 08:00 no se avisan ni cuentan.\n"
           "↩️ Responde con tu cuota de BetCris. Resumen cada noche en unidades.")
    if ELITE in LIGAS_SEG and not st.get("elite_a"):
        st["elite_a"] = now.strftime("%Y-%m-%d")
        st["canal_elite"] = True
        tg(token=TG_ELITE, text="🇵🇱 ELITE SERIES · sistema nuevo (contador a cero)\n"
           "Solo te llegan los picks que cumplen TODO:\n"
           "🔥 ya ha jugado 3 o más hoy y el rival aún no\n"
           "📈 llega en mejor forma que el rival\n"
           "💰 la cuota llega al mínimo\n"
           "Partidos de 00:00 a 08:00 no se avisan ni cuentan.\n"
           "↩️ Responde con tu cuota de BetCris. Resumen cada noche en unidades.")
    if not st.get("aviso_a_tt"):
        st["aviso_a_tt"] = True
        tg("🔥 TT CUP · sistema nuevo (contador a cero)\n"
           "Solo te llegan los picks que cumplen TODO:\n"
           "🔥 ya ha jugado 3 o más hoy y el rival aún no\n"
           "📈 llega en mejor forma que el rival\n"
           "💰 la cuota llega al mínimo\n"
           "Partidos de 00:00 a 08:00 no se avisan ni cuentan.\n"
           "↩️ Responde con tu cuota de BetCris. Resumen cada noche en unidades.")
    if ELITE in LIGAS_SEG and not st.get("canal_elite"):
        st["canal_elite"] = True
        st["inicio_elite"] = now.strftime("%Y-%m-%d")
        tg(token=TG_ELITE, text="🇵🇱 Canal de ELITE SERIES activo (seguimiento, sin dinero)\n"
           "Solo favoritos, con una de estas dos señales:\n"
           "🔥 ya ha jugado hoy y el rival aún no\n"
           "📈 el Elo le ve mejor de lo que dice la cuota\n"
           "💰 Se apuesta si BetCris paga al menos la cuota del aviso.\n"
           "↩️ Responde con tu cuota igual que en TT Cup. Resumen aparte cada noche.")
    if not st.get("v2"):
        st["v2"] = True
        tg("🟢 Seguimiento TT Cup actualizado\n"
           "Cada aviso te dice qué hacer según la cuota de BetCris:\n"
           "❌ no apostar · 💰 APUESTA · ⭐⭐ FUERTE · ⚠️ valor >30%, desconfiar\n"
           "↩️ Responde al aviso con tu cuota (ej. 1,84) o escribe: apellido cuota\n"
           "Escribe «estado» para saber si está funcionando\n"
           "Todo queda apuntado. Resumen cada noche a las 23:45.")
    # Tus mensajes se leen APARTE, cada 15 s, desde el primer momento: aunque
    # BetsAPI vaya lento o se cuelgue, tus cuotas se apuntan y se contestan igual.
    parar = threading.Event()

    def lector():
        while not parar.is_set():
            try:
                with LOCK:
                    leer_respuestas(picks, st)
                    guardar_picks(picks)
                    guardar_estado(st)
            except Exception as ex:
                print("WARN lector", ex)
            parar.wait(15)
    hilo = threading.Thread(target=lector, daemon=True)
    hilo.start()
    try:
        precargar(now)
    except Exception as ex:
        print("WARN historico", ex)
    # Cada ejecucion se queda RUN_MIN minutos dando una vuelta por minuto: asi
    # contesta rapido a tus cuotas y no depende de que GitHub sea puntual.
    mods = {}
    while True:
        t0 = time.time()
        now = datetime.now(TZ)
        todos_evs = {}
        datos = {}
        for lid in LIGAS_SEG:                # descargas SIN bloquear la lectura de mensajes
            if time.time() > fin + 180:
                print("Vuelta demasiado larga: se deja la liga", lid, "para la siguiente")
                continue
            try:
                datos[lid] = cargar(now, lid)
            except Exception as ex:
                print("ERROR liga", lid, ex)
                with LOCK:
                    st[f"error_{lid}"] = f"{now:%H:%M} {type(ex).__name__}: {str(ex)[:120]}"
        with LOCK:                           # el resto, con picks/estado reservados
            for lid in datos:
                try:
                    evs = datos[lid]
                    todos_evs.update(evs)
                    train, cands = calcular(evs, now)
                    print(f"{now:%H:%M} · {NOMBRE.get(lid, lid)} · {len(evs)} partidos · "
                          f"{len(train)} para entrenar · {len(cands)} candidatos")
                    if FALTAN.get(lid):
                        # historico de BetsAPI aun incompleto: sin avisos (saldrian con datos a medias)
                        total = TRAIN_DAYS + WARMUP_DAYS + 2
                        st[f"historico_{lid}"] = f"{total - FALTAN[lid]}/{total}"
                        if not st.get(f"aviso_hist_{lid}"):
                            st[f"aviso_hist_{lid}"] = now.strftime("%Y-%m-%d %H:%M")
                            t_h = (f"⏳ {NOMBRE.get(lid, lid)}: paso a datos de BetsAPI. Estoy cargando "
                                   f"el historial ({total - FALTAN[lid]}/{total} días). Mientras tanto "
                                   "no hay avisos nuevos (1-2 horas). Te aviso cuando termine.")
                            if lid == ELITE and TG_ELITE:
                                tg(t_h, token=TG_ELITE)
                            elif lid == LPRO and TG_LP:
                                tg(t_h, token=TG_LP)
                            elif lid == TTCUP:
                                tg(t_h)
                        continue
                    if st.get(f"historico_{lid}"):
                        st.pop(f"historico_{lid}")
                        aviso_hist = (f"✅ Histórico de BetsAPI cargado: vuelven los avisos de "
                                      f"{NOMBRE.get(lid, lid)}.")
                        if lid == ELITE and TG_ELITE:
                            tg(aviso_hist, token=TG_ELITE)
                        elif lid == LPRO and TG_LP:
                            tg(aviso_hist, token=TG_LP)
                        elif lid == TTCUP:
                            tg(aviso_hist)
                    # cada liga con su propio modelo (entrenado con sus partidos)
                    if mods.get(lid) is None and len(train) >= 500:
                        mods[lid] = entrenar([vector(f) for f in train], [f["gana_a"] for f in train])
                    if mods.get(lid) is not None and cands:
                        nuevos_picks(cands, mods[lid], picks, st, now,
                                     elite=(lid == ELITE), lp=(lid == LPRO))
                except Exception as ex:          # un fallo en una liga no para la otra
                    print("ERROR liga", lid, ex)
                    st[f"error_{lid}"] = f"{now:%H:%M} {type(ex).__name__}: {str(ex)[:120]}"
            corr = resolver(picks, todos_evs, now)
            try:
                revisar_ref(picks, todos_evs, now)
            except Exception as ex:
                print("WARN revisar_ref", ex)
            if corr:
                tg(f"🔧 Corregidos {corr} picks que estaban como anulados: ya tienen su "
                   "resultado real y cuentan en el resumen.")
            resumen_dia(picks, st, now)
            st["latido"] = now.strftime("%Y-%m-%d %H:%M")   # señal de vida (la mira el TT AVISADOR)
            guardar_picks(picks)
            guardar_estado(st)
        if time.time() + 60 > fin:
            break
        while time.time() - t0 < 60 and time.time() + 20 < fin:
            time.sleep(5)
    parar.set()
    hilo.join(timeout=60)
    with LOCK:                               # ultima lectura y guardado antes de acabar
        leer_respuestas(picks, st)
        guardar_picks(picks)
        guardar_estado(st)

if __name__ == "__main__":
    main()
