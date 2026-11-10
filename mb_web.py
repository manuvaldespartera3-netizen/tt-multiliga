"""
mb_web.py - Descarga unas cuantas paginas de la web oficial de Liga Pro
(tt.league-pro.com) para ver como estan hechas y poder preparar el lector del
RATING OFICIAL. No apuesta ni toca nada: solo guarda el HTML en web/.

Variables: WEB_BASE (por defecto https://tt.league-pro.com), WEB_MAX (25)
"""
import os
import re
import time
import urllib.parse
import urllib.request

BASE = os.getenv("WEB_BASE", "https://tt.league-pro.com").rstrip("/")
MAX = int(os.getenv("WEB_MAX", "25"))
OUT = "web"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Accept-Language": "es,en;q=0.8"}
CLAVES = ("rating", "reyting", "rank", "player", "igrok", "players", "tournament", "turnir",
          "match", "game", "stat", "h2h", "elo", "liga", "league")


def bajar(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace"), r.geturl()


def nombre(url):
    p = urllib.parse.urlparse(url)
    s = (p.path.strip("/") or "inicio") + ("_" + p.query if p.query else "")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:120] + ".html"


def main():
    os.makedirs(OUT, exist_ok=True)
    vistos, cola, indice = set(), [BASE + "/"], []
    while cola and len(vistos) < MAX:
        url = cola.pop(0)
        if url in vistos:
            continue
        vistos.add(url)
        try:
            html, final = bajar(url)
        except Exception as ex:
            indice.append(f"ERROR {url} {ex}")
            continue
        f = nombre(final)
        with open(os.path.join(OUT, f), "w") as fh:
            fh.write(html)
        indice.append(f"{f}\t{final}\t{len(html)} bytes")
        links = re.findall(r'href=["\']([^"\'#]+)', html)
        for h in links:
            u = urllib.parse.urljoin(final, h)
            if urllib.parse.urlparse(u).netloc != urllib.parse.urlparse(BASE).netloc:
                continue
            if any(k in u.lower() for k in CLAVES) and u not in vistos and u not in cola:
                cola.append(u)
        time.sleep(1.5)
    with open(os.path.join(OUT, "indice.txt"), "w") as fh:
        fh.write("\n".join(indice) + "\n")
    print("\n".join(indice))


if __name__ == "__main__":
    main()
