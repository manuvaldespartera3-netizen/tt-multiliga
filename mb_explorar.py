"""
mb_explorar.py - Busqueda profunda de patrones ganadores en una liga, con
CONFIRMACION en meses que no se han usado para buscarlos.

Por que asi: si se prueban muchos patrones sobre los mismos datos, alguno sale
"ganador" por pura suerte. Por eso el periodo se parte en dos:
  EXPLORACION  (primeros 2/3)  se prueban todos los patrones
  CONFIRMACION (ultimo 1/3)    solo se mira como les va a los que pasaron
Un patron solo vale si gana en las dos partes.

Todo se calcula con lo que se sabia 2 horas antes de cada partido.

Parte 1 · ~55 patrones fijados de antemano:
  cuota, local/visitante, H2H, racha, lo que lleva hoy, revancha, descanso,
  Elo frente a la casa, hora, MARGEN DE PUNTOS y de SETS (domina aunque pierda),
  como acabo su ultimo partido (3-0, 2-3...), minutos de descanso entre partidos,
  y jugadores concretos que la casa paga mal.
Parte 2 · Modelo de IA (gradient boosting) que parte de la cuota de la casa y
  aprende a CORREGIRLA con todas esas variables. Se entrena solo con el pasado
  (re-entrenando cada 15 dias). El umbral de valor se elige en la exploracion y
  se comprueba en la confirmacion.

Variables (ademas de las de mb_backtest): LIGA (por defecto 19039 = Liga Pro),
BT_DAYS (150), BT_MAX_MIN (290), BT_PACE
"""
import csv
import math
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

os.environ.setdefault("LIGA", "19039")
os.environ.setdefault("BT_DAYS", "150")

import numpy as np

import mb_api
import mb_backtest as bt
from mb_api import LIGAS, TZ
from mb_backtest import Estado, MATCH_LEN, CUTOFF, MIN_PLAYED, MIN_ODD, MAX_ODD, Z90

MIN_EXPL = 100           # picks minimos en exploracion para considerar un patron
MIN_CONF = 60            # picks minimos en confirmacion para dar veredicto
MIN_JUG = 25             # picks minimos de un jugador para medir su sesgo
ML_ARRANQUE = 40         # dias iniciales solo para entrenar el modelo
ML_CADA = 15             # re-entrenar cada X dias
ML_UMBRALES = [0.03, 0.05, 0.08, 0.12, 0.20]


# ============================================================ caracteristicas
class EstadoX(Estado):
    """Estado del backtest + racha, margenes de puntos y sets, ultimo partido de
    hoy, descanso y revanchas."""

    def __init__(self):
        super().__init__()
        self.ult_t = {}
        self.racha = defaultdict(int)
        self.ult_hoy = {}
        self.vs_hoy = {}
        self.pts = defaultdict(lambda: deque(maxlen=10))
        self.sets = defaultdict(lambda: deque(maxlen=10))

    def aplicar(self, m):
        super().aplicar(m)
        a, b = m["ida"], m["idb"]
        sa, sb = m["final"]
        wa = sa > sb
        pa, pb = m.get("pts") or (0, 0)
        dk = bt.day_key(m["t"])
        for p, o, w, s1, s2, p1, p2 in ((a, b, wa, sa, sb, pa, pb), (b, a, not wa, sb, sa, pb, pa)):
            self.ult_t[p] = m["t"]
            r = self.racha[p]
            self.racha[p] = (max(r, 0) + 1) if w else (min(r, 0) - 1)
            self.ult_hoy[p] = (dk, int(w), f"{s1}-{s2}", m["t"])
            self.vs_hoy[(dk, p, o)] = int(w)
            self.sets[p].append(s1 - s2)
            if p1 + p2 > 0:
                self.pts[p].append((p1 - p2) / (p1 + p2))

    def lado(self, p, o, t):
        dk = bt.day_key(t)
        u = self.ult_hoy.get(p)
        hoy = u if u and u[0] == dk else None
        lt = self.ult_t.get(p)
        return dict(ult=hoy[1] if hoy else None,
                    ult_marc=hoy[2] if hoy else None,
                    min_ult=(t - hoy[3]) / 60 if hoy else None,
                    racha=self.racha[p],
                    descanso=(t - lt) / 86400 if lt else None,
                    revancha=self.vs_hoy.get((dk, p, o)),
                    m_pts=float(np.mean(self.pts[p])) if self.pts[p] else 0.0,
                    m_sets=float(np.mean(self.sets[p])) if self.sets[p] else 0.0)


def construir(ms, inicio_ts):
    est = EstadoX()
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
        f.update(id=m["id"], t=m["t"], a=m["a"], b=m["b"],
                 gana_a=int(m["final"][0] > m["final"][1]),
                 la=est.lado(m["ida"], m["idb"], m["t"]),
                 lb=est.lado(m["idb"], m["ida"], m["t"]))
        filas.append(f)
    return filas


def apuestas(filas):
    """Cada partido con cuota da dos apuestas posibles, una por jugador."""
    out = []
    for f in filas:
        if not f.get("fin_a"):
            continue
        ia, ib = 1 / f["fin_a"], 1 / f["fin_b"]
        for s, o in (("a", "b"), ("b", "a")):
            c, cr = f[f"fin_{s}"], f[f"fin_{o}"]
            if not (MIN_ODD <= c <= MAX_ODD):
                continue
            pe = f["p_elo"] if s == "a" else 1 - f["p_elo"]
            pm = (ia if s == "a" else ib) / (ia + ib)
            ls, lo = f[f"l{s}"], f[f"l{o}"]
            gana = f["gana_a"] if s == "a" else 1 - f["gana_a"]
            out.append(dict(
                t=f["t"], id=f["id"], c=c, cr=cr, fav=c < cr, local=s == "a",
                pelo=pe, pmkt=pm, dif=pe - pm,
                h2h=f[f"h2h_{s}"], h2hr=f[f"h2h_{o}"],
                hj=f[f"hoy_j_{s}"], hjr=f[f"hoy_j_{o}"],
                ult=ls["ult"], ultr=lo["ult"], umarc=ls["ult_marc"], umarcr=lo["ult_marc"],
                minu=ls["min_ult"], minur=lo["min_ult"],
                racha=ls["racha"], rachar=lo["racha"],
                desc=ls["descanso"], descr=lo["descanso"], rev=ls["revancha"],
                mpts=ls["m_pts"], mptsr=lo["m_pts"], msets=ls["m_sets"], msetsr=lo["m_sets"],
                hora=datetime.fromtimestamp(f["t"], TZ).hour,
                jugador=f[s], rival=f[o], gana=gana, ben=(c - 1) if gana else -1.0))
    return out


# =================================================================== patrones
def rango(lo, hi):
    return lambda b: lo <= b["c"] < hi


def dpts(b):
    return b["mpts"] - b["mptsr"]


PATRONES = [
    # --- cuota
    ("Favorito 1,25–1,50", rango(1.25, 1.50)),
    ("Favorito 1,50–1,70", rango(1.50, 1.70)),
    ("Cuota 1,70–1,90", rango(1.70, 1.90)),
    ("Cuota 1,90–2,20", rango(1.90, 2.20)),
    ("No favorito 2,20–2,70", rango(2.20, 2.70)),
    ("No favorito 2,70–4,00", rango(2.70, 4.01)),
    ("Local favorito", lambda b: b["local"] and b["fav"]),
    ("Visitante favorito", lambda b: not b["local"] and b["fav"]),
    ("Local no favorito", lambda b: b["local"] and not b["fav"]),
    ("Visitante no favorito", lambda b: not b["local"] and not b["fav"]),
    # --- H2H
    ("H2H a favor ≥3 victorias más", lambda b: b["h2h"] - b["h2hr"] >= 3),
    ("H2H a favor ≥3 más y NO favorito", lambda b: b["h2h"] - b["h2hr"] >= 3 and not b["fav"]),
    ("H2H en contra ≥3 y favorito", lambda b: b["h2hr"] - b["h2h"] >= 3 and b["fav"]),
    ("H2H ≥6 partidos y gana ≥70%", lambda b: b["h2h"] + b["h2hr"] >= 6 and b["h2h"] >= 0.7 * (b["h2h"] + b["h2hr"])),
    ("Sin H2H reciente, favorito", lambda b: b["h2h"] + b["h2hr"] == 0 and b["fav"]),
    ("Sin H2H reciente, no favorito", lambda b: b["h2h"] + b["h2hr"] == 0 and not b["fav"]),
    # --- lo que lleva hoy
    ("Ganó su último hoy y el rival perdió", lambda b: b["ult"] == 1 and b["ultr"] == 0),
    ("Perdió su último hoy y el rival ganó", lambda b: b["ult"] == 0 and b["ultr"] == 1),
    ("Perdió su último hoy (no favorito)", lambda b: b["ult"] == 0 and not b["fav"]),
    ("Ganó su último hoy (favorito)", lambda b: b["ult"] == 1 and b["fav"]),
    ("Ha jugado ≥2 menos que el rival hoy", lambda b: b["hjr"] - b["hj"] >= 2),
    ("Ha jugado ≥2 más que el rival hoy", lambda b: b["hj"] - b["hjr"] >= 2),
    ("Rival con ≥4 partidos hoy", lambda b: b["hjr"] >= 4),
    ("Ya ha jugado hoy y el rival no", lambda b: b["hj"] > 0 and b["hjr"] == 0),
    ("Ya ha jugado hoy y el rival no, favorito", lambda b: b["hj"] > 0 and b["hjr"] == 0 and b["fav"]),
    # --- como acabo su ultimo partido de hoy
    ("Perdió su último 2-3 (por poco)", lambda b: b["umarc"] == "2-3"),
    ("Perdió su último 0-3 (paliza)", lambda b: b["umarc"] == "0-3"),
    ("Ganó su último 3-0", lambda b: b["umarc"] == "3-0"),
    ("Rival perdió su último 0-3", lambda b: b["umarcr"] == "0-3"),
    ("Rival ganó su último 3-2 (desgaste)", lambda b: b["umarcr"] == "3-2"),
    # --- minutos entre partidos (hoy)
    ("Rival juega seguido (<45 min de su último)", lambda b: b["minur"] is not None and b["minur"] < 45),
    ("Juega seguido (<45 min de su último)", lambda b: b["minu"] is not None and b["minu"] < 45),
    # --- margen de puntos y sets (domina aunque pierda / gana por poco)
    ("Domina en puntos (últ. 10) y NO favorito", lambda b: dpts(b) >= 0.04 and not b["fav"]),
    ("Domina en puntos (últ. 10), cualquier cuota", lambda b: dpts(b) >= 0.06),
    ("Favorito con peor margen de puntos", lambda b: b["fav"] and dpts(b) <= -0.02),
    ("Domina en sets (últ. 10) y NO favorito", lambda b: b["msets"] - b["msetsr"] >= 0.6 and not b["fav"]),
    ("Racha de derrotas pero buen margen de puntos", lambda b: b["racha"] <= -2 and b["mpts"] >= 0),
    ("Racha de victorias pero mal margen de puntos (contra)",
     lambda b: b["rachar"] >= 2 and b["mptsr"] <= 0),
    # --- rachas
    ("Racha ≥3 victorias", lambda b: b["racha"] >= 3),
    ("Racha ≥3 derrotas (rebote)", lambda b: b["racha"] <= -3),
    ("Rival con racha ≥3 derrotas", lambda b: b["rachar"] <= -3),
    ("Rival con racha ≥4 victorias (contra)", lambda b: b["rachar"] >= 4),
    # --- revancha el mismo dia
    ("Revancha: perdió con él antes hoy", lambda b: b["rev"] == 0),
    ("Revancha: le ganó antes hoy", lambda b: b["rev"] == 1),
    # --- descanso
    ("Rival sin jugar ≥3 días", lambda b: b["descr"] is not None and b["descr"] >= 3),
    ("Sin jugar ≥3 días", lambda b: b["desc"] is not None and b["desc"] >= 3),
    # --- Elo frente a la casa
    ("Elo ve ≥8 puntos más que la casa", lambda b: b["dif"] >= 0.08),
    ("Elo ve ≥15 puntos más que la casa", lambda b: b["dif"] >= 0.15),
    ("Elo ve ≥8 más y además favorito", lambda b: b["dif"] >= 0.08 and b["fav"]),
    ("Elo y puntos coinciden contra la casa", lambda b: b["dif"] >= 0.08 and dpts(b) >= 0.03),
    # --- hora
    ("Favorito antes de las 12:00", lambda b: b["hora"] < 12 and b["fav"]),
    ("No favorito antes de las 12:00", lambda b: b["hora"] < 12 and not b["fav"]),
    ("Favorito desde las 20:00", lambda b: b["hora"] >= 20 and b["fav"]),
    ("No favorito desde las 20:00", lambda b: b["hora"] >= 20 and not b["fav"]),
]


def stats(bs):
    n = len(bs)
    if not n:
        return None
    x = np.array([b["ben"] for b in bs])
    y = x.mean()
    se = x.std() / math.sqrt(n) if n > 1 else 1.0
    return dict(n=n, win=sum(b["gana"] for b in bs) / n, c=sum(b["c"] for b in bs) / n,
                u=x.sum(), y=y, lo=y - Z90 * se, hi=y + Z90 * se)


def fmt(s):
    if not s:
        return "0 picks"
    return (f"{s['n']} picks · win {s['win'] * 100:.0f}% · cuota {s['c']:.2f} · {s['u']:+.1f} u · "
            f"YIELD {s['y'] * 100:+.1f}% ({s['lo'] * 100:+.0f}% a {s['hi'] * 100:+.0f}%)")


def veredicto_conf(sc):
    if not sc or sc["n"] < MIN_CONF:
        return "⏳ pocos datos en confirmación"
    if sc["y"] <= 0:
        return "🔴 NO SE CONFIRMA"
    if sc["lo"] > 0:
        return "🟢 CONFIRMADO"
    return "🟡 GANA, PERO AÚN DUDOSO"


def veredicto(se, sc):
    """se = exploracion, sc = confirmacion."""
    if not se or se["n"] < MIN_EXPL or se["lo"] <= 0:
        return None                                   # ni siquiera pasa la exploracion
    return veredicto_conf(sc)


# ============================================================ modelo de IA
def logit(p):
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def nz(v, d=-1.0, cap=None):
    if v is None:
        return d
    return min(v, cap) if cap else v


def vec_ml(f):
    """Variables del partido desde el punto de vista del jugador A."""
    ia, ib = 1 / f["fin_a"], 1 / f["fin_b"]
    la, lb = f["la"], f["lb"]
    return [logit(ia / (ia + ib)), logit(f["p_elo"]), f["forma_a"] - f["forma_b"],
            (f["h2h_a"] - f["h2h_b"]) / (f["h2h_a"] + f["h2h_b"] + 2), f["h2h_a"] + f["h2h_b"],
            f["hoy_j_a"], f["hoy_j_b"], f["hoy_g_a"], f["hoy_g_b"],
            la["racha"], lb["racha"], nz(la["ult"]), nz(lb["ult"]),
            la["m_pts"], lb["m_pts"], la["m_sets"], lb["m_sets"],
            nz(la["descanso"], cap=30), nz(lb["descanso"], cap=30),
            nz(la["min_ult"], cap=600), nz(lb["min_ult"], cap=600),
            nz(la["revancha"]), datetime.fromtimestamp(f["t"], TZ).hour, ia + ib - 1]


def modelo_ia(filas, inicio_ts, corte_ts):
    """Probabilidad del modelo para cada partido con cuota, entrenando solo con
    partidos anteriores. Devuelve las filas con 'p_ia'."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    con = sorted([f for f in filas if f.get("fin_a")], key=lambda x: x["t"])
    if len(con) < 2000:
        return []
    X = np.array([vec_ml(f) for f in con])
    y = np.array([f["gana_a"] for f in con])
    ts = np.array([f["t"] for f in con])
    t = inicio_ts + ML_ARRANQUE * 86400
    fin = ts.max() + 1
    while t < fin:
        sig = t + ML_CADA * 86400
        tr = ts < t
        bl = (ts >= t) & (ts < sig)
        if bl.any() and tr.sum() >= 1500:
            m = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=15,
                                               min_samples_leaf=80, l2_regularization=1.0,
                                               random_state=0)
            m.fit(X[tr], y[tr])
            for i, p in zip(np.where(bl)[0], m.predict_proba(X[bl])[:, 1]):
                con[i]["p_ia"] = float(p)
        t = sig
    return [f for f in con if "p_ia" in f]


def picks_ia(filas, umbral):
    out = []
    for f in filas:
        best = None
        for s in ("a", "b"):
            c = f[f"fin_{s}"]
            if not (MIN_ODD <= c <= MAX_ODD):
                continue
            p = f["p_ia"] if s == "a" else 1 - f["p_ia"]
            v = p * c - 1
            if v >= umbral and (best is None or v > best[1]):
                best = (s, v, c)
        if best:
            s, v, c = best
            gana = f["gana_a"] if s == "a" else 1 - f["gana_a"]
            out.append(dict(t=f["t"], c=c, gana=gana, ben=(c - 1) if gana else -1.0))
    return out


def seccion_ia(L, filas, inicio_ts, corte_ts):
    L.append("🤖 MODELO DE IA (corrige la cuota de la casa con todas las variables)")
    con = modelo_ia(filas, inicio_ts, corte_ts)
    if not con:
        L.append("   Pocos partidos con cuota para entrenarlo.")
        return None
    co = [f for f in con if f["t"] >= corte_ts]
    if co:
        ys = np.array([f["gana_a"] for f in co])
        pi = np.clip([f["p_ia"] for f in co], 1e-6, 1 - 1e-6)
        pm = np.clip([(1 / f["fin_a"]) / (1 / f["fin_a"] + 1 / f["fin_b"]) for f in co], 1e-6, 1 - 1e-6)

        def ll(p):
            return float(-np.mean(ys * np.log(p) + (1 - ys) * np.log(1 - p)))
        a, b = ll(pi), ll(pm)
        L.append(f"   Precisión en confirmación (menos es mejor): IA {a:.4f} · casa {b:.4f} "
                 + ("✅ la IA afina más que la casa" if a < b else "⚠️ la casa afina más"))
    mejor = None
    for u in ML_UMBRALES:
        pk = picks_ia(con, u)
        se = stats([p for p in pk if p["t"] < corte_ts])
        sc = stats([p for p in pk if p["t"] >= corte_ts])
        ok = se and se["n"] >= MIN_EXPL and se["lo"] > 0
        L.append(f"   ▶ valor ≥{u * 100:.0f}%" + ("  ← pasa la exploración" if ok else ""))
        L.append(f"      Exploración:  {fmt(se)}")
        L.append(f"      Confirmación: {fmt(sc)}")
        if ok and (mejor is None or se["y"] > mejor[1]["y"]):
            mejor = (u, se, sc)
    if mejor:
        u, se, sc = mejor
        v = veredicto_conf(sc)
        L.append(f"   Umbral elegido en exploración: valor ≥{u * 100:.0f}% → {v}")
        return (f"🤖 IA valor ≥{u * 100:.0f}%", se, sc, v)
    L.append("   Ningún umbral gana con claridad en la exploración.")
    return None


# ================================================================== informe
def analizar(lid):
    nombre = LIGAS[lid]
    ms, per = bt.load(lid)
    inicio_ts = bt.ts_of(per["inicio"])
    fin_ts = bt.ts_of(per["fin"] + timedelta(days=1))
    corte_ts = inicio_ts + (fin_ts - inicio_ts) * 2 / 3
    filas = construir(ms, inicio_ts)
    L = [f"🔎 {nombre.upper()} · búsqueda profunda de patrones",
         f"Exploración: {per['inicio']:%d/%m} – {datetime.fromtimestamp(corte_ts, TZ):%d/%m} · "
         f"Confirmación (no usada para buscar): {datetime.fromtimestamp(corte_ts, TZ):%d/%m} – {per['fin']:%d/%m}",
         "Decisión 2 h antes · cuota de Sofascore · 1 u por pick", ""]

    cu = bt.Cuotas(lid)
    for f in sorted(filas, key=lambda x: -x["t"]):
        o = cu.get(f["id"])
        if o:
            f["ini_a"], f["fin_a"], f["ini_b"], f["fin_b"] = o
    print(f"[{nombre}] cuotas nuevas {cu.nuevas} · pendientes {cu.cupo}")
    if cu.cupo:
        L.append(f"⏳ SIN TERMINAR: faltan cuotas de {cu.cupo} de {len(filas)} partidos. "
                 "Vuelve a lanzarlo: sigue donde lo dejó.")
        return L, []

    bs = apuestas(filas)
    ex = [b for b in bs if b["t"] < corte_ts]
    co = [b for b in bs if b["t"] >= corte_ts]
    con_c = sum(1 for f in filas if f.get("fin_a"))
    L.append(f"{len(filas)} partidos · {con_c} con cuota · margen medio de la casa "
             f"{(sum(1 / f['fin_a'] + 1 / f['fin_b'] for f in filas if f.get('fin_a')) / max(1, con_c) - 1) * 100:.1f}%")
    L.append(f"Referencia, apostar a todo: {fmt(stats(bs))}")
    L.append("")

    filas_csv = []
    resultados = []
    for nombre_p, cond in PATRONES:
        se = stats([b for b in ex if cond(b)])
        sc = stats([b for b in co if cond(b)])
        v = veredicto(se, sc)
        resultados.append((nombre_p, se, sc, v))
        filas_csv.append([nombre_p, fmt(se), fmt(sc), v or "no pasa exploración"])

    # --- jugadores que la casa paga mal (se eligen en exploracion, se prueban despues)
    por_j = defaultdict(list)
    for b in ex:
        por_j[b["jugador"]].append(b)
    buenos, malos = set(), set()
    for j, lista in por_j.items():
        s = stats(lista)
        if s["n"] >= MIN_JUG and s["lo"] > 0:
            buenos.add(j)
        if s["n"] >= MIN_JUG and s["hi"] < 0:
            malos.add(j)
    for nombre_p, cond in (
            (f"Jugadores infravalorados ({len(buenos)})", lambda b: b["jugador"] in buenos),
            (f"Contra jugadores sobrevalorados ({len(malos)})", lambda b: b["rival"] in malos)):
        if nombre_p.endswith("(0)"):
            continue
        se = stats([b for b in ex if cond(b)])
        sc = stats([b for b in co if cond(b)])
        v = veredicto_conf(sc)        # elegidos a dedo en exploracion: solo cuenta confirmar
        resultados.append((nombre_p, se, sc, v))
        filas_csv.append([nombre_p, fmt(se), fmt(sc), v])

    pasan = [r for r in resultados if r[3]]
    orden = {"🟢": 0, "🟡": 1, "⏳": 2, "🔴": 3}
    pasan.sort(key=lambda r: (orden[r[3][0]], -(r[2]["y"] if r[2] else -9)))
    L.append(f"📋 PATRONES · probados: {len(resultados)} · pasan la exploración: {len(pasan)}")
    if not pasan:
        L.append("   Ningún patrón gana con claridad en la exploración.")
    for nombre_p, se, sc, v in pasan:
        L.append(f"▶ {nombre_p}")
        L.append(f"   {v}")
        L.append(f"   Exploración:  {fmt(se)}")
        L.append(f"   Confirmación: {fmt(sc)}")
    L.append("")

    try:
        ia = seccion_ia(L, filas, inicio_ts, corte_ts)
    except Exception as ex_:
        L.append(f"   (no se pudo entrenar el modelo: {ex_})")
        ia = None
    if ia:
        filas_csv.append([ia[0], fmt(ia[1]), fmt(ia[2]), ia[3]])
        pasan.append(ia)
    L.append("")

    verdes = [r for r in pasan if r[3] and r[3].startswith("🟢")]
    amar = [r for r in pasan if r[3] and r[3].startswith("🟡")]
    if verdes:
        L.append("CONCLUSIÓN: confirmado(s): " + " · ".join(r[0] for r in verdes)
                 + ". Siguiente paso: seguimiento en real.")
    elif amar:
        L.append("CONCLUSIÓN: nada confirmado del todo; a vigilar: " + " · ".join(r[0] for r in amar))
    else:
        L.append("CONCLUSIÓN: ningún patrón se confirma en los meses no usados para buscar. "
                 "Mejor no apostar en esta liga con estos datos.")
    return L, filas_csv


def main():
    if not mb_api.KEY:
        raise SystemExit("Falta ZYLA_API_KEY")
    lid = os.getenv("LIGA", "19039")
    bt.INICIO = time.time()
    bt.tg_msg(f"🔎 Empieza la búsqueda profunda de patrones en {LIGAS[lid]}. Tardará un rato.")
    L, filas_csv = analizar(lid)
    txt = "\n".join(L)
    print(txt)
    out = os.path.join(bt.OUT_ROOT, bt.slug(lid))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "patrones.txt"), "w") as fh:
        fh.write(txt + "\n")
    bt.tg_msg(txt)
    if filas_csv:
        path = os.path.join(out, "patrones.csv")
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["patron", "exploracion", "confirmacion", "veredicto"])
            w.writerows(filas_csv)
        bt.tg_file(path)


if __name__ == "__main__":
    main()
