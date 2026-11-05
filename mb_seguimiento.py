"""
mb_seguimiento.py - Bot de seguimiento de la REGLA EN CALIENTE en TT Cup.

Cada ejecucion (GitHub la lanza cada 5 minutos):
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
import time
import unicodedata
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
VALOR_SOSPECHA = 0.30
PACE = float(os.getenv("PACE", "1.0"))
TG = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT = os.getenv("MY_CHAT_ID", "").strip()

DIR = "seguimiento"
CACHE = os.path.join(DIR, "cache")
PICKS = os.path.join(DIR, "picks.csv")
ESTADO = os.path.join(DIR, "estado.json")
COLS = ["fecha", "hora", "nivel", "pick", "rival", "prob", "cuota_minima", "cuota_fuerte",
        "cuota_sospecha", "cuota_2h", "valor_2h", "cuota_betcris", "valor_betcris",
        "decision_betcris", "cuota_final", "pick_jugados_hoy", "resultado", "marcador",
        "beneficio", "beneficio_betcris", "aviso", "msg", "id"]

NIVEL_TXT = {"SOSPECHOSO": "⚠️ SOSPECHOSO (valor >30%, desconfiar)",
             "FUERTE": "⭐⭐ FUERTE", "APUESTA": "💰 APUESTA",
             "FLOJO": "❌ NO APOSTAR", "SIN CUOTA": ""}


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
def tg(text, reply_to=None):
    if not (TG and CHAT):
        print(text)
        return None
    params = {"chat_id": CHAT, "text": text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{TG}/sendMessage",
                                    urllib.parse.urlencode(params).encode(), timeout=30) as r:
            return json.loads(r.read().decode()).get("result", {}).get("message_id")
    except Exception as ex:
        print("WARN telegram", ex)
        return None


def tg_updates(offset):
    """Mensajes nuevos que le has escrito al bot."""
    if not (TG and CHAT):
        return []
    url = f"https://api.telegram.org/bot{TG}/getUpdates?timeout=0&offset={offset}"
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
               f"Según la cuota de BetCris:\n"
               f"❌ menos de {c_apuesta:.2f} → no apostar\n"
               f"💰 {c_apuesta:.2f} a {c_fuerte - 0.01:.2f} → APUESTA\n"
               f"⭐⭐ {c_fuerte:.2f} a {c_sospecha:.2f} → FUERTE\n"
               f"⚠️ más de {c_sospecha:.2f} → valor >30%, desconfiar\n"
               f"\n"
               f"Sofascore: {f'{c:.2f} → {NIVEL_TXT[nv]}' if c else 'sin cuota'}\n"
               f"Prob. {p * 100:.0f}% · ya ha jugado {jh} hoy, rival aún no\n"
               f"↩️ Responde con tu cuota de BetCris para apuntarla\n"
               f"🧪 Seguimiento")
        msg = tg(txt)
        picks.append(dict(fecha=dt.strftime("%Y-%m-%d"), hora=dt.strftime("%H:%M"),
                          nivel=nv, pick=pick, rival=rival, prob=round(p, 3),
                          cuota_minima=c_apuesta, cuota_fuerte=c_fuerte,
                          cuota_sospecha=c_sospecha,
                          cuota_2h=c or "", valor_2h=round(valor, 3) if valor is not None else "",
                          pick_jugados_hoy=jh, aviso=now.strftime("%Y-%m-%d %H:%M"),
                          resultado="pendiente", msg=msg or "", id=f["id"]))
        ya.add(f["id"])


def calc_betcris(p):
    """Decision y beneficio con la cuota de BetCris apuntada."""
    cb = num(p.get("cuota_betcris"))
    pr = num(p.get("prob"))
    if not cb or not pr:
        return
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
    if p.get("cuota_betcris"):
        b = num(p.get("beneficio_betcris")) or 0
        t += (f"\nBetCris {float(p['cuota_betcris']):.2f} · {NIVEL_TXT[p['decision_betcris']]}"
              f" · {b:+.2f} u")
    return t


def leer_respuestas(picks, st):
    """Apunta las cuotas de BetCris que le escribes al bot:
       - respondiendo al aviso con la cuota ("1,84"), o
       - escribiendo "apellido cuota" ("precek 1,84")."""
    upd = tg_updates(st.get("offset", 0))
    por_msg = {str(p.get("msg")): p for p in picks if p.get("msg")}
    for u in upd:
        st["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != CHAT:
            continue
        texto = (m.get("text") or "").strip()
        nums = re.findall(r"\d+[.,]\d+|\d+", texto)
        if not nums:
            continue
        cb = float(nums[-1].replace(",", "."))
        if not (1.01 <= cb <= 50):
            tg("❓ No entiendo la cuota. Escríbela así: 1,84", m.get("message_id"))
            continue
        rep = str((m.get("reply_to_message") or {}).get("message_id", ""))
        pk = por_msg.get(rep)
        if not pk:
            palabras = [w for w in limpio(texto).split() if len(w) >= 3]
            cand = [p for p in picks if any(w in limpio(p["pick"]).split() for w in palabras)]
            pend = [p for p in cand if p.get("resultado") == "pendiente"]
            cand = pend or cand
            if not cand:
                tg("❓ No encuentro ese pick. Responde al aviso con la cuota, "
                   "o escribe: apellido cuota (ej. precek 1,84)", m.get("message_id"))
                continue
            pk = max(cand, key=lambda p: (p["fecha"], p["hora"]))
        pk["cuota_betcris"] = cb
        calc_betcris(pk)
        t = (f"📝 Apuntado {pk['pick'].upper()} ({pk['hora']}) · BetCris {cb:.2f}\n"
             f"Valor {float(pk['valor_betcris']) * 100:+.0f}% → {NIVEL_TXT[pk['decision_betcris']]}")
        if pk.get("resultado") in ("ganado", "perdido", "anulado"):
            t += "\n" + texto_resultado(pk)
        tg(t, m.get("message_id"))


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
        else:
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
        if p.get("cuota_betcris"):               # solo te avisa de los que apuntaste
            tg(texto_resultado(p), p.get("msg") or None)


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
    sin = sum(1 for p in del_dia if p["resultado"] != "pendiente" and not p.get("cuota_2h"))
    txt = (f"📋 RESUMEN TT CUP · {dia:%d/%m} · {len(del_dia)} avisos\n" + bloque(del_dia)
           + (f"⏳ Pendientes: {pend}\n" if pend else "")
           + (f"(sin cuota en Sofascore: {sin}, solo cuentan si apuntaste BetCris)\n" if sin else "")
           + f"\n📈 ACUMULADO desde {st.get('inicio', hoy)} · {len(picks)} avisos\n" + bloque(picks)
           + "(1 u por pick)")
    tg(txt)
    st["resumen"] = hoy


def main():
    if not mb_api.KEY:
        raise SystemExit("Falta ZYLA_API_KEY")
    now = datetime.now(TZ)
    st = leer_estado()
    picks = leer_picks()
    for p in picks:                          # picks de versiones anteriores
        if p.get("nivel") == "REGLA" or not p.get("nivel"):
            p["nivel"] = nivel_de(p)
        if not p.get("cuota_sospecha") and num(p.get("prob")):
            p["cuota_minima"] = max(MIN_ODD, round(1 / float(p["prob"]), 2))
            p["cuota_fuerte"] = max(MIN_ODD, round((1 + VALOR_FUERTE) / float(p["prob"]), 2))
            p["cuota_sospecha"] = round((1 + VALOR_SOSPECHA) / float(p["prob"]), 2)
    if not st.get("inicio"):
        st["inicio"] = now.strftime("%Y-%m-%d")
    if not st.get("v2"):
        st["v2"] = True
        tg("🟢 Seguimiento TT Cup actualizado\n"
           "Cada aviso te dice qué hacer según la cuota de BetCris:\n"
           "❌ no apostar · 💰 APUESTA · ⭐⭐ FUERTE · ⚠️ valor >30%, desconfiar\n"
           "↩️ Responde al aviso con tu cuota (ej. 1,84) o escribe: apellido cuota\n"
           "Todo queda apuntado. Resumen cada noche a las 23:45.")
    leer_respuestas(picks, st)
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
