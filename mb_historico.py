"""
mb_historico.py - Simulacion de 12 MESES de la REGLA EN CALIENTE (TT Cup, Elite
Series o Liga Pro Checa, segun LIGA_SIM), con
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
LIGA_ID = os.getenv("LIGA_SIM", "15005").strip() or "15005"   # 15005 TT Cup · 19041 Elite · 19039 Liga Pro
DIAS = MESES_N * 31 + 30                      # 12 meses evaluados + 30 dias de arranque
os.environ.update(LIGA=LIGA_ID, BT_DAYS=str(DIAS), BT_WARMUP="45", ARRANQUE_DIAS="30",
                  VISTO_DIAS="30", BT_PACE="0", BT_OUT="resultados_12m",
                  BT_MAX_MIN=os.getenv("BT_MAX_MIN", "300"))

import mb_bets                                # noqa: E402
import mb_backtest as bt                      # noqa: E402

GAP = float(os.getenv("BETS_GAP", "2.5"))
VUELTA = int(os.getenv("VUELTA", "1") or 1)
UNIDAD = 50
NOMBRE = bt.LIGAS.get(LIGA_ID, LIGA_ID)
OUT = os.path.join("resultados_12m", bt.slug(LIGA_ID))
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


# ---- cuotas: se guarda la evolucion ANTES del partido (hora, local, visitante),
# para poder medir cuando abre el mercado, cuanto se mueve y la cuota de 2 h antes
LISTAS = {}


def odds_raw(mid):
    data = mb_bets.odds_raw(mid)
    lista = (((data or {}).get("results") or {}).get("odds") or {}).get("92_1") or []
    out = []
    for o in lista:
        if o.get("ss"):
            continue                              # en directo: no cuenta
        try:
            h, a, t = float(o["home_od"]), float(o["away_od"]), int(o.get("add_time") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if h > 1 and a > 1:
            out.append((t, h, a))
    out.sort()
    return {"mid": mid, "l": out}


def odds_from(d):
    lista = (d or {}).get("l") or []
    LISTAS[(d or {}).get("mid")] = lista
    if not lista:
        return None
    return lista[0][1], lista[-1][1], lista[0][2], lista[-1][2]


class CuotasDetalle(bt.Cuotas):
    """Como las de mb_backtest, pero en archivos odds2_ (con toda la evolucion)."""

    def get(self, mid):
        path = os.path.join(bt.cache_dir(self.lid), f"odds2_{mid}.json")
        if os.path.exists(path):
            with open(path) as fh:
                return odds_from(json.load(fh))
        if self.cortado or self.nuevas >= bt.MAX_ODDS or time.time() - bt.INICIO > bt.MAX_MIN * 60:
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
        if data is None:
            self.fallos_seguidos += 1
            if self.fallos_seguidos >= 5:
                print("STOP cuotas: 5 fallos seguidos")
                self.cortado = True
            self.cupo += 1
            return None
        self.fallos_seguidos = 0
        with open(path, "w") as fh:
            json.dump(data, fh)
        return odds_from(data)


bt.Cuotas = CuotasDetalle


def guardar_detalle(ev):
    """Un pick de la regla por fila, con todo lo que se sabia antes del partido."""
    import csv
    cols = ["fecha", "hora", "pick", "rival", "gana", "marcador", "prob", "cuota_cierre",
            "cuota_2h", "cuota_apertura", "abre_h_antes", "movimientos", "rival_cuota_cierre",
            "pick_jugados_hoy", "pick_ganados_hoy", "rival_jugados_hoy", "elo_pick", "elo_rival",
            "forma_pick", "forma_rival", "partidos_pick", "partidos_rival", "h2h_pick", "h2h_rival", "id"]
    path = os.path.join(OUT, "picks_detalle.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for f in sorted(ev, key=lambda x: x["t"]):
            s = f["caliente"]
            if not s or f.get("p") is None:
                continue
            o = "b" if s == "a" else "a"
            lista = LISTAS.get(f["id"]) or []
            if not lista:
                continue
            i = 1 if s == "a" else 2
            antes = [x for x in lista if x[0] <= f["t"] - 2 * 3600]
            p = f["p"] if s == "a" else 1 - f["p"]
            dt = datetime.fromtimestamp(f["t"], bt.TZ)
            w.writerow([dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M"), f[s], f[o],
                        f["gana_a"] if s == "a" else 1 - f["gana_a"], f["marcador"], round(p, 4),
                        lista[-1][i], antes[-1][i] if antes else "", lista[0][i],
                        round((f["t"] - lista[0][0]) / 3600, 2), len(lista), lista[-1][3 - i],
                        f[f"hoy_j_{s}"], f[f"hoy_g_{s}"], f[f"hoy_j_{o}"], f[f"elo_{s}"], f[f"elo_{o}"],
                        round(f[f"forma_{s}"], 3), round(f[f"forma_{o}"], 3), f[f"n_{s}"], f[f"n_{o}"],
                        f[f"h2h_{s}"], f[f"h2h_{o}"], f["id"]])
    return path


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

    L = [f"📊 {NOMBRE.upper()} · REGLA EN CALIENTE · SIMULACIÓN 12 MESES",
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
        tg(f"⏳ Empieza la simulación de 12 meses de {NOMBRE} (regla en caliente). "
           f"Tarda varias horas: va despacio para no molestar al bot. Te aviso al terminar.")
    bt.INICIO = time.time()
    lineas, ev, _ = bt.analizar(LIGA_ID)
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
        tg(f"❌ Simulación 12 meses de {NOMBRE}: BetsAPI no ha dado suficientes partidos.\n"
           + "\n".join(lineas[:8]))
        return
    bt.guardar_csv(LIGA_ID, ev)
    guardar_detalle(ev)
    txt = informe(ev)
    with open(os.path.join(OUT, "informe_12m.txt"), "w") as fh:
        fh.write(txt + "\n")
    print(txt)
    tg(txt + "\n\n✅ Datos con el detalle de cuotas guardados: avisa a Claude para buscar el filtro.")


if __name__ == "__main__":
    main()
