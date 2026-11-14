"""
mb_comparar.py - Comprueba que el Seguimiento da LOS MISMOS PICKS con BetsAPI que
con Zyla, en dias en que Zyla funcionaba bien (su historico en cache).

Para TT Cup y cada uno de los ultimos DIAS dias completos que hay en la cache de
Zyla:
  1. Partidos de cada fuente (Zyla de la cache, BetsAPI descargado).
  2. Regla en caliente con el MISMO calculo que el bot (Elo, forma, lo que lleva
     hoy cada jugador, corte de 2 h), con HIST_DIAS dias de historial en las dos.
  3. Compara: picks en las dos, solo en una, ganador y probabilidad del Elo.
Resultado en resultados/comparar_fuentes.txt y por Telegram.

Variables: BETSAPI_TOKEN, TELEGRAM_TOKEN, MY_CHAT_ID, DIAS (7), HIST_DIAS (35)
"""
import json
import os
import unicodedata
from datetime import datetime, timedelta

import mb_bets
from mb_api import TZ, parse
from mb_backtest import construir, tg_msg

DIAS = int(os.getenv("DIAS", "7"))
HIST_DIAS = int(os.getenv("HIST_DIAS", "35"))
ZYLA_CACHE = "seguimiento/cache"
OUT = "resultados/comparar_fuentes.txt"


def apellido(n):
    s = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode().lower()
    w = "".join(c if c.isalpha() else " " for c in s).split()
    if len(w) >= 2 and len(w[-1]) == 1:          # 'Kowal R.' / 'Velky Junior D.'
        return w[0]
    return w[-1] if len(w) >= 2 else (w[0] if w else "")   # 'Jakub Lysek'


def filas(fuente, dias_hist, dias_test):
    """Picks de la regla en caliente de esos dias, calculados como el bot."""
    evs = {}
    for d in dias_hist:
        if fuente == "zyla":
            path = os.path.join(ZYLA_CACHE, f"{d}.json")
            if not os.path.exists(path):
                continue
            with open(path) as f:
                lista = json.load(f)
        else:
            lista = mb_bets.day_events("15005", d)
        for e in lista:
            evs[str(e.get("id"))] = e
    ms = sorted([m for m in (parse(e) for e in evs.values()) if m and m["final"]],
                key=lambda x: (x["t"], x["id"]))
    inicio = datetime.strptime(dias_test[0], "%Y-%m-%d").replace(tzinfo=TZ).timestamp()
    out = []
    for f in construir(ms, inicio):
        if not f["caliente"]:
            continue
        lado = f["caliente"]
        pick, rival = (f["a"], f["b"]) if lado == "a" else (f["b"], f["a"])
        p = f["p_elo"] if lado == "a" else 1 - f["p_elo"]
        gana = f["gana_a"] if lado == "a" else 1 - f["gana_a"]
        out.append(dict(t=f["t"], dia=f["fecha"], pick=pick, rival=rival, p=p, gana=gana))
    return out, len(ms)


def main():
    L = ["🔬 COMPARACIÓN ZYLA vs BETSAPI · Seguimiento TT Cup (regla en caliente)", ""]
    if not mb_bets.TOKEN:
        L.append("❌ Falta BETSAPI_TOKEN")
        return fin(L)
    if not os.path.isdir(ZYLA_CACHE):
        L.append("❌ No encuentro la cache de Zyla (seguimiento/cache)")
        return fin(L)
    dias_z = sorted(f[:-5] for f in os.listdir(ZYLA_CACHE) if f.endswith(".json"))
    test = dias_z[-DIAS:]
    if not test:
        L.append("❌ La cache de Zyla está vacía")
        return fin(L)
    fin_t = datetime.strptime(test[-1], "%Y-%m-%d")
    hist = [(fin_t - timedelta(days=k)).strftime("%Y-%m-%d")
            for k in range(HIST_DIAS + DIAS - 1, -1, -1)]
    z, nz = filas("zyla", hist, test)
    b, nb = filas("betsapi", hist, test)
    L.append(f"Días comparados: {test[0]} a {test[-1]} · historial {HIST_DIAS} días antes en las dos fuentes")
    L.append(f"Partidos con resultado: Zyla {nz} · BetsAPI {nb} (consultas a BetsAPI: {mb_bets.LLAMADAS[0]})")
    L.append("")
    libres = list(b)
    pares, solo_z = [], []
    for x in z:
        k = {apellido(x["pick"]), apellido(x["rival"])}
        y = next((y for y in libres if abs(y["t"] - x["t"]) <= 1200
                  and {apellido(y["pick"]), apellido(y["rival"])} == k), None)
        if y:
            libres.remove(y)
            pares.append((x, y))
        else:
            solo_z.append(x)
    mismo_pick = sum(1 for x, y in pares if apellido(x["pick"]) == apellido(y["pick"]))
    mismo_gana = sum(1 for x, y in pares if x["gana"] == y["gana"])
    difp = [abs(x["p"] - y["p"]) for x, y in pares]
    L.append(f"PICKS de la regla: Zyla {len(z)} · BetsAPI {len(b)} · en las dos {len(pares)} · "
             f"solo Zyla {len(solo_z)} · solo BetsAPI {len(libres)}")
    if pares:
        L.append(f"  En los que coinciden: mismo jugador elegido {mismo_pick}/{len(pares)} · "
                 f"mismo resultado {mismo_gana}/{len(pares)} · diferencia media de prob. Elo "
                 f"{100 * sum(difp) / len(difp):.1f} puntos")
    L.append("")
    L.append("Por día (Zyla / BetsAPI / en las dos):")
    for d in test:
        L.append(f"  {d}: {sum(1 for x in z if x['dia'] == d)} / {sum(1 for x in b if x['dia'] == d)} / "
                 f"{sum(1 for x, _ in pares if x['dia'] == d)}")
    L.append("")
    for nom, lista in (("solo en Zyla", solo_z), ("solo en BetsAPI", libres)):
        if lista:
            L.append(f"Ejemplos {nom}:")
            for x in lista[:10]:
                L.append(f"  {datetime.fromtimestamp(x['t'], TZ):%d/%m %H:%M} {x['pick']} vs {x['rival']}")
    acierto = lambda r: f"{sum(x['gana'] for x in r)}/{len(r)} ({100 * sum(x['gana'] for x in r) / len(r):.0f}%)" if r else "—"
    L.append("")
    L.append(f"Acierto de la regla esos días: Zyla {acierto(z)} · BetsAPI {acierto(b)}")
    ok = pares and len(pares) >= 0.9 * max(len(z), 1) and mismo_pick == len(pares) and \
        mismo_gana >= 0.98 * len(pares)
    L.append("")
    L.append("VEREDICTO: " + ("✅ BetsAPI da los mismos picks que Zyla. Se puede cambiar."
                              if ok else "⚠️ Hay diferencias: pásaselo a Claude antes de seguir."))
    fin(L)


def fin(L):
    txt = "\n".join(L)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)
    tg_msg(txt[:3800])


if __name__ == "__main__":
    main()
