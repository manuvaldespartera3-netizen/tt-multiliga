"""
mb_backtest.py - Validacion del bot multiliga (TT Cup, Liga Pro Checa, Elite Series).

Todo se calcula con lo que se sabia 2 horas antes de cada partido (CUTOFF_H),
sin mirar nunca el futuro:
  - Elo dinamico, actualizado partido a partido (tambien los de ese mismo dia)
  - Forma ajustada (ultimos 10 partidos frente a lo esperado)
  - Lo que lleva hoy cada jugador (partidos y victorias del dia)
  - H2H de los ultimos 90 dias

Estrategias que se miden:
  🔥 REGLA EN CALIENTE: el pick ya ha jugado hoy y el rival todavia no.
  🔥🧠 REGLA + MODELO: la regla, y ademas el modelo ve valor (prob x cuota - 1).
  🧠 MODELO GENERAL: solo referencia, ultimos VISTO_DIAS dias.

Periodos (para que la prueba sea honesta):
  arranque   primeros ARRANQUE_DIAS dias: solo sirven para que el modelo aprenda
  validacion lo que hay en medio: NUNCA se ha mirado al buscar la regla
  ya visto   ultimos VISTO_DIAS dias: donde encontramos la regla en TT Cup
El modelo se re-entrena cada 15 dias solo con partidos anteriores (como en real).

Veredicto (sobre el periodo de validacion, cuota de Sofascore, 1 u por pick):
  🟢 CONFIRMADA   ≥300 picks, yield > 0, rango probable (90%) entero por encima
                  de 0 y al menos 2 de cada 3 meses en positivo
  🟡 PROMETEDORA  yield > 0 pero no cumple todo lo anterior
  🔴 DESCARTADA   yield ≤ 0

Variables:
  ZYLA_API_KEY                 clave de Zyla (secreto)
  TELEGRAM_TOKEN, MY_CHAT_ID   opcionales: si estan, manda informe y CSV
  LIGA          15005 | 19039 | 19041 | todas   (por defecto: todas)
  BT_DAYS       dias analizados (por defecto 180)
  BT_WARMUP     dias previos solo para calentar Elo/forma/H2H (por defecto 45)
  ARRANQUE_DIAS por defecto 30 · VISTO_DIAS por defecto 30
  CUTOFF_H      horas antes del partido con las que se decide (por defecto 2)
  MAX_ODDS      consultas nuevas de cuotas maximas por liga y ejecucion (8000)
  BT_PACE       segundos entre consultas (1.2 = 50/min; deja hueco al avisador)
"""
import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta

import numpy as np

import mb_api
from mb_api import LIGAS, TZ, parse, day_events, odds_raw, odds_from

LIGA = os.getenv("LIGA", "todas").strip() or "todas"
DAYS = int(os.getenv("BT_DAYS", "180"))
WARMUP = int(os.getenv("BT_WARMUP", "45"))
ARRANQUE = int(os.getenv("ARRANQUE_DIAS", "30"))
VISTO = int(os.getenv("VISTO_DIAS", "30"))
CUTOFF = float(os.getenv("CUTOFF_H", "2")) * 3600
MAX_ODDS = int(os.getenv("MAX_ODDS", "8000"))
PACE = float(os.getenv("BT_PACE", "1.2"))
TG = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT = os.getenv("MY_CHAT_ID", "").strip()
OUT_ROOT = os.getenv("BT_OUT", "resultados")

ELO_K = 24
MATCH_LEN = 30 * 60          # un partido se da por terminado 30 min despues de empezar
MIN_PLAYED = 10              # con menos partidos previos el jugador "no se conoce": no hay pick
H2H_DAYS = 90
RETRAIN_DAYS = 15
MIN_ODD, MAX_ODD = 1.25, 4.0
UMBRALES_MODELO = [0.05, 0.10]
MIN_N_OK = 300               # picks minimos en validacion para dar una regla por confirmada
MIN_N = 30                   # menos picks en una fila = no fiable
Z90 = 1.645


# ======================================================================= datos
def slug(lid):
    return LIGAS[lid].lower().replace(" ", "_")


def cache_dir(lid):
    d = os.path.join(OUT_ROOT, slug(lid), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def fetch_day(lid, d):
    path = os.path.join(cache_dir(lid), f"{d}.json")
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if os.path.exists(path) and d < today:
        with open(path) as f:
            return json.load(f)
    for intento in range(6):
        try:
            evs = day_events(lid, d)
            break
        except Exception as ex:
            print("WARN dia", lid, d, ex)
            time.sleep(10 * (intento + 1))
    else:
        return []
    with open(path, "w") as f:
        json.dump(evs, f)
    time.sleep(PACE)
    return evs


def ts_of(day):
    return datetime.combine(day, datetime.min.time(), TZ).timestamp()


def load(lid):
    end = datetime.now(TZ).date() - timedelta(days=1)          # hoy no (incompleto)
    start = end - timedelta(days=DAYS + WARMUP - 1)
    evs = {}
    d = start
    while d <= end:
        for e in fetch_day(lid, d.strftime("%Y-%m-%d")):
            evs[e.get("id")] = e
        d += timedelta(days=1)
    ms = [p for p in (parse(e) for e in evs.values()) if p and p["final"]]
    ms.sort(key=lambda x: (x["t"], x["id"]))
    per = dict(
        inicio=end - timedelta(days=DAYS - 1),
        eval=end - timedelta(days=DAYS - ARRANQUE - 1),
        visto=end - timedelta(days=VISTO - 1),
        fin=end)
    return ms, per


class Cuotas:
    """Consulta cuotas con cache, cupo y parada si Zyla deja de responder."""

    def __init__(self, lid):
        self.lid = lid
        self.nuevas = 0
        self.cupo = 0
        self.fallos_seguidos = 0
        self.cortado = False

    def get(self, mid):
        path = os.path.join(cache_dir(self.lid), f"odds_{mid}.json")
        if os.path.exists(path):
            with open(path) as f:
                return odds_from(json.load(f))
        if self.cortado or self.nuevas >= MAX_ODDS:
            self.cupo += 1
            return None
        data = None
        for intento in range(3):
            try:
                data = odds_raw(mid)
                break
            except Exception as ex:
                print("WARN cuotas", mid, ex)
                time.sleep(8 * (intento + 1))
        self.nuevas += 1
        time.sleep(PACE)
        if data is None:
            self.fallos_seguidos += 1
            if self.fallos_seguidos >= 5:          # Zyla corta (cupo agotado o caida)
                print("STOP cuotas: 5 fallos seguidos")
                self.cortado = True
            self.cupo += 1
            return None
        self.fallos_seguidos = 0
        with open(path, "w") as f:
            json.dump(data, f)
        return odds_from(data)


# ============================================================ caracteristicas
def day_key(t):
    return datetime.fromtimestamp(t, TZ).strftime("%Y-%m-%d")


class Estado:
    """Lo que se sabe de cada jugador en un instante dado."""

    def __init__(self):
        self.elo = defaultdict(lambda: 1500.0)
        self.n = defaultdict(int)
        self.hist = defaultdict(lambda: deque(maxlen=10))     # (gana - esperado)
        self.dia = defaultdict(lambda: [None, 0, 0])          # dia, jugados, ganados
        self.h2h = defaultdict(list)                          # pareja -> [(t, ganador)]

    def aplicar(self, m):
        a, b = m["ida"], m["idb"]
        ea = 1 / (1 + 10 ** ((self.elo[b] - self.elo[a]) / 400))
        wa = 1.0 if m["final"][0] > m["final"][1] else 0.0
        self.elo[a] += ELO_K * (wa - ea)
        self.elo[b] -= ELO_K * (wa - ea)
        self.n[a] += 1
        self.n[b] += 1
        self.hist[a].append(wa - ea)
        self.hist[b].append((1 - wa) - (1 - ea))
        dk = day_key(m["t"])
        for p, w in ((a, wa), (b, 1 - wa)):
            st = self.dia[p]
            if st[0] != dk:
                st[0], st[1], st[2] = dk, 0, 0
            st[1] += 1
            st[2] += int(w)
        self.h2h[frozenset((a, b))].append((m["t"], a if wa else b))

    def hoy(self, p, t):
        st = self.dia[p]
        if st[0] != day_key(t):
            return 0, 0
        return st[1], st[2]

    def feats(self, m):
        a, b, t = m["ida"], m["idb"], m["t"]
        pe = 1 / (1 + 10 ** ((self.elo[b] - self.elo[a]) / 400))
        ja, ga = self.hoy(a, t)
        jb, gb = self.hoy(b, t)
        lim = t - H2H_DAYS * 86400
        wa = wb = 0
        for (tt, w) in self.h2h.get(frozenset((a, b)), []):
            if tt >= lim:
                if w == a:
                    wa += 1
                else:
                    wb += 1
        return dict(elo_a=round(self.elo[a]), elo_b=round(self.elo[b]), p_elo=pe,
                    n_a=self.n[a], n_b=self.n[b],
                    forma_a=sum(self.hist[a]), forma_b=sum(self.hist[b]),
                    hoy_j_a=ja, hoy_g_a=ga, hoy_j_b=jb, hoy_g_b=gb, h2h_a=wa, h2h_b=wb)


def vector(f):
    """Caracteristicas numericas desde el punto de vista del jugador A."""
    pe = min(max(f["p_elo"], 0.01), 0.99)
    h = (f["h2h_a"] - f["h2h_b"]) / (f["h2h_a"] + f["h2h_b"] + 2)
    hoy_a = f["hoy_g_a"] - (f["hoy_j_a"] - f["hoy_g_a"])
    hoy_b = f["hoy_g_b"] - (f["hoy_j_b"] - f["hoy_g_b"])
    return [math.log(pe / (1 - pe)), f["forma_a"] - f["forma_b"], h,
            hoy_a - hoy_b, f["hoy_j_a"] - f["hoy_j_b"]]


NOMBRES = ["Elo", "Forma ajustada", "H2H 90 días", "Balance de hoy", "Partidos jugados hoy"]


def construir(ms, inicio_ts):
    """Caracteristicas de cada partido del periodo, con el corte de 2 h."""
    est = Estado()
    pend = sorted(ms, key=lambda x: x["t"] + MATCH_LEN)
    i = 0
    filas = []
    for m in ms:
        corte = m["t"] - CUTOFF
        while i < len(pend) and pend[i]["t"] + MATCH_LEN <= corte:
            est.aplicar(pend[i])
            i += 1
        if m["t"] < inicio_ts:
            continue
        f = est.feats(m)
        if f["n_a"] < MIN_PLAYED or f["n_b"] < MIN_PLAYED:
            continue
        f.update(id=m["id"], t=m["t"], a=m["a"], b=m["b"], fecha=day_key(m["t"]),
                 gana_a=int(m["final"][0] > m["final"][1]),
                 marcador=f"{m['final'][0]}-{m['final'][1]}")
        # regla en caliente: un lado ya ha jugado hoy y el otro no
        f["caliente"] = ("a" if f["hoy_j_a"] > 0 and f["hoy_j_b"] == 0 else
                         "b" if f["hoy_j_b"] > 0 and f["hoy_j_a"] == 0 else None)
        filas.append(f)
    return filas


# ============================================ modelo (regresion logistica, numpy)
def entrenar(X, y, l2=1.0, iters=2000, lr=0.2):
    X = np.asarray(X, float)
    y = np.asarray(y, float)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    w = np.zeros(X.shape[1])
    b = 0.0
    n = len(y)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Z @ w + b, -30, 30)))
        e = p - y
        w -= lr * (Z.T @ e / n + l2 * w / n)
        b -= lr * e.mean()
    return dict(w=w, b=b, mu=mu, sd=sd)


def predecir(mod, X):
    Z = (np.asarray(X, float) - mod["mu"]) / mod["sd"]
    return 1 / (1 + np.exp(-np.clip(Z @ mod["w"] + mod["b"], -30, 30)))


def walk_forward(filas, eval_ts):
    """Probabilidad del modelo para cada partido evaluado, entrenando SOLO con
    partidos anteriores y re-entrenando cada RETRAIN_DAYS dias."""
    ultimo = None
    corte = eval_ts
    fin = max(f["t"] for f in filas) + 1
    while corte < fin:
        sig = corte + RETRAIN_DAYS * 86400
        train = [f for f in filas if f["t"] < corte]
        bloque = [f for f in filas if corte <= f["t"] < sig]
        if bloque and len(train) >= 200:
            mod = entrenar([vector(f) for f in train], [f["gana_a"] for f in train])
            ultimo = mod
            ps = predecir(mod, [vector(f) for f in bloque])
            for f, p in zip(bloque, ps):
                f["p"] = float(p)
        corte = sig
    return ultimo


# ================================================================= apuestas
def lado_pick(f, lado):
    """(cuota, gana, prob_modelo, es_favorito_casa) del lado elegido."""
    o = "b" if lado == "a" else "a"
    c, co = f.get(f"fin_{lado}"), f.get(f"fin_{o}")
    gana = f["gana_a"] if lado == "a" else 1 - f["gana_a"]
    p = f.get("p")
    if p is not None and lado == "b":
        p = 1 - p
    return c, gana, p, (c is not None and co is not None and c < co)


def pick(f, lado, estrategia):
    c, gana, p, fav = lado_pick(f, lado)
    if not c or not (MIN_ODD <= c <= MAX_ODD):
        return None
    v = (p * c - 1) if p is not None else None
    return dict(f=f, lado=lado, cuota=c, gana=gana, p=p, valor=v, fav=fav,
                ben=(c - 1) if gana else -1.0, estrategia=estrategia,
                mes=f["fecha"][:7])


def picks_regla(filas, umbral=None, contra=False):
    """Regla en caliente (opcionalmente con valor del modelo ≥ umbral).
    contra=True apuesta al otro lado (al que aun no ha jugado): contraste."""
    out = []
    for f in filas:
        if not f["caliente"]:
            continue
        lado = f["caliente"] if not contra else ("b" if f["caliente"] == "a" else "a")
        pk = pick(f, lado, "regla")
        if not pk:
            continue
        if umbral is not None and (pk["valor"] is None or pk["valor"] < umbral):
            continue
        out.append(pk)
    return out


def picks_modelo(filas, umbral):
    out = []
    for f in filas:
        if f.get("p") is None:
            continue
        best = None
        for lado in ("a", "b"):
            pk = pick(f, lado, "modelo")
            if pk and pk["valor"] >= umbral and (best is None or pk["valor"] > best["valor"]):
                best = pk
        if best:
            out.append(best)
    return out


def stats(picks):
    n = len(picks)
    if not n:
        return None
    bens = np.array([p["ben"] for p in picks])
    y = bens.mean()
    se = bens.std() / math.sqrt(n) if n > 1 else 1.0
    return dict(n=n, g=sum(p["gana"] for p in picks), ben=bens.sum(), y=y,
                lo=y - Z90 * se, hi=y + Z90 * se,
                cm=sum(p["cuota"] for p in picks) / n)


def linea(picks, rango=False):
    s = stats(picks)
    if not s:
        return "0 picks"
    ic = "🟢" if s["ben"] > 0 and s["n"] >= MIN_N else ("🔴" if s["ben"] < 0 else "⚪")
    t = (f"{ic} {s['n']} picks · win {s['g'] / s['n'] * 100:.0f}% · cuota {s['cm']:.2f} · "
         f"{s['ben']:+.1f} u · YIELD {s['y'] * 100:+.1f}%")
    if rango:
        t += f" · rango probable {s['lo'] * 100:+.0f}% a {s['hi'] * 100:+.0f}%"
    if s["n"] < MIN_N:
        t += " · ⚠️ pocos"
    return t


def por_mes(picks):
    meses = defaultdict(list)
    for p in picks:
        meses[p["mes"]].append(p)
    res = []
    for m in sorted(meses):
        s = stats(meses[m])
        res.append((m, s))
    return res


def veredicto(picks):
    s = stats(picks)
    if not s or s["y"] <= 0:
        return "🔴 DESCARTADA", "no gana dinero en los meses nunca vistos"
    meses = [x for x in por_mes(picks) if x[1]["n"] >= MIN_N]
    pos = sum(1 for _, x in meses if x["ben"] > 0)
    ok_meses = meses and pos * 3 >= 2 * len(meses)
    faltas = []
    if s["n"] < MIN_N_OK:
        faltas.append(f"solo {s['n']} picks (mín. {MIN_N_OK})")
    if s["lo"] <= 0:
        faltas.append("el rango probable aún incluye perder")
    if not ok_meses:
        faltas.append(f"positiva en {pos} de {len(meses)} meses")
    if not faltas:
        return "🟢 CONFIRMADA", f"gana en {pos} de {len(meses)} meses y el rango probable es todo positivo"
    return "🟡 PROMETEDORA", "; ".join(faltas)


MESES = {"01": "ene", "02": "feb", "03": "mar", "04": "abr", "05": "may", "06": "jun",
         "07": "jul", "08": "ago", "09": "sep", "10": "oct", "11": "nov", "12": "dic"}


def bloque_estrategia(L, titulo, val, visto, contraste=None):
    v, motivo = veredicto(val)
    L.append(titulo)
    L.append(f"VEREDICTO: {v}")
    L.append(f"   ({motivo})")
    L.append(f"• Validación (nunca vista): {linea(val, rango=True)}")
    meses = por_mes(val)
    if meses:
        L.append("• Por meses: " + " · ".join(
            f"{MESES[m[5:]]} {s['y'] * 100:+.0f}% ({s['n']})" for m, s in meses))
    tr = [(1.25, 1.60), (1.60, 2.00), (2.00, 2.60), (2.60, 4.01)]
    L.append("• Por cuota:")
    for lo, hi in tr:
        L.append(f"   {lo:.2f}–{min(hi, 4):.2f}: {linea([p for p in val if lo <= p['cuota'] < hi])}")
    L.append(f"• Pick favorito de la casa: {linea([p for p in val if p['fav']])}")
    L.append(f"• Pick NO favorito: {linea([p for p in val if not p['fav']])}")
    if contraste is not None:
        L.append(f"• Contraste, apostando al que NO ha jugado: {linea(contraste)}")
    L.append(f"• Últimos {VISTO} días (ya vistos): {linea(visto)}")
    return v


# ================================================================== informe
def analizar(lid):
    nombre = LIGAS[lid]
    ms, per = load(lid)
    inicio_ts, eval_ts = ts_of(per["inicio"]), ts_of(per["eval"])
    visto_ts = ts_of(per["visto"])
    print(f"[{nombre}] {len(ms)} partidos terminados (con calentamiento)")
    filas = construir(ms, inicio_ts)
    L = [f"📊 {nombre.upper()} · validación bot multiliga",
         f"Decisión {CUTOFF / 3600:g} h antes del partido · cuota de Sofascore · 1 u por pick",
         f"Arranque (solo aprende): {per['inicio']:%d/%m} – {per['eval'] - timedelta(days=1):%d/%m}",
         f"Validación (nunca vista): {per['eval']:%d/%m} – {per['visto'] - timedelta(days=1):%d/%m}",
         f"Ya vistos: {per['visto']:%d/%m} – {per['fin']:%d/%m}",
         f"{len(filas)} partidos con ambos jugadores conocidos", ""]
    if len(filas) < 500:
        L.append("❌ Muy pocos partidos para sacar conclusiones.")
        return L, [], {}

    mod = walk_forward(filas, eval_ts)
    ev = [f for f in filas if f["t"] >= eval_ts and f.get("p") is not None]

    # ---- cuotas: primero los partidos de la regla, despues los ultimos dias
    cu = Cuotas(lid)
    necesarios = sorted([f for f in ev if f["caliente"]], key=lambda x: -x["t"])
    necesarios += sorted([f for f in ev if not f["caliente"] and f["t"] >= visto_ts],
                         key=lambda x: -x["t"])
    for f in necesarios:
        o = cu.get(f["id"])
        if o:
            f["ini_a"], f["fin_a"], f["ini_b"], f["fin_b"] = o
    print(f"[{nombre}] cuotas nuevas {cu.nuevas} · pendientes {cu.cupo}")
    if cu.cupo:
        L.append(f"⚠️ Faltan cuotas de {cu.cupo} partidos "
                 + ("(Zyla dejó de responder: puede que se haya acabado el cupo del mes). "
                    if cu.cortado else "(límite por ejecución). ")
                 + "Vuelve a lanzarlo: sigue donde lo dejó.")
        L.append("")

    val = [f for f in ev if f["t"] < visto_ts]
    vis = [f for f in ev if f["t"] >= visto_ts]
    con_cuota = [f for f in val if f["caliente"] and f.get("fin_a")]
    L.append(f"Partidos de la regla en validación: {sum(1 for f in val if f['caliente'])} "
             f"({len(con_cuota)} con cuota; sin cuota = la casa no lo ofrecía)")
    L.append("")

    ver = {}
    ver["regla"] = bloque_estrategia(
        L, "🔥 REGLA EN CALIENTE (el pick ya ha jugado hoy, el rival aún no)",
        picks_regla(val), picks_regla(vis), contraste=picks_regla(val, contra=True))
    L.append("")
    for u in UMBRALES_MODELO:
        ver[f"regla+{u}"] = bloque_estrategia(
            L, f"🔥🧠 REGLA + MODELO (valor ≥{u * 100:.0f}%)",
            picks_regla(val, umbral=u), picks_regla(vis, umbral=u))
        L.append("")

    # ---- modelo general, solo ultimos dias (referencia)
    L.append(f"🧠 MODELO GENERAL · últimos {VISTO} días (solo referencia)")
    vc = [f for f in vis if f.get("fin_a")]
    if vc:
        ys = [f["gana_a"] for f in vc]
        pm = [min(max(f["p"], 1e-6), 1 - 1e-6) for f in vc]
        pc = []
        for f in vc:
            ia, ib = 1 / f["fin_a"], 1 / f["fin_b"]
            pc.append(ia / (ia + ib))

        def ll(ps):
            return -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in zip(ps, ys)) / len(ys)
        fav_c = sum((p >= 0.5) == bool(y) for p, y in zip(pc, ys)) / len(ys)
        fav_m = sum((p >= 0.5) == bool(y) for p, y in zip(pm, ys)) / len(ys)
        L.append(f"• Favorito gana: modelo {fav_m * 100:.1f}% · casa {fav_c * 100:.1f}%")
        a, b = ll(pm), ll(pc)
        L.append(f"• Precisión (menos es mejor): modelo {a:.4f} · casa {b:.4f} "
                 + ("✅ modelo mejor" if a < b else "⚠️ casa mejor"))
        marg = sum(1 / f["fin_a"] + 1 / f["fin_b"] for f in vc) / len(vc) - 1
        L.append(f"• Margen medio de la casa: {marg * 100:.1f}%")
        for u in UMBRALES_MODELO:
            L.append(f"• Valor ≥{u * 100:.0f}%: {linea(picks_modelo(vc, u))}")
    else:
        L.append("• Sin cuotas.")
    if mod is not None:
        L.append("• Peso de cada factor: " + " · ".join(
            f"{nm} {w:+.2f}" for nm, w in sorted(zip(NOMBRES, mod["w"]), key=lambda x: -abs(x[1]))))
    L.append("")

    dias_val = max(1, (visto_ts - eval_ts) / 86400)
    L.append(f"📅 Picks al día · regla: ~{len(picks_regla(val)) / dias_val:.1f} · "
             f"regla+modelo 10%: ~{len(picks_regla(val, umbral=0.10)) / dias_val:.1f}")
    return L, ev, ver


def guardar_csv(lid, ev):
    out = os.path.join(OUT_ROOT, slug(lid))
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "picks_regla.csv")
    cols = ["fecha", "hora", "periodo", "pick", "rival", "cuota", "gana", "beneficio",
            "prob_modelo", "valor", "favorito_casa", "pick_jugados_hoy", "pick_ganados_hoy",
            "elo_pick", "elo_rival", "h2h_pick", "h2h_rival", "marcador", "id"]
    visto_ts = None
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for f in sorted(ev, key=lambda x: x["t"]):
            if not f["caliente"]:
                continue
            pk = pick(f, f["caliente"], "regla")
            if not pk:
                continue
            s, o = f["caliente"], ("b" if f["caliente"] == "a" else "a")
            dt = datetime.fromtimestamp(f["t"], TZ)
            if visto_ts is None:
                end = datetime.now(TZ).date() - timedelta(days=1)
                visto_ts = ts_of(end - timedelta(days=VISTO - 1))
            w.writerow([dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"),
                        "visto" if f["t"] >= visto_ts else "validacion",
                        f[s], f[o], pk["cuota"], pk["gana"], round(pk["ben"], 2),
                        round(pk["p"], 3), round(pk["valor"], 3), int(pk["fav"]),
                        f[f"hoy_j_{s}"], f[f"hoy_g_{s}"], f[f"elo_{s}"], f[f"elo_{o}"],
                        f[f"h2h_{s}"], f[f"h2h_{o}"], f["marcador"], f["id"]])
    return path


# ================================================================= telegram
def tg_msg(text):
    if not (TG and CHAT):
        return
    for i in range(0, len(text), 3800):
        data = urllib.parse.urlencode({"chat_id": CHAT, "text": text[i:i + 3800]}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{TG}/sendMessage", data, timeout=30)
        except Exception as ex:
            print("WARN telegram", ex)


def tg_file(path):
    if not (TG and CHAT) or not os.path.exists(path):
        return
    b = uuid.uuid4().hex
    with open(path, "rb") as fh:
        content = fh.read()
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{CHAT}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"document\"; "
            f"filename=\"{os.path.basename(path)}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            ).encode() + content + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG}/sendDocument", body,
                                 {"Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        urllib.request.urlopen(req, timeout=60)
    except Exception as ex:
        print("WARN telegram archivo", ex)


def main():
    if not mb_api.KEY:
        raise SystemExit("Falta ZYLA_API_KEY")
    ids = list(LIGAS) if LIGA.lower() == "todas" else [LIGA]
    for lid in ids:
        if lid not in LIGAS:
            raise SystemExit(f"Liga desconocida: {lid}")
        tg_msg(f"⏳ Empieza la validación de {LIGAS[lid]} ({DAYS} días). Tardará un rato.")
        lineas, ev, _ = analizar(lid)
        txt = "\n".join(lineas)
        print(txt)
        out = os.path.join(OUT_ROOT, slug(lid))
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "informe.txt"), "w") as fh:
            fh.write(txt + "\n")
        tg_msg(txt)
        if ev:
            tg_file(guardar_csv(lid, ev))


if __name__ == "__main__":
    main()
