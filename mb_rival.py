"""
mb_rival.py - Añade a la simulacion de 12 meses (picks_detalle.csv) si el RIVAL
(o el pick) juega otro partido entre el momento del aviso (2 h antes) y el partido.
Ejemplo: aviso a las 11:50 para un partido a las 13:55; el rival no ha jugado
todavia, pero juega a las 12:55 -> cuando empieza nuestro partido ya ha jugado 1.

Solo lee lo ya descargado (cache de la simulacion): no hace ninguna consulta a
BetsAPI, asi que no molesta al bot en vivo ni a otra simulacion en marcha.
Variable: LIGA_SIM (15005 TT Cup · 19041 Elite · 19039 Liga Pro)
"""
import csv
import glob
import json
import os
from collections import defaultdict

from mb_api import parse

SLUG = {"15005": "tt_cup", "19041": "tt_elite_series", "19039": "czech_liga_pro"}
LIGA = os.getenv("LIGA_SIM", "15005").strip() or "15005"
DIR = os.path.join("resultados_12m", SLUG[LIGA])
VENTANA = 2 * 3600 + 5 * 60          # el bot avisa 2 h 05 min antes como mucho


def main():
    ms = {}
    for path in glob.glob(os.path.join(DIR, "cache", "20*.json")):
        try:
            with open(path) as fh:
                evs = json.load(fh)
        except (OSError, ValueError):
            continue
        for e in evs or []:
            m = parse(e)
            if m and m["final"]:
                ms[m["id"]] = m
    por_jug = defaultdict(list)
    for m in ms.values():
        por_jug[m["a"]].append((m["t"], m["id"]))
        por_jug[m["b"]].append((m["t"], m["id"]))
    print(f"{len(ms)} partidos en la cache")
    entrada = os.path.join(DIR, "picks_detalle.csv")
    salida = os.path.join(DIR, "picks_detalle_rival.csv")
    n = sin = 0
    with open(entrada, newline="") as fi, open(salida, "w", newline="") as fo:
        r = csv.DictReader(fi)
        w = csv.DictWriter(fo, fieldnames=r.fieldnames + ["rival_antes", "pick_antes"])
        w.writeheader()
        for row in r:
            m = ms.get(row["id"])
            if not m:
                sin += 1
                row["rival_antes"] = row["pick_antes"] = ""
            else:
                t = m["t"]

                def antes(nombre):
                    return sum(1 for (tt, i) in por_jug.get(nombre, [])
                               if i != m["id"] and t - VENTANA <= tt < t)
                row["rival_antes"] = antes(row["rival"])
                row["pick_antes"] = antes(row["pick"])
            n += 1
            w.writerow(row)
    print(f"{n} picks · sin partido en la cache: {sin}")


if __name__ == "__main__":
    main()
