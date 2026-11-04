"""
mb_backtest.py - Backtest del bot multiliga (TT Cup, Liga Pro Checa, Elite Series).

Para CADA partido calcula la probabilidad de cada jugador con lo que se sabia
2 horas antes de empezar (CUTOFF_H), sin mirar nunca el futuro:
  - Elo dinamico, actualizado partido a partido (tambien los de ese mismo dia)
  - Forma ajustada: ultimos 10 partidos, ganados/perdidos frente a lo esperado
  - Lo que lleva hoy cada jugador (partidos y victorias del dia)
  - H2H de los ultimos 90 dias
Despues lo compara con la cuota: solo hay pick si
    valor = probabilidad x cuota - 1  >=  umbral
y calcula win rate, beneficio y YIELD por umbral, por rango de cuota y por mitades.

Honestidad del test:
  - El modelo se ajusta con la 1.a mitad del periodo y se mide SOLO en la 2.a
    mitad (partidos que no ha visto), que es lo que pasaria en real.
  - Cuota usada: la final de Sofascore (justo antes de empezar). Tambien se
    muestra con la de apertura como referencia.

Variables:
  ZYLA_API_KEY                 clave de Zyla (secreto)
  TELEGRAM_TOKEN, MY_CHAT_ID   opcionales: si estan, manda el informe
  LIGA        15005 | 19039 | 19041 | todas   (por defecto: todas)
  BT_DAYS     dias analizados (por defecto 60; la 2.a mitad es el test)
  BT_WARMUP   dias previos solo para calentar Elo/forma/H2H (por defecto 45)
  CUTOFF_H    horas antes del partido con las que se decide (por defecto 2)
  MAX_ODDS    consultas de cuotas maximas por ejecucion y liga (por defecto 4000)
  BT_PACE     segundos entre consultas (por defecto 1.2 = 50/min, deja hueco
              de sobra al avisador dentro del limite de 120/min)
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

import mb_api
from mb_api import LIGAS, TZ, parse, day_events, odds_raw, odds_from

LIGA = os.getenv("LIGA", "todas").strip() or "todas"
DAYS = int(os.getenv("BT_DAYS", "60"))
WARMUP = int(os.getenv("BT_WARMUP", "45"))
CUTOFF = float(os.getenv("CUTOFF_H", "2")) * 3600
MAX_ODDS = int(os.getenv("MAX_ODDS", "4000"))
PACE = float(os.getenv("BT_PACE", "1.2"))
TG = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT = os.getenv("MY_CHAT_ID", "").strip()
OUT_ROOT = os.getenv("BT_OUT", "resultados")

ELO_K = 24
MATCH_LEN = 30 * 60          # un partido se da por terminado 30 min despues de empezar
MIN_PLAYED = 10              # con menos partidos previos el jugador "no se conoce": no hay pick
H2H_DAYS = 90
MIN_ODD, MAX_ODD = 1.25, 4.0
UMBRALES = [0.0, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20]
UMBRAL_DETALLE = 0.05
MIN_N = 30                   # menos picks = no fiable


# ======================================================================= datos
def cache_dir(lid):
    d = os.path.join(OUT_ROOT, slug(lid), "cache")
    os.makedirs(d, exist_ok=True)
    return d


def slug(lid):
    return LIGAS[lid].lower().replace(" ", "_")


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

    def ts(day):
        return datetime.combine(day, datetime.min.time(), TZ).timestamp()
    period_start = ts(end - timedelta(days=DAYS - 1))
    test_start = ts(end - timedelta(days=DAYS // 2 - 1))
    return ms, period_start, test_start, start, end


_odds_used = 0


def fetch_odds(lid, mid):
    """Cuotas de un partido, con cache. None si se acabo el cupo de consultas."""
    global _odds_used
    path = os.path.join(cache_dir(lid), f"odds_{mid}.json")
    if os.path.exists(path):
        with open(path) as f:
            return odds_from(json.load(f))
    if _odds_used >= MAX_ODDS:
        return "cupo"
    data = None
    for intento in range(4):
        try:
            data = odds_raw(mid)
            break
        except Exception as ex:
            print("WARN cuotas", mid, ex)
            time.sleep(8 * (intento + 1))
    _odds_used += 1
    time.sleep(PACE)
    if data is None:
        return None                      # fallo de red: no se guarda, se reintenta otro dia
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
        fa = sum(self.hist[a]) if self.hist[a] else 0.0
        fb = sum(self.hist[b]) if self.hist[b] else 0.0
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
                    n_a=self.n[a], n_b=self.n[b], forma_a=fa, forma_b=fb,
                    hoy_j_a=ja, hoy_g_a=ga, hoy_j_b=jb, hoy_g_b=gb, h2h_a=wa, h2h_b=wb)


def vector(f):
    """Caracteristicas numericas desde el punto de vista del jugador A."""
    pe = min(max(f["p_elo"], 0.01), 0.99)
    h = (f["h2h_a"] - f["h2h_b"]) / (f["h2h_a"] + f["h2h_b"] + 2)     # H2H con pocas muestras pesa poco
    hoy_a = f["hoy_g_a"] - (f["hoy_j_a"] - f["hoy_g_a"])
    hoy_b = f["hoy_g_b"] - (f["hoy_j_b"] - f["hoy_g_b"])
    return [math.log(pe / (1 - pe)),
            f["forma_a"] - f["forma_b"],
            h,
            hoy_a - hoy_b,
            f["hoy_j_a"] - f["hoy_j_b"]]


NOMBRES = ["Elo", "Forma ajustada", "H2H 90 días", "Balance de hoy", "Partidos jugados hoy"]


def construir(ms, period_start):
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
        if m["t"] < period_start:
            continue
        f = est.feats(m)
        f.update(id=m["id"], t=m["t"], a=m["a"], b=m["b"], gana_a=int(m["final"][0] > m["final"][1]),
                 marcador=f"{m['final'][0]}-{m['final'][1]}")
        f["conocidos"] = f["n_a"] >= MIN_PLAYED and f["n_b"] >= MIN_PLAYED
        filas.append(f)
    return filas


# ===================================================== modelo (regresion logistica)
def entrenar(X, y, l2=1.0, iters=3000, lr=0.1):
    k = len(X[0])
    mu = [sum(r[j] for r in X) / len(X) for j in range(k)]
    sd = [max(1e-9, math.sqrt(sum((r[j] - mu[j]) ** 2 for r in X) / len(X))) for j in range(k)]
    Z = [[(r[j] - mu[j]) / sd[j] for j in range(k)] for r in X]
    w = [0.0] * k
    b = 0.0
    n = len(Z)
    for _ in range(iters):
        gw = [0.0] * k
        gb = 0.0
        for z, t in zip(Z, y):
            s = b + sum(wj * zj for wj, zj in zip(w, z))
            p = 1 / (1 + math.exp(-max(-30, min(30, s))))
            e = p - t
            gb += e
            for j in range(k):
                gw[j] += e * z[j]
        b -= lr * gb / n
        w = [w[j] - lr * (gw[j] / n + l2 * w[j] / n) for j in range(k)]
    return dict(w=w, b=b, mu=mu, sd=sd)


def predecir(mod, x):
    s = mod["b"] + sum(w * (v - m) / s_ for w, v, m, s_ in zip(mod["w"], x, mod["mu"], mod["sd"]))
    return 1 / (1 + math.exp(-max(-30, min(30, s))))


def logloss(ps, ys):
    ps = [min(max(p, 1e-6), 1 - 1e-6) for p in ps]
    return -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in zip(ps, ys)) / len(ys)


# ================================================================= apuestas
def elegir(r, umbral, cuota_key):
    """Lado con mas valor si supera el umbral. Devuelve (lado, cuota, prob) o None."""
    best = None
    for lado, p, c in (("a", r["p"], r[f"{cuota_key}_a"]), ("b", 1 - r["p"], r[f"{cuota_key}_b"])):
        if not c or not (MIN_ODD <= c <= MAX_ODD):
            continue
        v = p * c - 1
        if v >= umbral and (best is None or v > best[3]):
            best = (lado, c, p, v)
    return best


def apostar(rows, umbral, cuota_key="fin", filtro=None):
    picks = []
    for r in rows:
        e = elegir(r, umbral, cuota_key)
        if not e:
            continue
        lado, c, p, v = e
        gana = r["gana_a"] if lado == "a" else 1 - r["gana_a"]
        pk = dict(r=r, lado=lado, cuota=c, p=p, valor=v, gana=gana,
                  ben=(c - 1) if gana else -1.0,
                  fav_casa=c < (r[f"{cuota_key}_b"] if lado == "a" else r[f"{cuota_key}_a"]))
        if filtro is None or filtro(pk):
            picks.append(pk)
    return picks


def resumen(picks):
    n = len(picks)
    if not n:
        return "0 picks"
    g = sum(p["gana"] for p in picks)
    ben = sum(p["ben"] for p in picks)
    cm = sum(p["cuota"] for p in picks) / n
    aviso = "" if n >= MIN_N else " · ⚠️ pocos"
    icono = "🟢" if ben > 0 and n >= MIN_N else ("🔴" if ben < 0 else "⚪")
    return (f"{icono} {n} picks · {g}✅ · win {g / n * 100:.0f}% · cuota media {cm:.2f} · "
            f"{ben:+.1f} u · YIELD {ben / n * 100:+.1f}%{aviso}")


# ================================================================== informe
def analizar(lid):
    global _odds_used
    _odds_used = 0
    nombre = LIGAS[lid]
    ms, period_start, test_start, start, end = load(lid)
    print(f"[{nombre}] {len(ms)} partidos terminados del {start} al {end}")
    filas = construir(ms, period_start)
    conocidos = [f for f in filas if f["conocidos"]]
    train = [f for f in conocidos if f["t"] < test_start]
    test = [f for f in conocidos if f["t"] >= test_start]
    L = [f"📊 {nombre.upper()} · backtest bot multiliga",
         f"Periodo {datetime.fromtimestamp(period_start, TZ):%d/%m} – {end:%d/%m} · "
         f"decisión {CUTOFF / 3600:g} h antes del partido",
         f"{len(filas)} partidos · {len(conocidos)} con ambos jugadores conocidos (≥{MIN_PLAYED} previos)",
         f"Ajuste: {len(train)} partidos · Test (sin ver): {len(test)} partidos", ""]
    if len(train) < 200 or len(test) < 100:
        L.append("❌ Muy pocos partidos para sacar conclusiones.")
        return L, []

    mod = entrenar([vector(f) for f in train], [f["gana_a"] for f in train])
    for f in test:
        f["p"] = predecir(mod, vector(f))

    # ---- cuotas (solo partidos del test: lo mas reciente primero)
    cupo = 0
    for f in sorted(test, key=lambda x: -x["t"]):
        o = fetch_odds(lid, f["id"])
        if o == "cupo":
            cupo += 1
            o = None
        if not o:
            f.update(ini_a=None, fin_a=None, ini_b=None, fin_b=None)
            continue
        f["ini_a"], f["fin_a"], f["ini_b"], f["fin_b"] = o
    con = [f for f in test if f.get("fin_a")]
    print(f"[{nombre}] cuotas: {len(con)} con cuota · consultas nuevas {_odds_used} · pendientes por cupo {cupo}")

    # ---- calidad del modelo
    ys = [f["gana_a"] for f in test]
    L.append("🎯 ACIERTO DEL MODELO (partidos de test)")
    acierto_m = sum((f["p"] >= 0.5) == bool(f["gana_a"]) for f in test) / len(test)
    acierto_e = sum((f["p_elo"] >= 0.5) == bool(f["gana_a"]) for f in test) / len(test)
    L.append(f"• Favorito del modelo gana el {acierto_m * 100:.1f}% (solo Elo: {acierto_e * 100:.1f}%)")
    if con:
        mk = []
        for f in con:
            ia, ib = 1 / f["fin_a"], 1 / f["fin_b"]
            mk.append(ia / (ia + ib))
        ysc = [f["gana_a"] for f in con]
        acierto_c = sum((p >= 0.5) == bool(y) for p, y in zip(mk, ysc)) / len(con)
        L.append(f"• Favorito de la casa gana el {acierto_c * 100:.1f}%")
        lm, lc = logloss([f["p"] for f in con], ysc), logloss(mk, ysc)
        L.append(f"• Precisión (log-loss, menos es mejor): modelo {lm:.4f} · casa {lc:.4f}"
                 + ("  ✅ el modelo afina más que la casa" if lm < lc else "  ⚠️ la casa afina más que el modelo"))
    L.append("")
    L.append("Peso de cada factor (+ = ayuda a predecir a favor):")
    for nm, w in sorted(zip(NOMBRES, mod["w"]), key=lambda x: -abs(x[1])):
        L.append(f"   {nm}: {w:+.2f}")
    L.append("")

    if not con:
        L.append("❌ Sin cuotas en el test: no se puede calcular el yield.")
        return L, test
    if cupo:
        L.append(f"⚠️ Faltan cuotas de {cupo} partidos (cupo de consultas). "
                 "Vuelve a lanzarlo: sigue donde lo dejó.")
        L.append("")

    # ---- yield por umbral
    L.append("💶 YIELD SEGÚN EL VALOR MÍNIMO EXIGIDO (cuota final, 1 u por pick)")
    L.append(f"   (valor = prob × cuota − 1 · cuotas {MIN_ODD}–{MAX_ODD})")
    for u in UMBRALES:
        L.append(f"▶ valor ≥{u * 100:.0f}%: {resumen(apostar(con, u))}")
    L.append("")
    L.append("   Con la cuota de APERTURA (referencia, si entraras pronto):")
    for u in (0.05, 0.10):
        L.append(f"▶ valor ≥{u * 100:.0f}%: {resumen(apostar(con, u, 'ini'))}")
    L.append("")
    L.append("   Referencia sin modelo: apostar siempre al favorito de la casa")
    fav = []
    for f in con:
        lado = "a" if f["fin_a"] <= f["fin_b"] else "b"
        c = f["fin_a"] if lado == "a" else f["fin_b"]
        g = f["gana_a"] if lado == "a" else 1 - f["gana_a"]
        fav.append(dict(cuota=c, gana=g, ben=(c - 1) if g else -1.0))
    L.append(f"▶ {resumen(fav)}")
    L.append("")

    # ---- detalle con el umbral de referencia
    u = UMBRAL_DETALLE
    base = apostar(con, u)
    L.append(f"🔍 DETALLE con valor ≥{u * 100:.0f}%")
    tramos = [(1.25, 1.60), (1.60, 2.00), (2.00, 2.60), (2.60, 4.01)]
    for lo, hi in tramos:
        L.append(f"▶ cuota {lo:.2f}–{min(hi, 4):.2f}: {resumen([p for p in base if lo <= p['cuota'] < hi])}")
    L.append(f"▶ pick favorito de la casa: {resumen([p for p in base if p['fav_casa']])}")
    L.append(f"▶ pick NO favorito de la casa: {resumen([p for p in base if not p['fav_casa']])}")
    L.append(f"▶ con H2H a favor (≥2 victorias más): "
             + resumen([p for p in base if (p['r']['h2h_a'] - p['r']['h2h_b']) * (1 if p['lado'] == 'a' else -1) >= 2]))
    L.append(f"▶ el pick ya ha jugado hoy: "
             + resumen([p for p in base if (p['r']['hoy_j_a'] if p['lado'] == 'a' else p['r']['hoy_j_b']) > 0]))
    L.append(f"▶ el pick aún no ha jugado hoy: "
             + resumen([p for p in base if (p['r']['hoy_j_a'] if p['lado'] == 'a' else p['r']['hoy_j_b']) == 0]))
    mitad = test_start + (datetime.now(TZ).timestamp() - test_start) / 2
    y1 = [p for p in base if p["r"]["t"] < mitad]
    y2 = [p for p in base if p["r"]["t"] >= mitad]
    L.append(f"▶ 1.ª mitad del test: {resumen(y1)}")
    L.append(f"▶ 2.ª mitad del test: {resumen(y2)}")
    est = (y1 and y2 and sum(p['ben'] for p in y1) > 0 and sum(p['ben'] for p in y2) > 0)
    L.append("   " + ("estable ✅ gana en las dos mitades" if est else "inestable ⚠️ no gana en las dos mitades"))
    L.append("")

    # ---- partidos al dia
    dias = max(1, (datetime.now(TZ).timestamp() - test_start) / 86400)
    L.append(f"📅 Picks al día con valor ≥{u * 100:.0f}%: ~{len(base) / dias:.1f}")
    return L, test


def guardar_csv(lid, test):
    out = os.path.join(OUT_ROOT, slug(lid))
    os.makedirs(out, exist_ok=True)
    cols = ["fecha", "hora", "a", "b", "marcador", "gana_a", "p_modelo", "p_elo", "elo_a", "elo_b",
            "forma_a", "forma_b", "hoy_j_a", "hoy_g_a", "hoy_j_b", "hoy_g_b", "h2h_a", "h2h_b",
            "ini_a", "fin_a", "ini_b", "fin_b", "pick", "cuota_pick", "valor", "beneficio", "id"]
    path = os.path.join(out, "partidos_test.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for f in sorted(test, key=lambda x: x["t"]):
            dt = datetime.fromtimestamp(f["t"], TZ)
            e = elegir(f, UMBRAL_DETALLE, "fin") if f.get("fin_a") else None
            pick = cuota = valor = ben = ""
            if e:
                lado, cuota, p, v = e
                pick = f["a"] if lado == "a" else f["b"]
                gana = f["gana_a"] if lado == "a" else 1 - f["gana_a"]
                valor = round(v, 3)
                ben = round(cuota - 1, 2) if gana else -1
            w.writerow([dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), f["a"], f["b"], f["marcador"],
                        f["gana_a"], round(f["p"], 3), round(f["p_elo"], 3), f["elo_a"], f["elo_b"],
                        round(f["forma_a"], 2), round(f["forma_b"], 2), f["hoy_j_a"], f["hoy_g_a"],
                        f["hoy_j_b"], f["hoy_g_b"], f["h2h_a"], f["h2h_b"], f.get("ini_a") or "",
                        f.get("fin_a") or "", f.get("ini_b") or "", f.get("fin_b") or "",
                        pick, cuota, valor, ben, f["id"]])
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
        lineas, test = analizar(lid)
        txt = "\n".join(lineas)
        print(txt)
        out = os.path.join(OUT_ROOT, slug(lid))
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "informe.txt"), "w") as fh:
            fh.write(txt + "\n")
        tg_msg(txt)
        if test:
            tg_file(guardar_csv(lid, test))


if __name__ == "__main__":
    main()
