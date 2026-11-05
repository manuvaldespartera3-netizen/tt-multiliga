"""
mb_explorar.py - Busca patrones ganadores en una liga y los CONFIRMA en meses
que no se han usado para buscarlos.

Por que asi: si se prueban muchos patrones sobre los mismos datos, alguno sale
"ganador" por pura suerte. Por eso el periodo se parte en dos:
  EXPLORACION  (primeros 2/3)  se prueban todos los patrones
  CONFIRMACION (ultimo 1/3)    solo se mira como les va a los que pasaron
Un patron solo vale si gana en las dos partes.

Todo se calcula con lo que se sabia 2 horas antes de cada partido.

Familias de patrones (fijadas de antemano, no se inventan mirando los datos):
  • Rango de cuota (favoritos / no favoritos) y local / visitante
  • H2H de los ultimos 90 dias
  • Racha y lo que lleva hoy cada jugador (ultimo partido del dia, cansancio)
  • Revancha: se vuelven a enfrentar el mismo dia
  • Descanso: dias sin jugar
  • Elo frente a la casa (nivel que la cuota no refleja)
  • Hora del dia
  • Jugadores concretos que la casa paga de mas o de menos

Variables (ademas de las de mb_backtest): LIGA (por defecto 19039 = Liga Pro),
BT_DAYS (150), BT_MAX_MIN (290), BT_PACE
"""
import csv
import math
import os
import time
from collections import defaultdict
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


# ============================================================ caracteristicas
class EstadoX(Estado):
    """Estado del backtest + racha, ultimo partido de hoy, descanso y revanchas."""

    def __init__(self):
        super().__init__()
        self.ult_t = {}
        self.racha = defaultdict(int)
        self.ult_hoy = {}
        self.vs_hoy = {}

    def aplicar(self, m):
        super().aplicar(m)
        a, b = m["ida"], m["idb"]
        wa = m["final"][0] > m["final"][1]
        dk = bt.day_key(m["t"])
        for p, o, w in ((a, b, wa), (b, a, not wa)):
            self.ult_t[p] = m["t"]
            r = self.racha[p]
            self.racha[p] = (max(r, 0) + 1) if w else (min(r, 0) - 1)
            self.ult_hoy[p] = (dk, int(w))
            self.vs_hoy[(dk, p, o)] = int(w)

    def lado(self, p, o, t):
        dk = bt.day_key(t)
        u = self.ult_hoy.get(p)
        ult = u[1] if u and u[0] == dk else None
        lt = self.ult_t.get(p)
        return dict(ult=ult, racha=self.racha[p],
                    descanso=(t - lt) / 86400 if lt else None,
                    revancha=self.vs_hoy.get((dk, p, o)))


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
                t=f["t"], c=c, cr=cr, fav=c < cr, local=s == "a",
                pelo=pe, pmkt=pm, dif=pe - pm,
                h2h=f[f"h2h_{s}"], h2hr=f[f"h2h_{o}"],
                hj=f[f"hoy_j_{s}"], hjr=f[f"hoy_j_{o}"],
                ult=ls["ult"], ultr=lo["ult"], racha=ls["racha"], rachar=lo["racha"],
                desc=ls["descanso"], descr=lo["descanso"], rev=ls["revancha"],
                hora=datetime.fromtimestamp(f["t"], TZ).hour,
                jugador=f[s], rival=f[o], gana=gana, ben=(c - 1) if gana else -1.0))
    return out


# =================================================================== patrones
def rango(lo, hi):
    return lambda b: lo <= b["c"] < hi


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


def veredicto(se, sc):
    """se = exploracion, sc = confirmacion."""
    if not se or se["n"] < MIN_EXPL or se["lo"] <= 0:
        return None                                   # ni siquiera pasa la exploracion
    if not sc or sc["n"] < MIN_CONF:
        return "⏳ pocos datos en confirmación"
    if sc["y"] <= 0:
        return "🔴 NO SE CONFIRMA"
    if sc["lo"] > 0:
        return "🟢 CONFIRMADO"
    return "🟡 GANA, PERO AÚN DUDOSO"


# ================================================================== informe
def analizar(lid):
    nombre = LIGAS[lid]
    ms, per = bt.load(lid)
    inicio_ts = bt.ts_of(per["inicio"])
    fin_ts = bt.ts_of(per["fin"] + timedelta(days=1))
    corte_ts = inicio_ts + (fin_ts - inicio_ts) * 2 / 3
    filas = construir(ms, inicio_ts)
    L = [f"🔎 {nombre.upper()} · búsqueda de patrones",
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
        filas_csv.append([nombre_p, *(fmt(s) for s in (se, sc)), v or "no pasa exploración"])

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
        # aqui la exploracion esta elegida a dedo: solo cuenta la confirmacion
        v = ("⏳ pocos datos en confirmación" if not sc or sc["n"] < MIN_CONF else
             "🟢 CONFIRMADO" if sc["lo"] > 0 else
             "🟡 GANA, PERO AÚN DUDOSO" if sc["y"] > 0 else "🔴 NO SE CONFIRMA")
        resultados.append((nombre_p, se, sc, v))
        filas_csv.append([nombre_p, fmt(se), fmt(sc), v])

    pasan = [r for r in resultados if r[3]]
    orden = {"🟢": 0, "🟡": 1, "⏳": 2, "🔴": 3}
    pasan.sort(key=lambda r: (orden[r[3][0]], -(r[2]["y"] if r[2] else -9)))
    L.append(f"Patrones probados: {len(resultados)} · pasan la exploración: {len(pasan)}")
    L.append("")
    if not pasan:
        L.append("❌ Ningún patrón gana con claridad en la exploración.")
    for nombre_p, se, sc, v in pasan:
        L.append(f"▶ {nombre_p}")
        L.append(f"   {v}")
        L.append(f"   Exploración:  {fmt(se)}")
        L.append(f"   Confirmación: {fmt(sc)}")
    L.append("")
    verdes = [r for r in pasan if r[3].startswith("🟢")]
    L.append("CONCLUSIÓN: " + (
        f"{len(verdes)} patrón(es) confirmado(s). Siguiente paso: seguimiento en real."
        if verdes else
        "ningún patrón se confirma en los meses no usados para buscar. "
        "Mejor no apostar en esta liga con estos datos."))
    return L, filas_csv


def main():
    if not mb_api.KEY:
        raise SystemExit("Falta ZYLA_API_KEY")
    lid = os.getenv("LIGA", "19039")
    bt.INICIO = time.time()
    bt.tg_msg(f"🔎 Empieza la búsqueda de patrones en {LIGAS[lid]}. Tardará un rato.")
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
