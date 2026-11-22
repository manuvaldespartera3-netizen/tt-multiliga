"""
mb_historico.py - Simulacion de 12 MESES de la REGLA EN CALIENTE de TT Cup, con
datos y cuotas de BetsAPI, exactamente como trabaja ahora el bot:
  - el pick ya ha jugado hoy (hace mas de 2 h) y el rival todavia no
  - el modelo calcula la probabilidad con lo que se sabia 2 h antes
  - 🟢 se apuesta si la cuota llega al minimo (prob x cuota >= 1)
  - 🔴 no se apuesta si no llega · sin cuota = no cuenta
El modelo solo aprende del pasado (se re-entrena cada 15 dias), como en real.

No toca nada del bot ni de los resultados anteriores: todo va a resultados_12m/.
Si no le da tiempo a bajar todas las cuotas, se relanza solo y sigue donde lo dejo.

Variables: BETSAPI_TOKEN, TELEGRAM_TOKEN, MY_CHAT_ID, MESES (12), VUELTA (1),
           BETS_GAP (2.5 s entre consultas: deja sitio de sobra al bot en vivo)
"""
import json
import os
import time
from collections import defaultdict
from datetime import datetime

MESES_N = int(os.getenv("MESES", "12"))
DIAS = MESES_N * 31 + 30                      # 12 meses evaluados + 30 dias de arranque
os.environ.update(LIGA="15005", BT_DAYS=str(DIAS), BT_WARMUP="45", ARRANQUE_DIAS="30",
                  VISTO_DIAS="30", BT_PACE="0", BT_OUT="resultados_12m",
                  BT_MAX_MIN=os.getenv("BT_MAX_MIN", "300"))

import mb_bets                                # noqa: E402
import mb_backtest as bt                      # noqa: E402

GAP = float(os.getenv("BETS_GAP", "2.5"))
VUELTA = int(os.getenv("VUELTA", "1") or 1)
UNIDAD = 50
OUT = os.path.join("resultados_12m", "tt_cup")
MES_TXT = {"01": "Ene", "02": "Feb", "03": "Mar", "04": "Abr", "05": "May", "06": "Jun",
           "07": "Jul", "08": "Ago", "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dic"}

# ---- BetsAPI con freno: como mucho 1 consulta cada GAP segundos
_api = mb_bets.api
_ult = [0.0]


def api_lenta(path, **params):
    espera = GAP - (time.time() - _ult[0])
    if espera > 0:
        time.sleep(espera)
    try:
        return _api(path, **params)
    finally:
        _ult[0] = time.time()


mb_bets.api = api_lenta


# ---- cuotas: se guarda solo lo necesario (apertura y cierre), no la respuesta entera
def odds_raw(mid):
    return {"t": mb_bets.odds_from(mb_bets.odds_raw(mid))}


def odds_from(d):
    t = (d or {}).get("t")
    return tuple(t) if t else None


bt.day_events = mb_bets.day_events
bt.odds_raw = odds_raw
bt.odds_from = odds_from


def tg(texto):
    bt.TG = os.getenv("TELEGRAM_TOKEN", "").strip()
    bt.CHAT = os.getenv("MY_CHAT_ID", "").strip()
    bt.tg_msg(texto)


def informe(ev):
    """Resumen claro por meses: 🟢 apostar / 🔴 no apostar, yield, unidades y euros."""
    regla = [f for f in ev if f["caliente"]]
    picks = bt.picks_regla(ev)                      # con cuota 1,25-4,00
    verdes = [p for p in picks if p["valor"] is not None and p["valor"] >= 0]
    rojos = [p for p in picks if p["valor"] is not None and p["valor"] < 0]
    mes_regla = defaultdict(int)
    for f in regla:
        mes_regla[f["fecha"][:7]] += 1
    mv, mr = defaultdict(list), defaultdict(list)
    for p in verdes:
        mv[p["mes"]].append(p)
    for p in rojos:
        mr[p["mes"]].append(p)

    def st(lst):
        n = len(lst)
        if not n:
            return 0, 0, 0.0, 0.0
        g = sum(p["gana"] for p in lst)
        b = sum(p["ben"] for p in lst)
        return n, g, b, b / n

    L = ["📊 TT CUP · REGLA EN CALIENTE · SIMULACIÓN 12 MESES",
         "Cuota del bot (BetsAPI) · 🟢 = la cuota llega al mínimo · 1 u = 50 €", ""]
    L.append("Mes      🟢 apuestas  acierto   yield     unidades   euros   | 🔴 yield")
    neg = 0
    for m in sorted(set(mv) | set(mr)):
        n, g, b, y = st(mv[m])
        n2, _, _, y2 = st(mr[m])
        if n and b < 0:
            neg += 1
        L.append(f"{MES_TXT[m[5:]]} {m[2:4]}   {n:4}       {100 * g / max(1, n):3.0f}%   "
                 f"{100 * y:+6.1f}%   {b:+7.1f} u  {b * UNIDAD:+7.0f} € | {100 * y2:+.0f}% ({n2})")
    n, g, b, y = st(verdes)
    n2, g2, b2, y2 = st(rojos)
    L.append("")
    L.append(f"TOTAL 🟢: {n} apuestas · acierto {100 * g / max(1, n):.0f}% · yield {100 * y:+.1f}% · "
             f"{b:+.1f} u · {b * UNIDAD:+.0f} €")
    L.append(f"TOTAL 🔴 (no se apuestan): {n2} · yield {100 * y2:+.1f}%")
    L.append(f"Meses 🟢 en negativo: {neg} de {len(mv)}")
    # caidas
    eq = pico = dd = 0.0
    racha = peor_racha = 0
    dia = defaultdict(float)
    for p in sorted(verdes, key=lambda x: x["f"]["t"]):
        eq += p["ben"]
        pico = max(pico, eq)
        dd = max(dd, pico - eq)
        racha = racha + 1 if p["ben"] < 0 else 0
        peor_racha = max(peor_racha, racha)
        dia[p["f"]["fecha"]] += p["ben"]
    if dia:
        peor = min(dia, key=dia.get)
        L.append(f"Peor caída: {-dd:.1f} u ({-dd * UNIDAD:.0f} €) · peor día {dia[peor]:+.1f} u ({peor}) · "
                 f"peor racha: {peor_racha} perdidas seguidas")
    sin = sum(mes_regla.values()) - len(picks)
    L.append("")
    L.append(f"Picks de la regla: {sum(mes_regla.values())} · con cuota válida: {len(picks)} · "
             f"sin cuota o fuera de 1,25-4,00: {sin} (no cuentan)")
    return "\n".join(L)


def main():
    if not mb_bets.TOKEN:
        raise SystemExit("Falta BETSAPI_TOKEN")
    os.makedirs(OUT, exist_ok=True)
    if VUELTA == 1:
        tg(f"⏳ Empieza la simulación de 12 meses de TT Cup (regla en caliente). "
           f"Tarda varias horas: va despacio para no molestar al bot. Te aviso al terminar.")
    bt.INICIO = time.time()
    lineas, ev, _ = bt.analizar("15005")
    with open(os.path.join(OUT, "detalle.txt"), "w") as fh:
        fh.write("\n".join(lineas) + "\n")
    if bt.INCOMPLETO:
        with open(os.path.join(OUT, "INCOMPLETO"), "w") as fh:
            fh.write(str(VUELTA))
        print(f"Vuelta {VUELTA}: faltan cuotas, se relanza")
        return
    if os.path.exists(os.path.join(OUT, "INCOMPLETO")):
        os.remove(os.path.join(OUT, "INCOMPLETO"))
    if not ev:
        tg("❌ Simulación 12 meses: BetsAPI no ha dado suficientes partidos.\n" + "\n".join(lineas[:8]))
        return
    bt.guardar_csv("15005", ev)
    txt = informe(ev)
    with open(os.path.join(OUT, "informe_12m.txt"), "w") as fh:
        fh.write(txt + "\n")
    print(txt)
    tg(txt)


if __name__ == "__main__":
    main()
