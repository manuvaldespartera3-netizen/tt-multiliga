"""
mb_seguimiento.py - Bot de seguimiento de la REGLA EN CALIENTE en TT Cup.

Cada ejecucion (GitHub la lanza cada 5 minutos):
  1. Lee los partidos de TT Cup (historico en cache + hoy y manana en directo).
  2. Calcula Elo, forma, H2H y lo que lleva hoy cada jugador, con el mismo corte
     de 2 horas que el backtest validado.
  3. Para cada partido que empieza en las proximas ~2 horas, si un jugador ya ha
     jugado hoy (hace mas de 2 h) y el rival todavia no -> PICK.
       ⭐⭐ FUERTE  el modelo ve valor ≥10% con la cuota de Sofascore
       ⭐  REGLA   el resto de picks de la regla
     Avisa por Telegram con la cuota minima y lo apunta en seguimiento/picks.csv.
  4. Cuando el partido termina, apunta el resultado y el beneficio.
  5. Sobre las 23:45 manda el resumen del dia y el acumulado.

Variables: ZYLA_API_KEY, TELEGRAM_TOKEN, MY_CHAT_ID
           TRAIN_DAYS (120) · WARMUP_DAYS (45) · AVISO_MIN (125: avisa desde
           2 h 05 min antes) · TOURNAMENT (15005 = TT Cup)
"""
import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import mb_api
from mb_api import TZ, parse, day_events, odds_raw, odds_from
from mb_backtest import (Estado, vector, entrenar, predecir, MATCH_LEN, CUTOFF,
                         MIN_PLAYED, MIN_ODD, MAX_ODD)

LID = os.getenv("TOURNAMENT", "15005")
TRAIN_DAYS = int(os.getenv("TRAIN_DAYS", "120"))
WARMUP_DAYS = int(os.getenv("WARMUP_DAYS", "45"))
AVISO = int(os.getenv("AVISO_MIN", "125")) * 60
VALOR_FUERTE = 0.10
PACE = float(os.getenv("PACE", "1.0"))
TG = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT = os.getenv("MY_CHAT_ID", "").strip()

DIR = "seguimiento"
CACHE = os.path.join(DIR, "cache")
PICKS = os.path.join(DIR, "picks.csv")
ESTADO = os.path.join(DIR, "estado.json")
COLS = ["fecha", "hora", "nivel", "pick", "rival", "prob", "cuota_minima", "cuota_2h",
        "valor_2h", "cuota_final", "pick_jugados_hoy", "resultado", "marcador",
        "beneficio", "aviso", "id"]


# ===================================================================== datos
def fetch_day(d, now):
    """Partidos de un dia. Solo se guarda en cache cuando el dia ya ha acabado
    hace mas de 3 h (para no guardar un dia a medias)."""
    path = os.path.join(CACHE, f"{d}.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    for intento in range(4):
        try:
            evs = day_events(LID, d)
            break
        except Exception as ex:
            print("WARN dia", d, ex)
            time.sleep(5 * (intento + 1))
    else:
        return []
    fin_dia = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=TZ) + timedelta(days=1, hours=3)
    if now >= fin_dia:
        os.makedirs(CACHE, exist_ok=True)
        with open(path, "w") as f:
            json.dump(evs, f)
        time.sleep(PACE)
    return evs


def cargar(now):
    hoy = now.date()
    evs = {}
    d = hoy - timedelta(days=TRAIN_DAYS + WARMUP_DAYS)
    while d <= hoy + timedelta(days=1):
        for e in fetch_day(d.strftime("%Y-%m-%d"), now):
            evs[str(e.get("id"))] = e
        d += timedelta(days=1)
    return evs


def cuotas(mid):
    try:
        return odds_from(odds_raw(mid))
    except Exception as ex:
        print("WARN cuotas", mid, ex)
        return None


# ============================================================ caracteristicas
def calcular(evs, now):
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
            if f["caliente"]:
                cands.append(f)
    return train, cands


# ================================================================== telegram
def tg(text):
    if not (TG and CHAT):
        print(text)
        return None
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{TG}/sendMessage",
                                    data, timeout=30) as r:
            return json.loads(r.read().decode()).get("result", {}).get("message_id")
    except Exception as ex:
        print("WARN telegram", ex)
        return None


# =================================================================== estado
def leer_picks():
    if not os.path.exists(PICKS):
        return []
    with open(PICKS, newline="") as f:
        return list(csv.DictReader(f))


def guardar_picks(rows):
    os.makedirs(DIR, exist_ok=True)
    with open(PICKS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLS})


def leer_estado():
    if os.path.exists(ESTADO):
        with open(ESTADO) as f:
            return json.load(f)
    return {}


def guardar_estado(st):
    os.makedirs(DIR, exist_ok=True)
    with open(ESTADO, "w") as f:
        json.dump(st, f, indent=1)


def apellido(nombre):
    return nombre.upper()


# ==================================================================== pasos
def nuevos_picks(cands, mod, picks, st, now):
    descartados = st.setdefault("descartados", [])
    ya = {p["id"] for p in picks} | set(descartados)
    for f in sorted(cands, key=lambda x: x["t"]):
        if f["id"] in ya:
            continue
        lado = f["caliente"]
        p = float(predecir(mod, [vector(f)])[0])
        if lado == "b":
            p = 1 - p
        o = cuotas(f["id"])
        time.sleep(PACE)
        c = None
        if o:
            c = o[1] if lado == "a" else o[3]
        if c and not (MIN_ODD <= c <= MAX_ODD):
            descartados.append(f["id"])    # fuera del rango validado: no hay pick
            del descartados[:-500]
            continue
        valor = (p * c - 1) if c else None
        fuerte = valor is not None and valor >= VALOR_FUERTE
        minima = max(MIN_ODD, round((1 + VALOR_FUERTE) / p, 2)) if fuerte else MIN_ODD
        pick, rival = (f["a"], f["b"]) if lado == "a" else (f["b"], f["a"])
        jh = f["hoy_j_a"] if lado == "a" else f["hoy_j_b"]
        dt = datetime.fromtimestamp(f["t"], TZ)
        nivel = "⭐⭐" if fuerte else "⭐"
        txt = (f"🔥 PICK TT CUP · {dt:%H:%M}\n"
               f"✅ {apellido(pick)}\n"
               f"vs {rival}\n"
               f"{nivel} {'FUERTE (regla + modelo)' if fuerte else 'REGLA'}\n"
               f"Cuota mínima: {minima:.2f}\n"
               f"Sofascore ahora: {f'{c:.2f}' if c else 'sin cuota'}"
               + (f" · valor {valor * 100:+.0f}%" if valor is not None else "") + "\n"
               f"Ya ha jugado {jh} partido{'s' if jh != 1 else ''} hoy · rival aún no\n"
               f"🧪 Seguimiento")
        tg(txt)
        picks.append(dict(fecha=dt.strftime("%Y-%m-%d"), hora=dt.strftime("%H:%M"),
                          nivel="FUERTE" if fuerte else "REGLA", pick=pick, rival=rival,
                          prob=round(p, 3), cuota_minima=minima,
                          cuota_2h=c or "", valor_2h=round(valor, 3) if valor is not None else "",
                          pick_jugados_hoy=jh, aviso=now.strftime("%Y-%m-%d %H:%M"),
                          resultado="pendiente", id=f["id"]))
        ya.add(f["id"])


def resolver(picks, evs):
    for p in picks:
        if p.get("resultado") != "pendiente":
            continue
        e = evs.get(p["id"])
        if not e:
            continue
        st = (e.get("status") or {}).get("type", "")
        if st in ("notstarted", "inprogress"):
            continue
        m = parse(e)
        if not m or not m["final"]:
            p["resultado"], p["beneficio"] = "anulado", 0
            continue
        gana_local = m["final"][0] > m["final"][1]
        pick_local = (e.get("homeTeam") or {}).get("name") == p["pick"]
        gana = gana_local == pick_local
        p["marcador"] = f"{m['final'][0]}-{m['final'][1]}"
        o = cuotas(p["id"])
        time.sleep(PACE)
        if o:
            p["cuota_final"] = o[1] if pick_local else o[3]
        c = p.get("cuota_2h") or p.get("cuota_final")
        p["resultado"] = "ganado" if gana else "perdido"
        p["beneficio"] = round(float(c) - 1, 2) if (gana and c) else (-1 if c else "")


def resumen(rows):
    hechos = [r for r in rows if r.get("resultado") in ("ganado", "perdido")
              and r.get("beneficio") not in ("", None)]
    if not hechos:
        return "0 picks"
    n = len(hechos)
    g = sum(1 for r in hechos if r["resultado"] == "ganado")
    ben = sum(float(r["beneficio"]) for r in hechos)
    return f"{n} picks · {g}✅ {n - g}❌ · {ben:+.2f} u · yield {ben / n * 100:+.1f}%"


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
    del_dia = [p for p in picks if p["fecha"] == hoy]
    pend = sum(1 for p in del_dia if p["resultado"] == "pendiente")
    txt = (f"📋 RESUMEN TT CUP · {dia:%d/%m}\n"
           f"⭐⭐ Fuertes: {resumen([p for p in del_dia if p['nivel'] == 'FUERTE'])}\n"
           f"⭐ Todos (regla): {resumen(del_dia)}\n"
           + (f"⏳ Pendientes: {pend}\n" if pend else "")
           + f"\n📈 ACUMULADO desde {st.get('inicio', hoy)}\n"
           f"⭐⭐ Fuertes: {resumen([p for p in picks if p['nivel'] == 'FUERTE'])}\n"
           f"⭐ Todos (regla): {resumen(picks)}\n"
           f"(1 u por pick, cuota de Sofascore 2 h antes)")
    tg(txt)
    st["resumen"] = hoy


def main():
    if not mb_api.KEY:
        raise SystemExit("Falta ZYLA_API_KEY")
    now = datetime.now(TZ)
    st = leer_estado()
    picks = leer_picks()
    if not st.get("inicio"):
        st["inicio"] = now.strftime("%Y-%m-%d")
        tg("🟢 Seguimiento TT Cup activo\n"
           "Regla en caliente: el pick ya ha jugado hoy y el rival aún no.\n"
           "⭐⭐ FUERTE = regla + modelo con valor ≥10%\n"
           "⭐ REGLA = el resto\n"
           "Aviso ~2 h antes con la cuota mínima. Resumen cada noche a las 23:45.")
    evs = cargar(now)
    train, cands = calcular(evs, now)
    print(f"{len(evs)} partidos · {len(train)} para entrenar · {len(cands)} candidatos")
    if len(train) >= 500 and cands:
        mod = entrenar([vector(f) for f in train], [f["gana_a"] for f in train])
        nuevos_picks(cands, mod, picks, st, now)
    resolver(picks, evs)
    resumen_dia(picks, st, now)
    guardar_picks(picks)
    guardar_estado(st)


if __name__ == "__main__":
    main()
