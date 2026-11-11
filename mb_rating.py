"""
mb_rating.py - Liga Pro Checa: ¿el RATING OFICIAL (tt.league-pro.com) sabe algo
que la cuota de la casa no recoge?

1. Descarga los torneos de la web oficial (cada uno trae el rating de cada
   jugador ANTES del torneo y los partidos con su hora y resultado). Se guardan
   en cache: si se corta, la siguiente vez sigue donde lo dejo.
2. Los une con los partidos y cuotas de Sofascore que ya tenemos (mismos
   jugadores y misma hora).
3. Mide, con el metodo de siempre (buscar en 2/3, confirmar en el ultimo 1/3):
   • Modelo RATING: probabilidad solo con la diferencia de rating
   • Modelo RATING + CASA: parte de la cuota y la corrige con el rating
   • Patrones: "el rating le ve mejor pero la casa le hace no favorito", etc.

Variables: ZYLA_API_KEY, TELEGRAM_TOKEN, MY_CHAT_ID, BT_DAYS (150),
           BT_MAX_MIN (290), BT_PACE, LP_START_ID (opcional), LP_PACE (1.2)
"""
import json
import math
import os
import re
import time
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

os.environ.setdefault("LIGA", "19039")
os.environ.setdefault("BT_DAYS", "150")

import numpy as np

import mb_api
import mb_backtest as bt
from mb_api import TZ, LIGAS
from mb_backtest import MIN_ODD, MAX_ODD, Z90
from mb_explorar import stats, fmt, veredicto, veredicto_conf, MIN_EXPL

LID = "19039"
BASE = "https://tt.league-pro.com"
LP_PACE = float(os.getenv("LP_PACE", "1.2"))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Accept-Language": "en"}
UMBRALES = [0.03, 0.05, 0.08, 0.12, 0.20]


# ============================================================ web oficial
def nuxt(html):
    """Lee los datos que la web lleva dentro (formato Nuxt)."""
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    d = json.loads(m.group(1))
    memo = {}
    envoltorios = ("Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef",
                   "EmptyShallowRef", "NuxtError")

    def r(i):
        if i in memo:
            return memo[i]
        v = d[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in envoltorios:
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
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def ultimo_id():
    if os.getenv("LP_START_ID"):
        return int(os.getenv("LP_START_ID"))
    hoy = datetime.now(TZ).date()
    html = bajar(f"{BASE}/en/tournaments?date_from={hoy}&date_to={hoy + timedelta(days=2)}")
    ids = [int(x) for x in re.findall(r'"id"\s*:\s*(\d{5,6})', html) + re.findall(r"/tournaments/(\d{5,6})", html)]
    root = nuxt(html)
    if root:
        ids += [int(x) for x in re.findall(r'"id": (\d{5,6})', json.dumps(root))]
    return max(ids) + 20


def torneo(tid):
    """Torneo compacto: inicio, jugadores con su rating previo y partidos."""
    html = bajar(f"{BASE}/en/tournaments/{tid}")
    root = nuxt(html)
    try:
        pd = root["data"]["tournament-page"]["pageData"]
        t, w = pd["tournament"], pd["widgets"]
    except (TypeError, KeyError):
        return {"id": tid, "vacio": True}
    lados = {}
    for s in w.get("sides") or []:
        p = s.get("player") or {}
        if not p:
            continue
        lados[str(s["id"])] = dict(nombre=p.get("surname_en", ""), pila=p.get("first_name_en", ""),
                                   pid=p.get("id"), rating=s.get("rating_before_tournament"))
    partidos = []
    for m in w.get("matches") or []:
        res = m.get("results") or {}
        if m.get("status") != 3 or res.get("score_one") is None:
            continue
        partidos.append(dict(t=m.get("start_game"), a=str(m.get("side_one_id")),
                             b=str(m.get("side_two_id")), sa=res.get("score_one"), sb=res.get("score_two")))
    return {"id": tid, "nombre": t.get("name_en"), "inicio": t.get("start_date"),
            "lados": lados, "partidos": partidos}


def descargar(desde_ts):
    """Recorre los torneos hacia atras desde el ultimo hasta la fecha pedida."""
    cdir = bt.cache_dir(LID)
    tid = ultimo_id()
    viejos = fallos = nuevos = 0
    pendiente = False
    while viejos < 150 and fallos < 300:
        if time.time() - bt.INICIO > bt.MAX_MIN * 60:
            pendiente = True
            break
        path = os.path.join(cdir, f"lp_{tid}.json")
        if os.path.exists(path):
            with open(path) as f:
                tr = json.load(f)
        else:
            try:
                tr = torneo(tid)
            except Exception as ex:
                print("WARN torneo", tid, ex)
                tr = {"id": tid, "vacio": True, "error": str(ex)[:80]}
            nuevos += 1
            time.sleep(LP_PACE)
            if not tr.get("error"):
                with open(path, "w") as f:
                    json.dump(tr, f)
        if tr.get("vacio"):
            fallos += 1
        else:
            fallos = 0
            ini = datetime.fromisoformat(tr["inicio"].replace("Z", "+00:00")).timestamp()
            viejos = viejos + 1 if ini < desde_ts else 0
        tid -= 1
    print(f"torneos nuevos descargados: {nuevos} · ultimo id mirado {tid}")
    return pendiente


def cargar_lp():
    cdir = bt.cache_dir(LID)
    out = []
    for fn in os.listdir(cdir):
        if fn.startswith("lp_"):
            with open(os.path.join(cdir, fn)) as f:
                tr = json.load(f)
            if not tr.get("vacio"):
                out.append(tr)
    return out


# ================================================================== union
def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z ]", " ", s).split()


def clave_sofa(nombre):
    """'Dedek J.' -> 'dedek j' · 'Velky Junior D.' -> 'velky junior d'."""
    w = norm(nombre)
    if len(w) >= 2 and len(w[-1]) == 1:
        return " ".join(w[:-1]) + " " + w[-1]
    if len(w) >= 2:                                    # 'Jakub Lysek' (nombre apellido)
        return w[-1] + " " + w[0][0]
    return " ".join(w)


def clave_lp(lado):
    ap = " ".join(norm(lado["nombre"]))
    pila = norm(lado["pila"])
    return ap + (" " + pila[0][0] if pila else "")


def indexar_lp(torneos):
    idx = defaultdict(list)
    for tr in torneos:
        L = tr["lados"]
        for m in tr["partidos"]:
            if m["a"] not in L or m["b"] not in L or not m["t"]:
                continue
            la, lb = L[m["a"]], L[m["b"]]
            if la["rating"] is None or lb["rating"] is None:
                continue
            t = datetime.fromisoformat(m["t"].replace("Z", "+00:00")).timestamp()
            ka, kb = clave_lp(la), clave_lp(lb)
            idx[frozenset((ka, kb))].append(dict(t=t, ka=ka, kb=kb, ra=la["rating"], rb=lb["rating"],
                                                  sa=m["sa"], sb=m["sb"]))
    return idx


def unir(ms, idx):
    """A cada partido de Sofascore le pone el rating oficial de cada jugador."""
    unidos = []
    for m in ms:
        ka, kb = clave_sofa(m["a"]), clave_sofa(m["b"])
        cands = idx.get(frozenset((ka, kb)), [])
        best = None
        for c in cands:
            dt = abs(c["t"] - m["t"])
            if dt <= 45 * 60 and (best is None or dt < abs(best["t"] - m["t"])):
                best = c
        if not best:
            continue
        ra, rb = (best["ra"], best["rb"]) if best["ka"] == ka else (best["rb"], best["ra"])
        sa, sb = (best["sa"], best["sb"]) if best["ka"] == ka else (best["sb"], best["sa"])
        if (sa > sb) != (m["final"][0] > m["final"][1]):
            continue                                   # no cuadra el ganador: no es el mismo partido
        unidos.append(dict(m, ra=ra, rb=rb))
    return unidos


# =============================================================== modelos
def logit(p):
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def ajustar(X, y, l2=0.5, iters=4000, lr=0.3):
    X = np.asarray(X, float)
    y = np.asarray(y, float)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    w = np.zeros(X.shape[1])
    b = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Z @ w + b, -30, 30)))
        e = p - y
        w -= lr * (Z.T @ e / len(y) + l2 * w / len(y))
        b -= lr * e.mean()
    return lambda Xn: 1 / (1 + np.exp(-np.clip(((np.asarray(Xn, float) - mu) / sd) @ w + b, -30, 30)))


def lados(f):
    """Las dos apuestas posibles de un partido, con sus variables."""
    ia, ib = 1 / f["fin_a"], 1 / f["fin_b"]
    pa = ia / (ia + ib)
    out = []
    for s in ("a", "b"):
        c, cr = (f["fin_a"], f["fin_b"]) if s == "a" else (f["fin_b"], f["fin_a"])
        rd = (f["ra"] - f["rb"]) if s == "a" else (f["rb"] - f["ra"])
        gana = f["gana_a"] if s == "a" else 1 - f["gana_a"]
        out.append(dict(t=f["t"], c=c, fav=c < cr, rd=rd, pm=pa if s == "a" else 1 - pa,
                        gana=gana, ben=(c - 1) if gana else -1.0))
    return out


def analizar():
    ms, per = bt.load(LID)
    inicio_ts = bt.ts_of(per["inicio"])
    fin_ts = bt.ts_of(per["fin"] + timedelta(days=1))
    corte = inicio_ts + (fin_ts - inicio_ts) * 2 / 3
    ms = [m for m in ms if m["t"] >= inicio_ts]
    L = ["🏅 LIGA PRO · RATING OFICIAL frente a la cuota de la casa",
         f"Buscar: {per['inicio']:%d/%m} – {datetime.fromtimestamp(corte, TZ):%d/%m} · "
         f"Confirmar (no usado para buscar): {datetime.fromtimestamp(corte, TZ):%d/%m} – {per['fin']:%d/%m}",
         "Rating = el de la web oficial ANTES de cada torneo · cuota de Sofascore · 1 u por pick", ""]

    pendiente = descargar(inicio_ts - 3 * 86400)
    torneos = cargar_lp()
    idx = indexar_lp(torneos)
    unidos = unir(ms, idx)
    L.append(f"{len(torneos)} torneos de la web · {len(ms)} partidos de Sofascore · "
             f"unidos con su rating: {len(unidos)} ({len(unidos) / max(1, len(ms)) * 100:.0f}%)")
    if pendiente:
        L.append("⏳ SIN TERMINAR: faltan torneos por descargar. Vuelve a lanzarlo: sigue donde lo dejó.")
        return L

    cu = bt.Cuotas(LID)
    for f in unidos:
        o = cu.get(f["id"])
        if o:
            f["ini_a"], f["fin_a"], f["ini_b"], f["fin_b"] = o
        f["gana_a"] = int(f["final"][0] > f["final"][1])
    if cu.cupo:
        L.append(f"⏳ SIN TERMINAR: faltan cuotas de {cu.cupo} partidos. Vuelve a lanzarlo.")
        return L
    con = [f for f in unidos if f.get("fin_a")]
    L.append(f"Con cuota: {len(con)}")
    if len(con) < 1000:
        L.append("❌ Muy pocos partidos unidos con cuota para sacar conclusiones.")
        return L
    ex = [f for f in con if f["t"] < corte]
    co = [f for f in con if f["t"] >= corte]

    # --- ¿cuanto sabe el rating por si solo?
    ys_ex = [f["gana_a"] for f in ex]
    ys_co = np.array([f["gana_a"] for f in co])
    m_r = ajustar([[f["ra"] - f["rb"]] for f in ex], ys_ex)
    m_rc = ajustar([[logit((1 / f["fin_a"]) / (1 / f["fin_a"] + 1 / f["fin_b"])), f["ra"] - f["rb"]] for f in ex], ys_ex)
    pr = np.clip(m_r([[f["ra"] - f["rb"]] for f in co]), 1e-6, 1 - 1e-6)
    prc = np.clip(m_rc([[logit((1 / f["fin_a"]) / (1 / f["fin_a"] + 1 / f["fin_b"])), f["ra"] - f["rb"]] for f in co]), 1e-6, 1 - 1e-6)
    pm = np.clip([(1 / f["fin_a"]) / (1 / f["fin_a"] + 1 / f["fin_b"]) for f in co], 1e-6, 1 - 1e-6)

    def ll(p):
        return float(-np.mean(ys_co * np.log(p) + (1 - ys_co) * np.log(1 - p)))

    def acierto(p):
        return float(np.mean((np.asarray(p) >= 0.5) == (ys_co == 1)))
    L.append("")
    L.append("🎯 PRECISIÓN en los meses de confirmación (menos es mejor)")
    L.append(f"   Casa {ll(pm):.4f} ({acierto(pm) * 100:.1f}% aciertos) · Solo rating {ll(pr):.4f} "
             f"({acierto(pr) * 100:.1f}%) · Rating + casa {ll(prc):.4f}")
    L.append("   " + ("✅ el rating AÑADE información a la cuota" if ll(prc) < ll(pm) - 0.0005 else
                      "⚠️ el rating no mejora la cuota de la casa"))
    L.append("")

    resultados = []

    def bloque(nombre, modelo, xf):
        L.append(f"🤖 {nombre}")
        mejor = None
        for u in UMBRALES:
            def picks(fs):
                out = []
                for f in fs:
                    pa = float(modelo([xf(f)])[0])
                    best = None
                    for s, p in (("a", pa), ("b", 1 - pa)):
                        c = f[f"fin_{s}"]
                        if MIN_ODD <= c <= MAX_ODD and p * c - 1 >= u and (best is None or p * c > best[0]):
                            g = f["gana_a"] if s == "a" else 1 - f["gana_a"]
                            best = (p * c, dict(t=f["t"], c=c, gana=g, ben=(c - 1) if g else -1.0))
                    if best:
                        out.append(best[1])
                return out
            se, sc = stats(picks(ex)), stats(picks(co))
            ok = se and se["n"] >= MIN_EXPL and se["lo"] > 0
            L.append(f"   ▶ valor ≥{u * 100:.0f}%" + ("  ← pasa la búsqueda" if ok else ""))
            L.append(f"      Buscar:    {fmt(se)}")
            L.append(f"      Confirmar: {fmt(sc)}")
            if ok and (mejor is None or se["y"] > mejor[1]["y"]):
                mejor = (u, se, sc)
        if mejor:
            v = veredicto_conf(mejor[2])
            L.append(f"   Umbral elegido al buscar: ≥{mejor[0] * 100:.0f}% → {v}")
            resultados.append((f"{nombre} valor ≥{mejor[0] * 100:.0f}%", v))
        L.append("")

    bloque("SOLO RATING", m_r, lambda f: [f["ra"] - f["rb"]])
    bloque("RATING + CASA", m_rc, lambda f: [logit((1 / f["fin_a"]) / (1 / f["fin_a"] + 1 / f["fin_b"])), f["ra"] - f["rb"]])

    # --- patrones sencillos de rating frente a cuota
    bex = [b for f in ex for b in lados(f) if MIN_ODD <= b["c"] <= MAX_ODD]
    bco = [b for f in co for b in lados(f) if MIN_ODD <= b["c"] <= MAX_ODD]
    pat = [(f"Rating ≥{x} más y NO favorito", lambda b, x=x: b["rd"] >= x and not b["fav"]) for x in (0, 20, 40, 60)]
    pat += [(f"Rating ≥{x} más y favorito", lambda b, x=x: b["rd"] >= x and b["fav"]) for x in (40, 80)]
    L.append("📋 PATRONES de rating frente a la cuota")
    for nombre, cond in pat:
        se = stats([b for b in bex if cond(b)])
        sc = stats([b for b in bco if cond(b)])
        v = veredicto(se, sc)
        L.append(f"▶ {nombre} → {v or 'no pasa la búsqueda'}")
        L.append(f"   Buscar:    {fmt(se)}")
        L.append(f"   Confirmar: {fmt(sc)}")
        if v:
            resultados.append((nombre, v))
    L.append("")
    verdes = [r for r in resultados if r[1].startswith("🟢")]
    amar = [r for r in resultados if r[1].startswith("🟡")]
    L.append("CONCLUSIÓN: " + (
        "confirmado: " + " · ".join(r[0] for r in verdes) + ". Siguiente paso: seguimiento en real."
        if verdes else ("a vigilar: " + " · ".join(r[0] for r in amar)) if amar else
        "el rating oficial no da ventaja frente a la cuota en los meses de confirmación."))
    return L


def main():
    if not mb_api.KEY:
        raise SystemExit("Falta ZYLA_API_KEY")
    bt.INICIO = time.time()
    bt.tg_msg("🏅 Empieza el análisis del rating oficial de Liga Pro. Tardará un rato.")
    L = analizar()
    txt = "\n".join(L)
    print(txt)
    out = os.path.join(bt.OUT_ROOT, bt.slug(LID))
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "rating.txt"), "w") as fh:
        fh.write(txt + "\n")
    bt.tg_msg(txt)


if __name__ == "__main__":
    main()
