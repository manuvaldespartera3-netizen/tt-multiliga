"""
tt_alerts.py - Avisador de grupos (TT Cup y Liga Pro) -> Telegram (GitHub Actions).
La liga se elige con variables de entorno (TT_TOURNAMENT_ID, TT_TIEBREAK, LIGA_NOMBRE...).

Cada ejecucion consulta la API varias veces (cada POLL_SECONDS) durante
RUN_SECONDS y termina. GitHub la relanza cada 5 minutos. Lo ya enviado se
guarda en state.json para no repetir avisos.

Secretos / variables:
  ZYLA_API_KEY    clave de Zyla Labs
  TELEGRAM_TOKEN  token del bot nuevo (BotFather)
  MY_CHAT_ID      tu id de Telegram
  TEST_MODE       1 = modo prueba (resumenes cada 15 min). 0 = solo picks
"""
import csv
import json
import os
from datetime import datetime
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

import tt_betsapi
import tt_feed
from tt_feed import fetch, build_groups, h2h
from tt_engine import Group, Match
from tt_tipo_b import eval_b

TG = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT = os.getenv("MY_CHAT_ID", "").strip()
# --- ajustes por liga (por defecto, TT Cup tal cual) ---
LIGA = os.getenv("LIGA_NOMBRE", "TT Cup")
LIGA_TAG = os.getenv("LIGA_TAG", "").strip()           # prefijo en cada mensaje (si se comparte chat)
LEER_MENSAJES = os.getenv("LEER_MENSAJES", "1") == "1"  # leer cuotas, /pick, /ver...
ESTRELLAS = os.getenv("ESTRELLAS", "1") == "1"          # categoria ⭐ (TT Cup si, Liga Pro no)
FILTRO_NOCHE = os.getenv("FILTRO_NOCHE", "1") == "1"    # de noche solo ⭐ (TT Cup si, Liga Pro no)
MIN_PARTIDOS = int(os.getenv("MIN_PARTIDOS", "30"))     # por debajo, algo falla en los datos
POLL = int(os.getenv("POLL_SECONDS", "60"))     # ritmo normal
POLL_FAST = int(os.getenv("POLL_FAST", "15"))    # ritmo rapido cuando un pick esta a punto
RUN = int(os.getenv("RUN_SECONDS", "320"))      # algo mas de 5 min: la siguiente ejecucion
                                                # entra justo al acabar (sin huecos)
HOT = {"on": False, "ids": set()}   # partidos decisivos en juego
FRESH = {}      # datos recientes de esos partidos (leidos uno a uno, mas rapidos)
POLL_EVENT = int(os.getenv("POLL_EVENT", "8"))   # segundos entre lecturas del partido decisivo
# Al final de un set importante del partido decisivo se mira mas a menudo,
# siempre que este bot no pase de BETSAPI_MAX_HORA consultas en la ultima hora.
POLL_EVENT_RAPIDO = int(os.getenv("POLL_EVENT_RAPIDO", "3"))
BETSAPI_MAX_HORA = int(os.getenv("BETSAPI_MAX_HORA", "1100"))
HOT_SETS = {}   # ultimo marcador en sets visto de cada partido decisivo
FIN_VISTO = {}  # lecturas seguidas en que el partido decisivo sale terminado
FIN_T = {}      # cuando se vio terminado por primera vez (para el registro)
LAST = {"events": None}   # ultima lista completa de partidos (para el aviso rapido)
TEST = os.getenv("TEST_MODE", "1") == "1"
REPORT_EVERY = 15 * 60
STATE_FILE = os.getenv("STATE_FILE", "state.json")
VERSION = "17"   # al cambiarlo se reenvia el mensaje de arranque
PICKS_FILE = os.getenv("PICKS_FILE", "picks.csv")
SUMMARY_HOUR = os.getenv("SUMMARY_AT", "23:45")   # hora del resumen diario
FIELDS = ["fecha", "hora", "tipo", "pick", "rival", "h2h_pick", "h2h_rival",
          "aviso_puntos", "match_id", "resultado", "marcador", "momento",
          "calidad", "enviado", "cuota", "apuesta", "msg_id",
          "cuota_aviso", "cuota_cierre"]          # cuotas de Sofascore (automaticas)
URL_ODDS = os.getenv(
    "ZYLA_URL_ODDS",
    "https://zylalabs.com/api/12787/sofascore+-+live+api/25097/get+match+odds")
LIVE_PTS_MAX = int(os.getenv("LIVE_PTS_MAX", "8"))   # puntos jugados en el 1.er set

state = {"sent": [], "last_report": 0, "last_error": "", "started": False, "urls": {},
         "last_summary": ""}
picks = []   # historial (se guarda en picks.csv dentro del repositorio)


def load_state():
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    except Exception:
        pass
    for k, v in (state.get("urls") or {}).items():
        if v and not tt_feed.URLS.get(k):
            tt_feed.URLS[k] = v
    tt_betsapi.cargar_nombres(state.get("nombres_bets"))   # nombres fijos de jugadores


def load_picks():
    picks.clear()
    try:
        with open(PICKS_FILE, newline="", encoding="utf-8") as f:
            picks.extend(csv.DictReader(f))
    except FileNotFoundError:
        pass


def save_picks():
    with open(PICKS_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for p in picks:
            w.writerow({k: p.get(k, "") for k in FIELDS})


def save_state():
    state["sent"] = state["sent"][-3000:]
    state["urls"] = dict(tt_feed.URLS)
    state["nombres_bets"] = dict(tt_betsapi.NOMBRES)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send(text, reply_to=None):
    """Envia un mensaje y devuelve su id (para poder responderle luego)."""
    if LIGA_TAG:
        text = f"{LIGA_TAG}\n{text}"
    print(text, flush=True)
    if not (TG and CHAT):
        return None
    params = {"chat_id": CHAT, "text": text[:4000]}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    # Un pick NO se puede perder por un fallo puntual de Telegram: se reintenta
    # hasta 5 veces (y si Telegram pide esperar, se espera lo que diga).
    for intento in range(5):
        data = urllib.parse.urlencode(params).encode()
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{TG}/sendMessage",
                                        data, timeout=15) as resp:
                return json.loads(resp.read().decode()).get("result", {}).get("message_id")
        except urllib.error.HTTPError as ex:
            body = ""
            try:
                body = ex.read().decode()
            except Exception:
                pass
            print("WARN telegram", ex.code, body[:200])
            if ex.code == 429:                      # demasiados mensajes seguidos
                try:
                    espera = int(json.loads(body)["parameters"]["retry_after"])
                except Exception:
                    espera = 5
                time.sleep(min(espera, 30) + 1)
                continue
            if ex.code == 400 and "reply_to_message_id" in params:
                params.pop("reply_to_message_id")   # el mensaje citado ya no existe
                continue
            if ex.code < 500:
                return None                         # error que no se arregla reintentando
        except Exception as ex:
            print("WARN telegram", ex)
        time.sleep(2 * (intento + 1))
    return None


def odds_now(mid, pick_home):
    """Cuota del pick antes del partido: Bet365 (BetsAPI) si el partido viene de
    BetsAPI, si no Sofascore (Zyla). '' si no hay."""
    mid = tt_feed.ALIAS.get(str(mid), str(mid))
    if mid.startswith("b"):
        try:
            o = tt_feed.tt_betsapi.cuota_prepartido(mid)
        except Exception as ex:
            print("WARN odds BetsAPI", ex)
            return ""
        return f"{(o[0] if pick_home else o[1]):.2f}" if o else ""
    if not tt_feed.KEY or mid.startswith("s"):
        return ""                      # sin Zyla, o partido deducido (sin id real)
    try:
        data = tt_feed._call(URL_ODDS, match_id=mid)
    except Exception as ex:
        print("WARN odds", ex)
        return ""
    def markets(o):
        if isinstance(o, dict):
            if isinstance(o.get("markets"), list):
                return o["markets"]
            for v in o.values():
                r = markets(v)
                if r:
                    return r
        elif isinstance(o, list):
            if o and isinstance(o[0], dict) and "choices" in o[0]:
                return o
            for v in o:
                r = markets(v)
                if r:
                    return r
        return []
    for m in markets(data):
        ch = {str(c.get("name")): c for c in m.get("choices", [])}
        if set(ch) == {"1", "2"}:
            c = ch["1" if pick_home else "2"]
            v = c.get("value") or {}
            dec = v.get("decimal") if isinstance(v, dict) else None
            if dec:
                return f"{float(dec):.2f}"
            fr = c.get("fractionalValue") or (v.get("fractional") if isinstance(v, dict) else None)
            if fr and "/" in str(fr):
                a, b = str(fr).split("/")
                return f"{1 + float(a) / float(b):.2f}"
    return ""


# ------------------------------------------------ cuotas que apuntas tu
APUESTA_DEFECTO = float(os.getenv("APUESTA_DEFECTO", "120"))


def _num(t):
    try:
        return float(t.replace(",", ".").replace("€", ""))
    except ValueError:
        return None


def manual_pick(text, parsed, reply_to):
    """'/pick Apellido 1.88 [120]' -> registra un pick que has apostado tu.
    Busca el partido de hoy de ese jugador (el que no ha terminado, o el
    ultimo que ha terminado) y el bot apunta solo si gana o pierde."""
    words = text.split()[1:]
    hora = next((w for w in words if len(w) in (4, 5) and ":" in w
                 and w.replace(":", "").isdigit()), None)
    words = [w for w in words if w != hora]
    # fecha opcional '29/10' -> partido de otro dia (para apuntar picks atrasados)
    fecha = next((w for w in words if "/" in w and w.replace("/", "").isdigit()
                  and len(w.split("/")) == 2), None)
    if fecha:
        words = [w for w in words if w != fecha]
        try:
            dd, mo = (int(x) for x in fecha.split("/"))
            hoy = datetime.now(tt_feed.TZ)
            d = hoy.replace(month=mo, day=dd)
            if d > hoy:
                d = d.replace(year=hoy.year - 1)
            evs = tt_feed.eventos_dia(d.strftime("%Y-%m-%d"))
            parsed = [p for p in (tt_feed.parse(e) for e in evs) if p]
        except Exception as ex:
            send(f"❓ No he podido leer los partidos del {fecha} ({str(ex)[:80]}). Prueba en un rato.",
                 reply_to)
            return False
    if hora and len(hora) == 4:
        hora = "0" + hora
    nums = [w for w in words if _num(w) is not None]
    name = " ".join(w for w in words if _num(w) is None).strip().lower()
    if not name or not nums or not (1.01 <= _num(nums[0]) <= 50):
        send("❓ Escríbelo así: /pick Apellido Rival 1.88 120   (o /pick Apellido 1.88)", reply_to)
        return False
    cuota = _num(nums[0])
    apuesta = _num(nums[1]) if len(nums) > 1 else APUESTA_DEFECTO
    # '/pick Pesout Motacek 1.97' -> la 2.a palabra es el rival (lo deja claro del todo)
    nw = name.split()
    name, rv = nw[0], (nw[1] if len(nw) > 1 else "")

    def es(p):
        a, b = p["a"].lower(), p["b"].lower()
        return (name in a and rv in b) or (name in b and rv in a)
    cands = [p for p in parsed if es(p)]
    if not cands:
        txt = f"'{name}'" + (f" contra '{rv}'" if rv else "")
        cuando = f"del {fecha}" if fecha else "de hoy"
        send(f"❓ No encuentro ningún partido {cuando} de {txt}. Revisa los apellidos.", reply_to)
        return False
    if hora:
        # Sofascore a veces cambia la hora al empezar el partido (18:35 -> 18:41):
        # se coge el partido de ese jugador mas cercano a esa hora (max. 30 min)
        hh, mm = int(hora[:2]), int(hora[3:])
        def gap(p):
            d = datetime.fromtimestamp(p["t"], tt_feed.TZ)
            return abs((d.hour * 60 + d.minute) - (hh * 60 + mm))
        cands = [p for p in cands if gap(p) <= 30]
        if not cands:
            send(f"❓ No encuentro el partido de '{name}' cerca de las {hora}. Revisa la hora.", reply_to)
            return False
        cands = [min(cands, key=gap)]
    vivos = [p for p in cands if not p["final"]]
    if vivos:
        m = min(vivos, key=lambda p: p["t"])           # el proximo o el que se juega
    else:
        m = max(cands, key=lambda p: p["t"])           # el ultimo terminado
    pick = m["a"] if (name in m["a"].lower() and rv in m["b"].lower()) else m["b"]
    rival = m["b"] if pick == m["a"] else m["a"]
    if any(p.get("match_id") == m["id"] and p.get("pick") == pick for p in picks):
        # ya estaba registrado (por ejemplo, el bot lo mando): solo se pone la cuota
        pk = next(p for p in picks if p.get("match_id") == m["id"] and p.get("pick") == pick)
        pk["cuota"], pk["apuesta"], pk["enviado"] = f"{cuota:.2f}", f"{apuesta:.0f}", "si"
    else:
        picks.append({"fecha": datetime.fromtimestamp(m["t"], tt_feed.TZ).strftime("%Y-%m-%d"),
                      "hora": datetime.fromtimestamp(m["t"], tt_feed.TZ).strftime("%H:%M"),
                      "tipo": "OFICIAL", "pick": pick, "rival": rival,
                      "h2h_pick": "", "h2h_rival": "", "aviso_puntos": "",
                      "match_id": m["id"], "resultado": "", "marcador": "",
                      "momento": "manual", "calidad": "manual", "enviado": "si",
                      "cuota": f"{cuota:.2f}", "apuesta": f"{apuesta:.0f}", "msg_id": ""})
    if fecha and m["final"]:
        # partido de otro dia que ya termino: el resultado se apunta ahora mismo
        pk = next(p for p in picks if p.get("match_id") == m["id"] and p.get("pick") == pick)
        sa, sb = m["final"]
        home = pick == m["a"]
        pk["resultado"] = "G" if ((sa > sb) if home else (sb > sa)) else "P"
        pk["marcador"] = f"{sa}-{sb}" if home else f"{sb}-{sa}"
        fin = f"Resultado: {'✅ ganado' if pk['resultado'] == 'G' else '❌ perdido'} {pk['marcador']}"
    else:
        fin = "El resultado se apuntará solo."
    send(f"✅ Registrado a mano: {pick} vs {rival} · "
         f"{datetime.fromtimestamp(m['t'], tt_feed.TZ).strftime('%d/%m %H:%M')} @ {cuota:.2f} · "
         f"{apuesta:.0f} €\n{fin}", reply_to)
    return True


def delete_manual(text, reply_to):
    """'/borrar Apellido [Rival]' -> quita el ultimo pick puesto a mano de ese
    jugador (si se pone el rival, solo el de ese partido)."""
    nw = text.split()[1:]
    name = nw[0].lower() if nw else ""
    rv = nw[1].lower() if len(nw) > 1 else ""
    hits = [p for p in picks if p.get("calidad") == "manual" and name
            and name in p["pick"].lower() and rv in p["rival"].lower()]
    if not hits:
        txt = f"'{name}'" + (f" contra '{rv}'" if rv else "")
        send(f"❓ No hay ningún pick puesto a mano de {txt}.", reply_to)
        return False
    pk = hits[-1]
    picks.remove(pk)
    send(f"🗑 Borrado: {pk['pick']} vs {pk['rival']} · {pk['fecha'][8:10]}/{pk['fecha'][5:7]} "
         f"{pk['hora']} @ {pk.get('cuota', '')}", reply_to)
    return True


def grupos_text(parsed, groups):
    """'/grupos' -> los grupos que el bot ve ahora mismo y en que estado."""
    now = time.time()
    vivos = [g for g in groups if g.pending()]
    L = [f"🔎 {LIGA}: {len(parsed)} partidos hoy en los datos · {len(groups)} grupos "
         f"formados · {len(vivos)} sin terminar",
         f"Fuente: {tt_feed.FUENTE.get('modo', 'consulta por torneo')}"]
    for g in sorted(vivos, key=lambda g: g.matches[0].time)[:15]:
        jug = sum(1 for x in g.matches if x.final)
        vivo = next((x for x in g.matches if x.live is not None), None)
        flag = (f" ⚠️ grupo de 5 (con {g.quinto})" if getattr(g, "quinto", None)
                else " ⚠️ datos dudosos" if getattr(g, "dudoso", False)
                else " 👥 grupo de 5" if getattr(g, "cinco", False) else "")
        estado = f"jugando {vivo.a.split()[0]}–{vivo.b.split()[0]}" if vivo else "esperando"
        L.append(f"• {g.matches[0].time} {' · '.join(p.split()[0] for p in g.players)}: "
                 f"{jug}/{len(g.matches)} jugados, {estado}{flag}")
    sueltos = [p for p in parsed if p["t"] > now - 3 * 3600 and
               not any(p["id"] in {x.mid for x in g.matches} for g in groups)]
    if sueltos:
        L.append(f"Partidos de las últimas 3 h que NO encajan en ningún grupo: {len(sueltos)}")
    return "\n".join(L)[:3900]


def ver_text(name, parsed, groups):
    """'/ver Apellido' -> lo que el bot esta viendo AHORA del grupo de ese jugador."""
    name = name.strip().lower()
    if not name:
        return "❓ Escríbelo así: /ver Apellido"
    mine = [p for p in parsed if name in p["a"].lower() or name in p["b"].lower()]
    if not mine:
        return f"🔎 No veo ningún partido de hoy de '{name}' en los datos de Sofascore."
    gs = [g for g in groups if any(name in pl.lower() for pl in g.players)]
    L = []
    for g in gs:
        L.append("🔎 Grupo: " + " · ".join(g.players)
                 + (f" ⚠️ PARECE GRUPO DE 5 (con {g.quinto}): no se avisa" if getattr(g, "quinto", None)
                    else " ⚠️ DATOS DUDOSOS (no se avisa)" if getattr(g, "dudoso", False)
                    else " 👥 GRUPO DE 5 (pasan los 2 primeros)" if getattr(g, "cinco", False) else ""))
        for x in g.matches:
            if x.final:
                st = f"{x.final[0]}-{x.final[1]}"
            elif x.live is not None:
                st = f"en juego {x.live[0]}-{x.live[1]}"
            else:
                st = "por jugar"
            L.append(f"  {x.order}. {x.time} {x.a} – {x.b}: {st}")
        if not g.pending():
            L.append("  Grupo terminado.")
            continue
        try:
            res = g.analyze()
        except Exception as ex:
            res = []
            L.append(f"  ❗ error al analizar: {ex}")
        if not res:
            L.append("  Análisis: ahora mismo no hay ningún pick posible.")
        for r in res:
            m = r["match"]
            cond = " y ".join(f"gane {w}" for _, w in r["conds"]) or "seguro"
            if r.get("partial"):
                cond += f" ({r['partial']})"
            L.append(f"  → {m.time} {kind(r)} {r['pick']} vs {r['vs']}: {cond}"
                     + (" · ⚠️ puntos" if r["warn"] else ""))
    sueltos = [p for p in mine if not any(p["id"] in {x.mid for x in g.matches} for g in groups)]
    if sueltos:
        L.append(f"🔎 Partidos de '{name}' que NO he podido meter en ningún grupo:")
        for p in sorted(sueltos, key=lambda p: p["t"])[:8]:
            h = datetime.fromtimestamp(p["t"], tt_feed.TZ).strftime("%H:%M")
            L.append(f"  {h} {p['a']} – {p['b']}: {p['score']} ({p['status']})")
    return "\n".join(L)[:3900]


AYUDA = ("📖 Lo que puedes escribirme:\n"
         "• 1.65 → (respondiendo a un pick) la cuota a la que apostaste · 'no' si no apostaste\n"
         "• grupos (o estado) → los grupos que estoy siguiendo ahora\n"
         "• ver Apellido → lo que veo del grupo de ese jugador\n"
         "• resumen → el resumen al momento\n"
         "• pick Apellido Rival 1.88 120 → apuntar a mano una apuesta (añade 09/11 si es de otro día)\n"
         "• borrar Apellido Rival → quitar una apuesta puesta a mano")


def read_replies(parsed=(), groups=()):
    """Lee lo que escribes al bot:
       - respondiendo a un pick: '1.45'  o  '1.45 120'  o  'no'
       - sin responder: se aplica al ultimo pick avisado que no tenga cuota
       - '/pick Apellido 1.88': registra a mano un pick que has apostado tu
       - '/resumen': manda el resumen al momento"""
    if not (TG and CHAT and LEER_MENSAJES):
        return
    url = (f"https://api.telegram.org/bot{TG}/getUpdates?timeout=0"
           f"&offset={state.get('tg_offset', 0)}")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            ups = json.loads(resp.read().decode()).get("result", [])
    except Exception as ex:
        print("WARN getUpdates", ex)
        return
    changed = False
    for u in ups:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != CHAT:
            continue
        text = (msg.get("text") or "").strip().lower()
        if not text:
            continue
        # se aceptan las ordenes con o sin "/" (estado, grupos, resumen, ayuda...)
        palabra = text.split()[0].lstrip("/")
        if palabra in ("estado", "grupos", "resumen", "ver", "pick", "borrar", "ayuda", "help", "start"):
            text = "/" + text.lstrip("/")
        if palabra == "estado":
            text = "/grupos"
        if text.startswith(("/ayuda", "/help", "/start")):
            send(AYUDA, msg.get("message_id"))
            continue
        if text.startswith("/resumen"):
            send(summary_text(datetime.now(tt_feed.TZ).strftime("%Y-%m-%d")))
            continue
        if text.startswith("/grupos"):
            send(grupos_text(parsed, groups), msg.get("message_id"))
            continue
        if text.startswith("/ver"):
            send(ver_text(" ".join(text.split()[1:]), parsed, groups), msg.get("message_id"))
            continue
        if text.startswith("/pick"):
            if manual_pick(text, parsed, msg.get("message_id")):
                changed = True
            continue
        if text.startswith("/borrar"):
            if delete_manual(text, msg.get("message_id")):
                changed = True
            continue
        target = None
        rep = (msg.get("reply_to_message") or {}).get("message_id")
        if rep:
            target = next((p for p in picks if str(p.get("msg_id")) == str(rep)), None)
        if target is None:
            cand = [p for p in picks if p.get("enviado") != "no" and p.get("msg_id")
                    and not p.get("cuota")]
            target = cand[-1] if cand else None
        if target is None:
            send("❓ No sé a qué pick se refiere. Responde (deslizando) al mensaje del pick.",
                 msg.get("message_id"))
            continue
        if text in ("no", "no apostado", "x", "-"):
            target["cuota"], target["apuesta"] = "no", ""
            send(f"👍 Apuntado: {target['pick']} NO apostado", msg.get("message_id"))
            changed = True
            continue
        nums = [n for n in (_num(t) for t in text.split()) if n is not None]
        if not nums:
            send("❓ No te entiendo.\n" + AYUDA, msg.get("message_id"))
            continue
        if not (1.01 <= nums[0] <= 50):
            send("❓ Escribe la cuota así: 1.45   (o 1.45 120 si cambias la cantidad)",
                 msg.get("message_id"))
            continue
        cuota = nums[0]
        apuesta = nums[1] if len(nums) > 1 else APUESTA_DEFECTO
        target["cuota"], target["apuesta"] = f"{cuota:.2f}", f"{apuesta:.0f}"
        send(f"✅ Apuntado: {target['pick']} @ {cuota:.2f} · {apuesta:.0f} €", msg.get("message_id"))
        changed = True
    if changed:
        save_picks()


def euros(rows):
    """(apostado, beneficio, n) de los picks con cuota apuntada y resultado."""
    ap = ben = 0.0
    n = 0
    for p in rows:
        c, a, res = p.get("cuota"), p.get("apuesta"), p.get("resultado")
        if not c or c == "no" or not a or res not in ("G", "P"):
            continue
        c, a = float(c), float(a)
        ap += a
        ben += a * (c - 1) if res == "G" else -a
        n += 1
    return ap, ben, n


def euro_line(label, rows):
    ap, ben, n = euros(rows)
    if not n:
        return f"{label}: sin cuotas apuntadas"
    return (f"{label}: {n} apuestas · {ap:.0f} € apostados · "
            f"{'+' if ben >= 0 else ''}{ben:.0f} € ({100 * ben / ap:+.1f}%)")


def startup_report():
    lines = [f"🟢 Avisador {LIGA} activo (v{VERSION})" + (" (MODO PRUEBA)" if TEST else ""),
             "Solo OFICIALES · ⭐ = alta calidad · cada pick trae su cuota mínima",
             "✍️ Responde a cada pick con tu cuota (ej. 1.45) · /resumen = resumen al momento",
             "✍️ /pick Apellido Rival 1.88 = registrar a mano un pick que apostaste",
             "🗑 /borrar Apellido Rival = quitar un pick puesto a mano",
             "🔎 /ver Apellido = lo que el bot ve ahora del grupo de ese jugador",
             "🔎 /grupos = todos los grupos que el bot está siguiendo ahora"]
    try:
        events, live = fetch()
        groups, parsed = build_groups(events)
        lines.append(f"✅ Datos OK ({tt_feed.FUENTE.get('modo', 'Zyla')}): {len(parsed)} partidos "
                     f"{LIGA} hoy, {live} en vivo, {len(groups)} grupos")
    except Exception as ex:
        lines.append(f"❌ No pude leer {LIGA} ({'BetsAPI' if tt_feed.usa_betsapi() else 'Zyla'}): {ex}")
    if not tt_feed.usa_betsapi():
        lines.append("✅ H2H activo" if tt_feed.URL_H2H else "⏳ H2H pendiente de configurar")
    send("\n".join(lines))


def kind(r):
    return "OFICIAL" if "eliminado" in r["reason"] else "POTENCIAL"


def h2h_for(m, pick):
    """(victorias del pick, victorias del rival) o None."""
    d = h2h(m.mid) if m.mid else None
    if not d:
        return None
    return d if pick == m.a else (d[1], d[0])


# Resultado del historico (20/07-17/10, 234 picks oficiales):
#   ⭐ sin aviso de puntos + rival con 2 sets o menos en el grupo -> 84% (cuota min 1.30)
#   normal de dia (antes de las 20:00)                              -> 77% (cuota min 1.55)
#   normal de noche                                                 -> 48% (no se avisa)
#   POTENCIAL: 43%, peor que el nivel de los jugadores              -> no se avisa
# Los no avisados se siguen guardando en picks.csv para medirlos.
CUOTA_ESTRELLA = os.getenv("CUOTA_ESTRELLA", "1.30")
CUOTA_NORMAL = os.getenv("CUOTA_NORMAL", "1.45")
AVISAR_POTENCIAL = os.getenv("AVISAR_POTENCIAL", "0") == "1"


def is_night(hhmm):
    """Noche a efectos de filtro (si la liga no usa filtro de noche, nunca)."""
    return FILTRO_NOCHE and bool(hhmm) and (hhmm >= "20:00" or hhmm < "06:00")


def rival_sets(g, rival):
    return sum((m.final[0] if m.a == rival else m.final[1])
               for m in g.matches if m.final and rival in (m.a, m.b))


def quality(r, g):
    """'estrella', 'normal' o 'noche' (solo para OFICIAL)."""
    if ESTRELLAS and not r["warn"] and rival_sets(g, r["vs"]) <= 2:
        return "estrella"
    return "noche" if is_night(r["match"].time) else "normal"


def confirm_text(r, g, hh, early=False):
    m = r["match"]
    k = kind(r)
    cal = quality(r, g)
    when = f" · {m.time}" if m.time else ""
    if early:
        when += "\n🔴 YA EMPEZADO (1.er set): solo si aún puedes apostar en vivo"
    rival = "eliminado" if k == "OFICIAL" else "ya clasificado"
    if k == "OFICIAL":
        star = "⭐ " if cal == "estrella" else ""
        head = f"🟢🟢 {star}PICK OFICIAL: {r['pick'].upper()} 🟢🟢"
        cuota = CUOTA_ESTRELLA if cal == "estrella" else CUOTA_NORMAL
    else:
        head = f"🟡 PICK POTENCIAL: {r['pick'].upper()}"
        cuota = None
    hline = f"H2H {r['pick']} {hh[0]}-{hh[1]} {r['vs']}" if hh else "Sin H2H"
    txt = f"{head}\nvs {r['vs']} ({rival}){when}"
    if getattr(g, "cinco", False):
        txt += "\n👥 Grupo de 5 (pasan los 2 primeros)"
    if cuota:
        txt += f"\n💶 Apuesta solo con cuota ≥ {cuota}"
    txt += f"\n{hline}"
    if r["warn"]:
        txt += "\n⚠️ puede decidir por puntos"
    if depende_de_puntos(g, r):
        txt += "\n🔢 Lo decide un desempate por PUNTOS de los sets (dato de BetsAPI): algo más frágil"
    txt += f"\n🔎 Puntos: {table_line(g)}"
    return txt


def depende_de_puntos(g, r):
    """¿El pick solo es seguro gracias a los PUNTOS de los sets ya jugados? (sin
    esos puntos, el desempate quedaria sin decidir)."""
    try:
        ms = [Match(x.a, x.b, x.order, time=x.time, final=x.final, live=x.live, mid=x.mid)
              for x in g.matches]
        g2 = Group(g.players, ms)
        m2 = next(x for x in g2.matches if x.order == r["match"].order)
        return not safe_pick(g2, m2, r["pick"], {})
    except Exception as ex:
        print("WARN depende_de_puntos", ex)
        return False


def late_text(r, cal):
    """Confirmacion de un pick avisado cuyo partido ya ha empezado."""
    m = r["match"]
    sa, sb = m.live or (0, 0)
    star = "⭐ " if cal == "estrella" else ""
    cuota = CUOTA_ESTRELLA if cal == "estrella" else CUOTA_NORMAL
    tipo = "🟡 PICK CASI SEGURO" if r.get("casi") else "PICK OFICIAL"
    return (f"✅ SE CUMPLIÓ → {star}{tipo} {r['pick'].upper()} vs {r['vs']} · {m.time}\n"
            f"🔴 El partido ya ha empezado ({m.a} {sa}-{sb} {m.b} en sets).\n"
            f"Si apostaste con el 🔔, está bien hecho. Si no, solo en vivo y con cuota ≥ {cuota}.\n"
            f"✍️ Responde con tu cuota (ej. 1.45) o 'no'")


def table_line(g):
    """Puntos de la tabla tal como los calcula el bot (victoria 2, derrota 1)."""
    pts = {p: 0 for p in g.players}
    for m in g.matches:
        if m.final:
            w, l = (m.a, m.b) if m.final[0] > m.final[1] else (m.b, m.a)
            pts[w] += 2
            pts[l] += 1
    return " · ".join(f"{p.split()[0]} {v}" for p, v in
                      sorted(pts.items(), key=lambda x: -x[1]))


def register_pick(r, hh, early=False, cal="", enviado=True, msg_id=None):
    m = r["match"]
    if any(p.get("match_id") == m.mid and p.get("pick") == r["pick"]
           and p.get("tipo") != "OFICIAL B" for p in picks):
        return          # ya estaba apuntado (nunca dos veces el mismo pick)
    cuota_sofa = odds_now(m.mid, r["pick"] == m.a) if kind(r) == "OFICIAL" else ""   # (incluye CASI)
    picks.append({"cuota_aviso": cuota_sofa,
                  "momento": "vivo" if early else "antes", "msg_id": msg_id or "",
                  "calidad": cal, "enviado": "si" if enviado else "no",
                  "fecha": datetime.now(tt_feed.TZ).strftime("%Y-%m-%d"), "hora": m.time,
                  "tipo": "CASI" if r.get("casi") else kind(r), "pick": r["pick"], "rival": r["vs"],
                  "h2h_pick": hh[0] if hh else "", "h2h_rival": hh[1] if hh else "",
                  "aviso_puntos": "si" if r["warn"] else "no",
                  "match_id": m.mid, "resultado": "", "marcador": ""})
    save_picks()


def update_results(parsed):
    """Marca G/P en los picks cuyo partido ya ha terminado."""
    by_id = {p["id"]: p for p in parsed}
    changed = False
    for pk in picks:
        if pk.get("resultado"):
            continue
        ev = by_id.get(pk["match_id"])
        if not ev:
            continue
        if ev["final"]:
            sa, sb = ev["final"]
            pick_home = pk["pick"] == ev["a"]
            won = (sa > sb) if pick_home else (sb > sa)
            pk["resultado"] = "G" if won else "P"
            pk["marcador"] = f"{sa}-{sb}" if pick_home else f"{sb}-{sa}"
            if pk.get("tipo") in ("OFICIAL", "CASI") and not pk.get("cuota_cierre"):
                pk["cuota_cierre"] = odds_now(pk["match_id"], pick_home)
                if not pk.get("cuota_aviso"):
                    pk["cuota_aviso"] = pk["cuota_cierre"]
            changed = True
        elif ev["status"] not in ("notstarted", "inprogress"):
            pk["resultado"] = "N"          # cancelado / no jugado
            changed = True
    # picks de BetsAPI ya resueltos sin cuota de la casa (de antes de este cambio):
    # se completan poco a poco (como mucho 3 por vuelta)
    hechos = 0
    for pk in picks:
        if hechos >= 3:
            break
        if (pk.get("tipo") in ("OFICIAL", "CASI") and pk.get("resultado") in ("G", "P")
                and str(pk.get("match_id", "")).startswith("b") and not pk.get("cuota_cierre")
                and not pk.get("_sin_cuota")):
            hechos += 1
            ev = by_id.get(pk["match_id"])
            if ev:
                ph = pk["pick"] == ev["a"]
            else:                                   # partido de otro dia: se lee aparte
                e2 = tt_feed.event(pk["match_id"])
                if not e2:
                    pk["_sin_cuota"] = 1
                    continue
                ph = pk["pick"] == (e2.get("homeTeam") or {}).get("name")
            c = odds_now(pk["match_id"], ph)
            if c:
                pk["cuota_cierre"] = c
                pk["cuota_aviso"] = pk.get("cuota_aviso") or c
                changed = True
            else:
                pk["_sin_cuota"] = 1
    if changed:
        save_picks()


def unidades(rows, campo):
    """(n, unidades, sin_cuota): 1 u por pick con la cuota de ese campo."""
    n = sin = 0
    u = 0.0
    for p in rows:
        if p.get("resultado") not in ("G", "P"):
            continue
        try:
            c = float(str(p.get(campo) or "").replace(",", "."))
        except ValueError:
            c = 0
        if campo == "cuota_casa":
            try:
                c = float(p.get("cuota_aviso") or p.get("cuota_cierre") or 0)
            except ValueError:
                c = 0
        if c <= 1:
            sin += 1
            continue
        n += 1
        u += (c - 1) if p["resultado"] == "G" else -1
    return n, u, sin


def u_line(label, rows, campo):
    n, u, sin = unidades(rows, campo)
    extra = f" ({sin} sin cuota)" if sin else ""
    if not n:
        return f"{label}: sin cuotas{extra}"
    return f"{label}: {n} picks · {u:+.2f} u · yield {100 * u / n:+.1f}%{extra}"


def _stats(rows):
    g = sum(1 for p in rows if p.get("resultado") == "G")
    l = sum(1 for p in rows if p.get("resultado") == "P")
    n = g + l
    pct = (100 * g / n) if n else 0
    return g, l, n, pct


def _line(label, rows):
    g, l, n, pct = _stats(rows)
    if not n:
        return f"{label}: sin picks resueltos"
    # con pocos picks la cifra no significa nada: no se muestra hasta tener 30
    if n >= 30:
        be = f" · cuota mínima rentable {100 / pct:.2f}" if pct else ""
    else:
        be = " · pocos picks todavía"
    return f"{label}: {n} picks · {g}✅ {l}❌ ({pct:.0f}%){be}"


def summary_text(today):
    env = [p for p in picks if p.get("enviado") != "no"]          # los que te llegaron
    calla = [p for p in picks if p.get("enviado") == "no"]        # guardados sin avisar
    hoy = [p for p in env if p.get("fecha") == today]
    lines = [f"📊 RESUMEN {LIGA} {today[8:10]}/{today[5:7]}"]
    lines.append(_line("Hoy (avisados)", hoy))
    lines.append("")
    lines.append(_line("TOTAL avisados", env))
    lines.append(_line("  🟢 OFICIAL", [p for p in env if p.get("tipo") == "OFICIAL"]))
    lines.append(_line("  🟡 CASI SEGURO", [p for p in env if p.get("tipo") == "CASI"]))
    lines.append(_line("  ⭐ ESTRELLA", [p for p in env if p.get("calidad") == "estrella"]))
    lines.append(_line("  🟢 normal", [p for p in env if p.get("calidad") == "normal"]))
    fav = [p for p in env if p.get("h2h_pick") not in ("", None)
           and int(p["h2h_pick"]) > int(p["h2h_rival"])]
    contra = [p for p in env if p.get("h2h_pick") not in ("", None)
              and int(p["h2h_pick"]) < int(p["h2h_rival"])]
    lines.append(_line("  H2H a favor", fav))
    lines.append(_line("  H2H en contra", contra))
    lines.append("")
    lines.append("Sin avisar (solo seguimiento):")
    lines.append(_line("  🔵 OFICIAL B", [p for p in calla if p.get("tipo") == "OFICIAL B"]))
    lines.append(_line("  🟡 POTENCIAL", [p for p in calla if p.get("tipo") == "POTENCIAL"]))
    lines.append(_line("  🌙 oficial noche", [p for p in calla if p.get("calidad") == "noche"]))
    lines.append("")
    lines.append("📈 EN UNIDADES (1 u por pick)")
    lines.append(u_line("  Hoy · tus cuotas", hoy, "cuota"))
    lines.append(u_line("  Hoy · cuota de la casa", hoy, "cuota_casa"))
    lines.append(u_line("  Total · tus cuotas", env, "cuota"))
    lines.append(u_line("  Total · cuota de la casa", env, "cuota_casa"))
    lines.append("")
    lines.append("💶 TU DINERO (cuotas que has apuntado)")
    lines.append(euro_line("  Hoy", hoy))
    lines.append(euro_line("  Total", env))
    lines.append(euro_line("  ⭐ estrella", [p for p in env if p.get("calidad") == "estrella"]))
    lines.append(euro_line("  🟢 normal", [p for p in env if p.get("calidad") == "normal"]))
    sin = sum(1 for p in env if p.get("msg_id") and not p.get("cuota"))
    if sin:
        lines.append(f"  ✍️ Picks sin cuota apuntada: {sin}")
    pend = sum(1 for p in env if not p.get("resultado"))
    if pend:
        lines.append(f"⏳ Pendientes de resultado: {pend}")
    return "\n".join(lines)


# ------------------------------------------- vigilante de los otros bots
VIGILAR = os.getenv("VIGILAR", "").strip()          # p. ej. "usuario/tt-multiliga"
VIGILADOS = [("seguimiento/estado.json", "Seguimiento TT Cup y Elite", "Seguimiento TT Cup"),
             ("seguimiento/rating_estado.json", "Rating Liga Pro", "Rating Liga Pro en vivo")]
VIGIA_MIN = int(os.getenv("VIGIA_MIN", "40"))       # minutos sin dar señales -> aviso


def vigilar_otros():
    """Cada 10 min mira cuando guardaron por ultima vez los bots de tt-multiliga.
    Si alguno lleva mas de VIGIA_MIN minutos callado, avisa (una vez) y avisa
    tambien cuando vuelve. Nunca para este avisador si algo falla."""
    if not VIGILAR or time.time() - state.get("vigia_ts", 0) < 600:
        return
    state["vigia_ts"] = time.time()
    alertas = state.setdefault("vigia_alerta", {})
    cab = {"User-Agent": "tt-avisador", "Accept": "application/vnd.github+json"}
    if os.getenv("GH_TOKEN"):
        cab["Authorization"] = f"Bearer {os.getenv('GH_TOKEN')}"
    for path, nombre, workflow in VIGILADOS:
        try:
            url = (f"https://api.github.com/repos/{VIGILAR}/commits?"
                   + urllib.parse.urlencode({"path": path, "per_page": 1}))
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=20) as r:
                    d = json.loads(r.read().decode())
            except urllib.error.HTTPError as ex:
                if ex.code not in (401, 403) or "Authorization" not in cab:
                    raise
                cab.pop("Authorization")              # el token no vale aqui: sin token
                with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=20) as r:
                    d = json.loads(r.read().decode())
            fecha = d[0]["commit"]["committer"]["date"]
            ult = datetime.fromisoformat(fecha.replace("Z", "+00:00")).timestamp()
        except Exception as ex:
            print("WARN vigia", nombre, ex)
            continue
        mins = (time.time() - ult) / 60
        if mins > VIGIA_MIN and not alertas.get(path):
            alertas[path] = True
            send(f"⚠️ El bot {nombre} lleva {mins:.0f} min sin dar señales (puede estar parado).\n"
                 f"Arráncalo: {VIGILAR.split('/')[-1]} → Actions → {workflow} → Run workflow.\n"
                 "Las cuotas que le mandes mientras tanto no se pierden: las lee al volver.")
        elif mins <= VIGIA_MIN and alertas.get(path):
            alertas[path] = False
            send(f"✅ El bot {nombre} vuelve a funcionar.")


def maybe_daily_summary():
    now = datetime.now(tt_feed.TZ)
    today = now.strftime("%Y-%m-%d")
    if now.strftime("%H:%M") >= SUMMARY_HOUR and state.get("last_summary") != today:
        state["last_summary"] = today
        send(summary_text(today))


def score_detail(r, g, pm, w):
    """Para el aviso: con que marcadores de 'pm' el pick queda seguro y con
    cuales dependeria de los puntos (o no habria pick)."""
    if g is None or not w:
        return ""
    seguro, puntos, nada = [], [], []
    for f in pm.possible_finals():
        if (f[0] > f[1]) != (w == pm.a):
            continue
        r2 = g._eval_match(r["match"], {pm.order: [f]})
        txt = f"{f[0]}-{f[1]}" if w == pm.a else f"{f[1]}-{f[0]}"
        if not r2 or r2["pick"] != r["pick"]:
            nada.append(txt)
        elif r2["warn"]:
            puntos.append(txt)
        else:
            seguro.append(txt)
    if not (puntos or nada):
        return ""
    out = []
    if seguro:
        out.append(f"✅ {w} gana {' o '.join(seguro)} → pick seguro")
    if puntos:
        out.append(f"⚠️ {w} gana {' o '.join(puntos)} → decide por puntos (dudoso)")
    if nada:
        out.append(f"❌ {w} gana {' o '.join(nada)} → no hay pick seguro (puede decidir por puntos)")
    return "\n" + "\n".join(out)


def heads_up_text(r, pm, w, g=None):
    m = r["match"]
    k = kind(r)
    sa, sb = pm.live
    cond = f"Si gana {w}" if w else "Gane quien gane"
    if r["partial"] and w:
        # marcador exacto, contado desde el ganador: "SOLO si gana 3-2"
        res = []
        for s in r["partial"].split(", "):
            x, y = s.split(":")
            res.append(f"{y}-{x}" if w == pm.b else f"{x}-{y}")
        cond = f"SOLO si gana {w} {' o '.join(res)}"
    return (f"⏳ ATENTO: {pm.a} {sa}-{sb} {pm.b} (3.er set o más)\n"
            f"{cond} → posible PICK {k} {r['pick'].upper()} vs {r['vs']}"
            + (f" · {m.time}" if m.time else "")
            + ("" if r["partial"] else score_detail(r, g, pm, w)))


def worth_heads_up(r, g):
    """Solo se anticipan los OFICIALES que pueden acabar avisandose."""
    if kind(r) != "OFICIAL":
        return AVISAR_POTENCIAL
    night = is_night(r["match"].time)
    # de noche solo valen los estrella; si ya no puede serlo, no se avisa
    if night and (r["warn"] or rival_sets(g, r["vs"]) > 2):
        return False
    return True


# marcadores posibles de un set, del mas amplio al mas ajustado
SET_SCORES = [(11, k) for k in range(10)] + [(k + 2, k) for k in range(10, 15)]


def win_sure(g, m, pick, fixed):
    """¿Ganando 'm', 'pick' se clasifica SEGURO en todos los casos posibles?"""
    wins = [f for f in m.possible_finals() if (f[0] > f[1]) == (pick == m.a)]
    fx = dict(fixed)
    fx[m.order] = wins
    return all(st[pick] == "Q" for _, st in g.completions(fx))


def safe_pick(g, m, pick, fixed):
    """Pick OFICIAL de verdad: rival eliminado y, si gana, pasa SEGURO.
    (Si solo hay duda con la derrota -perder ajustado y pasar igual-, vale.)"""
    r2 = g._eval_match(m, fixed)
    if not (r2 and r2["pick"] == pick and kind(r2) == "OFICIAL"):
        return None
    if r2["warn"] and not win_sure(g, m, pick, fixed):
        return None
    return r2


def _pick_with_points(r, g, pm, f, pa, pb):
    """¿Hay pick seguro si 'pm' acaba 'f' con estos puntos totales?"""
    ms = [Match(x.a, x.b, x.order, time=x.time, final=f, points=(pa, pb), mid=x.mid)
          if x is pm else x for x in g.matches]
    g2 = Group(g.players, ms)
    m2 = next(x for x in g2.matches if x.order == r["match"].order)
    return bool(safe_pick(g2, m2, r["pick"], {}))


_SCAN = {}


def pts_scan(r, g, pm, f):
    """Cuando el desempate depende de los PUNTOS del partido decisivo, prueba
    marcadores posibles. Devuelve 'si', 'no' o un texto con la condicion.
    (Se guarda el resultado: con el grupo igual no se vuelve a calcular.)"""
    key = (r["pick"], r["match"].order, pm.order, f,
           tuple((x.final, x.points, x.live, getattr(x, "done_pts", None)) for x in g.matches))
    if key not in _SCAN:
        if len(_SCAN) > 5000:
            _SCAN.clear()
        _SCAN[key] = _pts_scan(r, g, pm, f)
    return _SCAN[key]


def _sumas(n_win, n_lose, exhaustivo):
    """Puntos (del que gana el partido, del que lo pierde) posibles en n_win sets
    ganados + n_lose perdidos. Sin repetir sumas (lo que decide son las sumas)."""
    if exhaustivo:
        opts = SET_SCORES
    else:
        opts = [(11, 0), (11, 3), (11, 6), (11, 8), (11, 9), (12, 10), (14, 12), (16, 14)]
    sumas = {(0, 0)}
    for _ in range(n_win):
        sumas = {(a + w, b + l) for a, b in sumas for w, l in opts}
    for _ in range(n_lose):
        sumas = {(a + l, b + w) for a, b in sumas for w, l in opts}
    return sumas


def _empate_txt(r, g, pm, f):
    """Quien empataria y que lo decide (para explicar el ❓)."""
    m = r["match"]
    ms = []
    for x in g.matches:
        if x is pm:
            ms.append(Match(x.a, x.b, x.order, final=f))
        elif x is m:
            ms.append(Match(x.a, x.b, x.order, final=(3, 1) if r["pick"] == x.a else (1, 3)))
        else:
            ms.append(x if x.final else Match(x.a, x.b, x.order, final=(3, 1)))
    res = {frozenset((x.a, x.b)): (x.a, x.b, x.final[0], x.final[1],
                                   *(x.points if (x.points and x is not pm and x is not m)
                                     else (None, None))) for x in ms}
    from tt_engine import _rank, TIEBREAK
    pos = 0
    for t in _rank(g.players, res):
        if len(t) > 1 and pos < 2 < pos + len(t):
            nombres = ", ".join(p.split()[0] for p in t[:-1]) + " y " + t[-1].split()[0]
            if TIEBREAK == "ligapro":
                regla = "decide la diferencia de puntos en TODO el grupo"
            else:
                regla = "decide el cociente de puntos en los partidos entre ellos"
            return f"empatarían {nombres} en victorias y en sets: {regla}"
        pos += len(t)
    return "lo decide el desempate por puntos"


def _pts_scan(r, g, pm, f):
    """El pick depende de los PUNTOS del partido 'pm' si acaba 'f'. Prueba todas
    las sumas de puntos posibles y lo resume en una condicion clara sobre la
    DIFERENCIA de puntos del partido (sumando todos sus sets).
    Devuelve 'si', 'no' o el texto de la condicion."""
    done = getattr(pm, "done_pts", None) or (0, 0)
    la, lb = pm.live if pm.live else (0, 0)
    left_a, left_b = f[0] - la, f[1] - lb          # sets que faltan a cada uno
    if left_a < 0 or left_b < 0 or left_a + left_b < 1:
        return "no"
    win_a = f[0] > f[1]
    wname = (pm.a if win_a else pm.b).split()[0]
    wl, ll = (left_a, left_b) if win_a else (left_b, left_a)
    dw, dl = (done if win_a else (done[1], done[0]))   # puntos ya jugados (ganador, perdedor)
    por_dif = {}
    for pw, pl in _sumas(wl, ll, exhaustivo=True):
        tw, tl = dw + pw, dl + pl
        pa, pb = (tw, tl) if win_a else (tl, tw)
        por_dif.setdefault(tw - tl, set()).add(_pick_with_points(r, g, pm, f, pa, pb))
    todos = set().union(*por_dif.values())
    if todos == {True}:
        return "si"
    if todos == {False}:
        return "no"
    expl = _empate_txt(r, g, pm, f)
    difs = sorted(por_dif)
    lname, wfull = (pm.b if win_a else pm.a).split()[0], wname
    quien = f"la diferencia de puntos del partido ({wfull} − {lname}, sumando todos los sets)"
    ok = [d for d in difs if por_dif[d] == {True}]
    no = [d for d in difs if por_dif[d] == {False}]
    mixto = [d for d in difs if len(por_dif[d]) == 2]

    def tramos(vals):
        """[3,4,5,9,10] -> 'entre 3 y 5, o bien 9 o más' usando los extremos posibles."""
        out, ini = [], None
        for i, d in enumerate(difs):
            dentro = d in vals
            if dentro and ini is None:
                ini = d
            if ini is not None and (not dentro or i == len(difs) - 1):
                fin = d if dentro else difs[i - 1]
                if ini == fin:
                    out.append(f"exactamente {ini}")
                elif fin == difs[-1]:
                    out.append(f"{ini} o más")
                elif ini == difs[0]:
                    out.append(f"{fin} o menos")
                else:
                    out.append(f"entre {ini} y {fin}")
                ini = None
        return ", o bien ".join(out)
    if not ok:
        return (f"PICK solo con algunos marcadores exactos · {expl} · te confirmo al acabar")
    if not mixto and len(no) <= 3 and len(tramos(no).split(", o bien ")) <= len(tramos(ok).split(", o bien ")):
        cond = f"PICK salvo que {quien} sea {tramos(no)}"
    else:
        cond = f"PICK si {quien} es {tramos(ok)}"
        if mixto:
            cond += f"; si es {tramos(mixto)}, depende de los puntos exactos (te confirmo al acabar)"
    return f"{cond} · {expl}"


CASI = os.getenv("CASI", "1") == "1"     # avisar los 🟡 PICK CASI SEGURO


def _marc(f, pick, m):
    """Marcador del partido m visto desde 'pick' (p. ej. '3-1')."""
    a, b = (f[0], f[1]) if pick == m.a else (f[1], f[0])
    return f"{a}-{b}"


def casi_pick(g, m, pick, fixed):
    """🟡 PICK CASI SEGURO: el rival esta ELIMINADO pase lo que pase, si el pick
    pierde queda fuera, y ganando pasa SEGURO con 2 de los 3 marcadores (con el
    otro, lo deciden los puntos u otros resultados). Devuelve la descripcion o None."""
    if not CASI:
        return None
    vs = m.b if pick == m.a else m.a
    fins = m.possible_finals()
    gana = [f for f in fins if (f[0] > f[1]) == (pick == m.a)]
    pierde = [f for f in fins if f not in gana]
    if not gana or not pierde:
        return None

    def st(p, permitidos):
        fx = dict(fixed)
        fx[m.order] = permitidos
        return {x[p] for _, x in g.completions(fx)}
    if st(vs, fins) != {"O"}:
        return None
    nl = st(pick, pierde)
    if not (nl <= {"O", "A"} and "O" in nl):
        return None
    seguros, dudas = [], []
    for f in gana:
        x = st(pick, [f])
        (seguros if x == {"Q"} else dudas).append((f, x))
    if len(seguros) < 2 or not dudas:
        return None
    ok = " o ".join(_marc(f, pick, m) for f, _ in seguros)
    partes = []
    for f, x in dudas:
        if x == {"O"}:
            partes.append(f"con {_marc(f, pick, m)} quedaría fuera")
        elif "A" in x:
            partes.append(f"con {_marc(f, pick, m)} lo deciden los puntos")
        else:
            partes.append(f"con {_marc(f, pick, m)} depende de otros resultados")
    return f"pasa seguro si gana {ok}; " + "; ".join(partes)


def casi_text(r, g, early=False):
    m = r["match"]
    when = f" · {m.time}" if m.time else ""
    if early:
        when += "\n🔴 YA EMPEZADO (1.er set): solo si aún puedes apostar en vivo"
    return (f"🟡🟡 PICK CASI SEGURO: {r['pick'].upper()} 🟡🟡\n"
            f"vs {r['vs']} (eliminado){when}\n"
            f"✔️ {r['casi'][0].upper()}{r['casi'][1:]}. Si pierde, fuera.\n"
            f"💶 Apuesta solo con cuota ≥ {CUOTA_NORMAL}\n"
            f"🔎 Puntos: {table_line(g)}")


def outcome(r, g, pm, f):
    """Que pasa con el pick si el partido decisivo 'pm' acaba exactamente 'f'.
    Devuelve (simbolo, texto). ❓ = pick solo con una condicion de puntos (el texto la dice)."""
    m = r["match"]
    if kind(r) != "OFICIAL":
        return "❌", "solo potencial"
    r2 = safe_pick(g, m, r["pick"], {pm.order: [f]})
    if not r2:
        sc = pts_scan(r, g, pm, f)
        if sc == "si":
            r2 = dict(r, warn=False)
        elif sc != "no":
            return "❓", sc
        else:
            c = casi_pick(g, m, r["pick"], {pm.order: [f]})
            if c:
                return "🟡", f"pick CASI seguro: {c} (cuota ≥ {CUOTA_NORMAL})"
            r3 = g._eval_match(m, {pm.order: [f]})
            return "❌", ("lo deciden los puntos" if r3 and r3["pick"] == r["pick"]
                         else "no hay pick seguro")
    rs = rival_sets(g, r2["vs"]) + ((f[0] if pm.a == r2["vs"] else f[1])
                                     if r2["vs"] in (pm.a, pm.b) else 0)
    star = rs <= 2 and ESTRELLAS and not r2["warn"]
    if not star and is_night(m.time):
        return "❌", "oficial de noche sin ⭐"
    return "✅", ("⭐ " if star else "") + f"pick (cuota ≥ {CUOTA_ESTRELLA if star else CUOTA_NORMAL})"


def full_heads_up_text(r, pm, g):
    """ATENTO con cada resultado posible del partido decisivo revisado."""
    m = r["match"]
    sa, sb = pm.live
    lines = [f"⏳ ATENTO (aún NO apuestes): {pm.a} {sa}-{sb} {pm.b}",
             f"Posible PICK OFICIAL {r['pick'].upper()} vs {r['vs']} · {m.time}"]
    for p in (pm.a, pm.b):
        parts = []
        for f in pm.possible_finals():
            if (f[0] > f[1]) != (p == pm.a):
                continue
            sym, txt = outcome(r, g, pm, f)
            parts.append(f"{sym} {max(f)}-{min(f)} {txt}")
        if parts:
            lines.append(f"Si gana {p}:  " + "  ·  ".join(parts))
    lines.append("Espera al 🔔 SET DECISIVO: ahí te digo si apostar.")
    return "\n".join(lines)


def early_table(g, p0, p1, m, pick, vs):
    """Para cada resultado EXACTO de p0 y de p1: ¿pick OFICIAL apostable?
    Devuelve {f0: [(f1, simbolo)]} o None si no merece aviso."""
    tab = {}
    for f0 in p0.possible_finals():
        fila = []
        for f1 in p1.possible_finals():
            r2 = safe_pick(g, m, pick, {p0.order: [f0], p1.order: [f1]})
            if r2:
                rs = rival_sets(g, vs)            # sets del rival al acabar esos dos
                for pmx, fx in ((p0, f0), (p1, f1)):
                    if vs in (pmx.a, pmx.b):
                        rs += fx[0] if pmx.a == vs else fx[1]
                if (rs > 2 or r2["warn"] or not ESTRELLAS) and is_night(m.time):
                    sym = "❌"                    # de noche solo valen los ⭐
                else:
                    sym = "✅"
            else:
                # ¿no hay pick seguro solo porque lo deciden los puntos? -> ❓
                sts = [st for _, st in g.completions({p0.order: [f0], p1.order: [f1]})]
                dudoso = any(st.get(pick) == "A" or st.get(vs) == "A" for st in sts)
                rival_fuera = all(st.get(vs) in ("O", "A") for st in sts)
                sym = "❓" if dudoso and rival_fuera else "❌"
            fila.append((f1, sym))
        tab[f0] = fila
    # se avisa si con algun resultado del 1.er partido hay pick en la mitad o mas
    # de los resultados del 2.º (si no, seria ruido)
    ok = any(sum(x == "✅" for _, x in fila) * 2 >= len(fila) for fila in tab.values())
    return tab if ok else None


def early_text(tab, m, pick, vs, p0, p1):
    def nom(p):
        return p.split()[0]
    lines = [f"🗓 PICK PROBABLE (con tiempo): {pick.upper()} vs {vs} · {m.time}",
             f"Faltan 2 partidos: 1.º {p0.a} – {p0.b}"
             + (f" (va {p0.live[0]}-{p0.live[1]})" if p0.live else f" ({p0.time})")
             + f" · 2.º {p1.a} – {p1.b} ({p1.time})"]
    for f0, fila in tab.items():
        w0 = p0.a if f0[0] > f0[1] else p0.b
        head = f"{nom(w0)} {max(f0)}-{min(f0)}"
        syms = {x for _, x in fila}
        if syms == {"✅"}:
            lines.append(f"• {head} → ✅ pick pase lo que pase en el 2.º")
        elif syms == {"❌"}:
            lines.append(f"• {head} → ❌ no")
        else:
            lines.append(f"• {head} → " + " ".join(
                f"{nom(p1.a if f[0] > f[1] else p1.b)}{max(f)}-{min(f)}{x}" for f, x in fila))
    if any(x == "❓" for fila in tab.values() for _, x in fila):
        lines.append("❓ = pick solo según los puntos: cuando se acerque te digo cuántos hacen falta")
    lines.append("Aún NO apuestes: te voy concretando según acaben.")
    return "\n".join(lines)


def preview_text(r, pm, g):
    """PREAVISO: posible pick y, para CADA resultado del partido del que depende,
    si es pick seguro, pick solo con una condicion de puntos (dicha en numeros) o no."""
    m = r["match"]
    when = f" ({pm.time})" if pm.time else ""
    if pm.live:
        when = f" (va {pm.live[0]}-{pm.live[1]})"
    lines = [f"📋 POSIBLE PICK OFICIAL {r['pick'].upper()} vs {r['vs']} · {m.time}",
             f"Depende de {pm.a} – {pm.b}{when}:"]
    for p in (pm.a, pm.b):
        res = [(f, outcome(r, g, pm, f)) for f in pm.possible_finals()
               if (f[0] > f[1]) == (p == pm.a)]
        if not res:
            continue
        nom = p.split()[0]
        si = [f for f, o in res if o[0] == "✅"]
        no = [f for f, o in res if o[0] == "❌"]
        cond = [(f, o[1]) for f, o in res if o[0] == "❓"]
        casi = [(f, o[1]) for f, o in res if o[0] == "🟡"]
        def marc(fs):
            return " o ".join(f"{max(f)}-{min(f)}" for f in fs)
        if len(si) == len(res):
            lines.append(f"✅ Gana {nom} (cualquier resultado) → {res[0][1][1]}")
            continue
        if len(no) == len(res):
            lines.append(f"❌ Gana {nom} → no hay pick")
            continue
        if si:
            txt = next(o[1] for f, o in res if o[0] == "✅")
            lines.append(f"✅ {nom} gana {marc(si)} → {txt}")
        for f, txt in cond:
            cond_, _, expl = txt.partition(" · ")
            lines.append(f"❓ {nom} gana {max(f)}-{min(f)} → {cond_}")
            if expl:
                lines.append(f"   ({expl})")
        for f, txt in casi:
            lines.append(f"🟡 {nom} gana {max(f)}-{min(f)} → {txt}")
        if no:
            lines.append(f"❌ {nom} gana {marc(no)} → no hay pick")
    lines.append("Aún NO apuestes: te confirmo en cuanto acabe ese partido (con los puntos reales).")
    return "\n".join(lines)


def closing_set_text(r, pm, g):
    """Mensaje para el set que puede terminar el partido decisivo: para cada
    jugador que puede cerrarlo, si con ESE resultado hay pick seguro o no."""
    m = r["match"]
    sa, sb = pm.live
    lines = [f"🔔 SET DECISIVO: {pm.a} {sa}-{sb} {pm.b}"]
    for p in (pm.a, pm.b):
        mine = sa if p == pm.a else sb
        if mine != 2:
            nxt = (sa + 1, sb) if p == pm.a else (sa, sb + 1)
            lines.append(f"↔️ Si gana {p} este set → {nxt[0]}-{nxt[1]}, sigue el partido (te aviso)")
            continue
        f = (3, sb) if p == pm.a else (sa, 3)
        res = f"{max(f)}-{min(f)}"
        sym, txt = outcome(r, g, pm, f)
        if sym == "✅":
            lines.append(f"✅ Si gana {p} ({res}) → {txt.replace('pick', 'PICK OFICIAL ' + r['pick'].upper())}"
                         f" · apuesta YA")
        elif sym == "❓":
            lines.append(f"❓ Si gana {p} ({res}) → {txt.replace('PICK', 'PICK OFICIAL ' + r['pick'].upper())}"
                         f" · espera: te confirmo al acabar")
        elif sym == "🟡":
            lines.append(f"🟡 Si gana {p} ({res}) → {txt.replace('pick CASI', 'PICK CASI').replace('PICK CASI seguro', 'PICK CASI SEGURO ' + r['pick'].upper())}"
                         f" · apuesta YA")
        else:
            lines.append(f"❌ Si gana {p} ({res}) → NO apuestes ({txt})")
    if min(sa, sb) < 2:
        # el partido aun puede alargarse: mapa de TODOS los finales posibles
        mapa = []
        for p in (pm.a, pm.b):
            for f in pm.possible_finals():
                if (f[0] > f[1]) != (p == pm.a):
                    continue
                sym, _ = outcome(r, g, pm, f)
                mapa.append(f"{p.split()[0]} {max(f)}-{min(f)} {sym}")
        lines.append("📋 Todos los casos: " + " · ".join(mapa))
    lines.append(f"Partido del pick: {m.time}")
    return "\n".join(lines)


def deciding_set(pm, w, partial):
    """Solo para picks que dependen del marcador exacto: True si el set que se
    esta jugando decide (ganandolo w, el marcador final es el que hace falta)."""
    if not partial or not pm.live:
        return False
    sa, sb = pm.live
    after = (sa + 1, sb) if w == pm.a else (sa, sb + 1)
    if max(after) != 3:
        return False
    return f"{after[0]}:{after[1]}" in partial.split(", ")


def last_set_text(r, pm, w):
    m = r["match"]
    sa, sb = pm.live
    return (f"🔔 SET DECISIVO: {pm.a} {sa}-{sb} {pm.b}\n"
            f"Si {w} gana ESTE set → PICK {kind(r)} {r['pick'].upper()} vs {r['vs']}"
            + (f" · {m.time}" if m.time else "") + "\nPrepara la apuesta")


def in_late_stage(pm):
    """Mitad del 3.er set o mas avanzado."""
    if not pm.live:
        return False
    played = sum(pm.live)
    return played >= 3 or (played == 2 and pm.live_pts >= 10)


def progress(e):
    """Cuanto ha avanzado un partido: estado, sets y puntos jugados."""
    if not e:
        return -1
    rank = {"notstarted": 0, "inprogress": 1, "finished": 2}.get((e.get("status") or {}).get("type"), 0)
    hs, as_ = e.get("homeScore") or {}, e.get("awayScore") or {}
    def n(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    sets = n(hs.get("current")) + n(as_.get("current"))
    pts = sum(n(hs.get(f"period{i}")) + n(as_.get(f"period{i}")) for i in range(1, 8))
    return rank * 100000 + sets * 1000 + pts


def track_b(g, results, sent):
    """OFICIAL B, solo seguimiento (no se avisa): rival eliminado y pick
    obligado a ganar, aunque ganar no le asegure pasar."""
    try:
        pend = g.pending()
        if not pend:
            return
        m0 = pend[0]
        early0 = m0.live == (0, 0) and m0.live_pts <= LIVE_PTS_MAX
        if not (m0.live is None or early0):
            return
        if any(r["match"].mid == m0.mid and not r["conds"] for r in results):
            return                              # ya es oficial o potencial
        rb = eval_b(g, m0)
        if not rb:
            return
        key = f"B|{m0.mid}|{rb['pick']}"
        if key in sent:
            return
        sent.add(key)
        state["sent"].append(key)
        picks.append({"fecha": datetime.now(tt_feed.TZ).strftime("%Y-%m-%d"), "hora": m0.time,
                      "tipo": "OFICIAL B", "pick": rb["pick"], "rival": rb["vs"],
                      "h2h_pick": "", "h2h_rival": "", "aviso_puntos": "",
                      "match_id": m0.mid, "resultado": "", "marcador": "",
                      "momento": "vivo" if early0 else "antes", "calidad": "",
                      "enviado": "no", "cuota": "", "apuesta": "", "msg_id": ""})
        save_picks()
        print("Seguimiento OFICIAL B:", rb["pick"], "vs", rb["vs"])
    except Exception as ex:
        print("WARN tipo B", ex)


def tick(rapido=None):
    """Una vuelta completa. Con rapido=<id de partido>: AVISO RAPIDO, solo el grupo
    de ese partido, con los datos que ya hay + la lectura fresca de ese partido (sin
    consultar la lista entera ni hacer nada mas). Justo despues va la vuelta normal."""
    if rapido:
        if not LAST["events"]:
            return
        events = list(LAST["events"])
    else:
        HOT["on"] = False
        HOT["ids"] = set()
        tt_feed.ACUM = state.setdefault("acum_vivo", {})   # respaldo en vivo (Liga Pro)
        events, live_total = fetch()
        LAST["events"] = events
    if FRESH:
        # lo leido partido a partido es mas reciente que la lista del torneo
        by = {str(e.get("id")): e for e in events}
        for mid, ev in FRESH.items():
            base = by.get(mid)
            # solo se usa si va IGUAL o MAS ADELANTADO que la lista (nunca un dato viejo)
            if not base or progress(ev) < progress(base):
                continue
            if (str(mid).startswith("b") and (ev.get("status") or {}).get("type") == "finished"
                    and not tt_betsapi.aceptado(mid)):
                continue                     # terminado aun sin confirmar: no se usa
            if ((ev.get("homeTeam") or {}).get("name") != (base.get("homeTeam") or {}).get("name")):
                continue                     # datos de otro formato: no mezclar
            # de la lectura rapida solo se toman marcador y estado; jugadores y hora
            # siguen siendo los de la lista (si faltaran, el grupo no se formaria)
            mix = dict(base)
            for k in ("status", "homeScore", "awayScore", "winnerCode"):
                if ev.get(k) is not None:
                    mix[k] = ev[k]
            by[mid] = mix
        events = list(by.values())
    groups, parsed = build_groups(events)
    if rapido:
        groups = [g for g in groups if any(m.mid == rapido for m in g.matches)]
        return _avisos(groups, rapido=True)

    if TEST and time.time() - state["last_report"] >= REPORT_EVERY:
        state["last_report"] = time.time()
        live = [p for p in parsed if p["status"] == "inprogress"]
        lines = [f"🧪 {LIGA}: {len(parsed)} partidos hoy · {len(live)} en vivo · "
                 f"{len(groups)} grupos"]
        for p in live[:5]:
            lines.append(f"  🔴 {p['a']} {p['score']} {p['b']}")
        send("\n".join(lines))

    # Alarma: si la fuente de datos casi no da partidos de esta liga, avisar
    # (una vez al dia) en vez de quedarse callado como si no hubiera picks.
    ahora = datetime.now(tt_feed.TZ)
    if 10 <= ahora.hour < 23 and len(parsed) < MIN_PARTIDOS and \
            state.get("aviso_pocos_datos") != ahora.strftime("%Y-%m-%d"):
        state["aviso_pocos_datos"] = ahora.strftime("%Y-%m-%d")
        send(f"⚠️ {LIGA}: hoy solo me llegan {len(parsed)} partidos en los datos "
             f"(lo normal es más de 100). Puede que Sofascore haya cambiado algo.\n"
             f"Mientras siga así NO podré avisarte de picks. Lanza 'Explorar ligas' en "
             f"GitHub (Actions) y pásale el resultado a Claude.")
    update_results(parsed)
    read_replies(parsed, groups)
    maybe_daily_summary()
    try:
        vigilar_otros()
    except Exception as ex:                      # nunca debe afectar a los avisos
        print("WARN vigia", ex)
    _avisos(groups, parsed)


def _avisos(groups, parsed=None, rapido=False):
    """Analiza los grupos y manda los avisos de picks. En modo rapido (justo al
    acabar un set o el partido decisivo) solo lo urgente del grupo de ese partido:
    confirmacion del pick y 🔔; lo demas lo hace la vuelta normal de justo despues."""
    sent = set(state["sent"])

    def once(key, text_fn):
        if key in sent:
            return
        sent.add(key)
        state["sent"].append(key)
        send(text_fn())

    def cond_alert(r, m, pm, g):
        """Avisos de un pick que depende del partido 'pm': preaviso, ATENTO y 🔔.
        Solo si con algun resultado de 'pm' sale un pick APOSTABLE (✅ o ❓)."""
        if pm.final or len(g.pending()) > 3:
            return      # grupo aun al principio: no hay picks posibles y saldria carisimo
        outs = {f: outcome(r, g, pm, f) for f in pm.possible_finals()}
        if not any(o[0] in ("✅", "❓", "🟡") for o in outs.values()):
            return
        akey = f"{m.mid}|{r['pick']}"
        if pm.live is not None:
            HOT["on"] = True      # partido decisivo en juego: mirar cada 15 s
            HOT["ids"].add(pm.mid)
            HOT["last"] = time.time()
        info = {"pm": pm.mid, "a": pm.a, "b": pm.b, "pick": r["pick"], "hora": m.time,
                "ts": time.time(), "vs": r["vs"], "reason": r["reason"]}
        pkey = f"P|{akey}|{pm.mid}"
        if pkey not in sent and not (pm.live and max(pm.live) == 2):   # entonces ya va el 🔔
            # PREAVISO con antelacion, con TODOS los resultados posibles
            alerted[akey] = info
            once(pkey, lambda r=r, pm=pm, g=g: preview_text(r, pm, g))
        if in_late_stage(pm) and max(pm.live) < 2 and pkey not in sent:
            alerted[akey] = info
            once(f"H|{akey}|{pm.mid}", lambda r=r, pm=pm, g=g: full_heads_up_text(r, pm, g))
        if pm.live and max(pm.live) == 2:
            # set que puede cerrar el partido: respuesta exacta por resultado
            alerted[akey] = info
            once(f"F|{akey}|{pm.mid}|{pm.live[0]}-{pm.live[1]}",
                 lambda r=r, pm=pm, g=g: closing_set_text(r, pm, g))

    valid = set()        # (match_id, pick) que siguen siendo pick ahora mismo
    cands = set()        # "match|pick" que todavia pueden ser pick
    alerted = state.setdefault("alerted", {})   # avisos ATENTO pendientes de resolver
    checked = set()      # partidos de grupos analizados con datos fiables
    for g in groups:
        if not g.pending():
            continue
        if getattr(g, "dudoso", False):
            # algun resultado del grupo no cuadra en la API: no se avisa de nada
            print("Grupo con datos dudosos, se omite:", g.players)
            continue
        if len(g.pending()) > 3:
            # con 2 partidos jugados o menos nadie puede estar ya eliminado ni
            # clasificado: no hay picks posibles y analizarlo es lo mas lento
            continue
        checked |= {m.mid for m in g.matches}
        results = g.analyze()
        todos = list(results)            # sin filtrar (para el seguimiento OFICIAL B)
        # fuera los "picks" en los que ganar NO asegura el pase (dudas por puntos)
        results = [r for r in results if not (r["warn"] and kind(r) == "OFICIAL" and
                   not win_sure(g, r["match"], r["pick"],
                                {pm.order: [f for f in pm.possible_finals()
                                            if (f[0] > f[1]) == (w == pm.a)]
                                 for pm, w in r["conds"]}))]
        # 🟡 PICK CASI SEGURO en el siguiente partido (si no es ya oficial)
        pend0 = g.pending()[0]
        if not any(r["match"] is pend0 and not r["conds"] for r in results):
            for pick in (pend0.a, pend0.b):
                c = casi_pick(g, pend0, pick, {})
                if c:
                    vs = pend0.b if pick == pend0.a else pend0.a
                    rc = dict(match=pend0, conds=[], partial=False, pick=pick, vs=vs,
                              reason=f"{vs} ya eliminado", warn=True, casi=c)
                    results.append(rc)
                    todos.append(rc)
        track_b(g, todos, sent)         # OFICIAL B (solo seguimiento) si no hay otro pick
        for r in results:
            cands.add(f"{r['match'].mid}|{r['pick']}")
            if not r["conds"]:
                valid.add((r["match"].mid, r["pick"]))
        for r in results:
            m = r["match"]
            if m.final:
                continue
            # regla: picks antes de empezar. Excepcion: si el partido acaba de
            # empezar (1.er set, 0-0 en sets y pocos puntos) se manda igual,
            # marcado como YA EMPEZADO, por si aun se puede apostar en vivo.
            early = m.live == (0, 0) and m.live_pts <= LIVE_PTS_MAX
            if m.live is not None and not early:
                # El partido del pick ya va avanzado. Si antes se mando el ATENTO o
                # el SET DECISIVO de este pick (puede que ya hayas apostado), se
                # confirma igualmente y se registra, para que nunca quede en el aire.
                akey = f"{m.mid}|{r['pick']}"
                key = f"C|{akey}"
                if not r["conds"] and akey in alerted and key not in sent:
                    cal = quality(r, g) if kind(r) == "OFICIAL" else ""
                    if r.get("casi"):
                        cal = "casi"
                    sent.add(key)
                    state["sent"].append(key)
                    mid_tg = send(late_text(r, cal))
                    register_pick(r, None, True, cal, True, mid_tg)
                continue
            if not r["conds"]:
                # pick seguro -> confirmacion (o solo registro si no se avisa)
                key = f"C|{m.mid}|{r['pick']}"
                if key not in sent:
                    k = kind(r)
                    cal = quality(r, g) if k == "OFICIAL" else ""
                    if r.get("casi"):
                        cal = "casi"
                    avisar = (k == "OFICIAL" and cal != "noche") or \
                             (k == "POTENCIAL" and AVISAR_POTENCIAL)
                    sent.add(key)
                    state["sent"].append(key)
                    hh, mid_tg = None, None
                    if avisar:
                        hh = h2h_for(m, r["pick"])
                        txt = casi_text(r, g, early) if r.get("casi") else confirm_text(r, g, hh, early)
                        mid_tg = send(txt + "\n✍️ Responde con tu cuota (ej. 1.45) o 'no'")
                    register_pick(r, hh, early, cal, avisar, mid_tg)
            elif len(r["conds"]) == 1 and m.live is None:
                cond_alert(r, m, r["conds"][0][0], g)
        # Picks que SOLO pueden salir por los puntos del partido anterior: el motor
        # no los ve hasta que ese partido acaba (los puntos aun no existen). Se
        # prueban aqui, marcador a marcador, para avisar con antelacion igualmente.
        pend = g.pending()
        if rapido:
            continue                          # lo demas, en la vuelta normal
        if 2 <= len(pend) <= 3 and pend[1].live is None:
            pm, m = pend[0], pend[1]
            have = {r["pick"] for r in results if r["match"] is m}
            for pick, vs in ((m.a, m.b), (m.b, m.a)):
                if pick in have:
                    continue
                rx = dict(match=m, pick=pick, vs=vs, reason=f"{vs} ya eliminado",
                          warn=False, conds=[(pm, "")])
                cond_alert(rx, m, pm, g)
        # AVISO TEMPRANO: aun faltan 2 partidos antes del del pick. Se avisa ya
        # si en la mayoria de casos va a haber pick, para que lo tengas con tiempo.
        if len(pend) == 3 and pend[1].live is None and pend[2].live is None:
            p0, p1, m = pend
            if p0.live is not None:
                HOT["on"] = True              # en cuanto acabe, se recalcula al momento
                HOT["ids"].add(p0.mid)
                HOT["last"] = time.time()
            for pick, vs in ((m.a, m.b), (m.b, m.a)):
                key = f"E|{m.mid}|{pick}"
                if key in sent:
                    continue
                tab = early_table(g, p0, p1, m, pick, vs)
                if tab:
                    once(key, lambda tab=tab, m=m, pick=pick, vs=vs, p0=p0, p1=p1:
                         early_text(tab, m, pick, vs, p0, p1))

    if rapido:
        return
    # si se avisó "ATENTO" y ese pick ya no es posible, decirlo claro
    pstat = {p["id"]: p for p in parsed}
    for key, al in list(alerted.items()):
        if f"C|{key}" in sent or any(x.startswith(f"C|{key}") for x in sent):
            alerted.pop(key)                 # ya se confirmó el pick
            continue
        if key in cands:
            continue                         # todavía puede salir
        pmx = pstat.get(al["pm"])
        mid_pick = key.split("|")[0]
        # ¿sigue siendo posible segun los PUNTOS del partido decisivo en juego?
        # (el motor no puede saberlo hasta que acabe: aqui se prueban marcadores)
        if al.get("vs"):
            gx = next((g for g in groups if any(x.mid == mid_pick for x in g.matches)
                       and any(x.mid == al["pm"] for x in g.matches)), None)
            if gx:
                mx = next(x for x in gx.matches if x.mid == mid_pick)
                px = next(x for x in gx.matches if x.mid == al["pm"])
                if not px.final and not mx.final:
                    rx = dict(match=mx, pick=al["pick"], vs=al["vs"],
                              reason=al.get("reason", f"{al['vs']} ya eliminado"),
                              warn=False, conds=[(px, "")])
                    outs = [outcome(rx, gx, px, f)[0] for f in px.possible_finals()]
                    if "✅" in outs or "❓" in outs or "🟡" in outs:
                        al["ts"] = time.time()
                        if px.live and max(px.live) == 2:
                            once(f"F|{mid_pick}|{al['pick']}|{px.mid}|{px.live[0]}-{px.live[1]}",
                                 lambda rx=rx, px=px, gx=gx: closing_set_text(rx, px, gx))
                        continue
        if mid_pick not in checked and time.time() - al.get("ts", 0) < 1200:
            # el grupo no se ha podido leer bien en esta vuelta (dato a medias de
            # la API): no se da por perdido, se espera a la siguiente lectura
            print("Grupo del aviso sin datos fiables ahora, se espera:", key)
            continue
        if pmx and pstat.get(mid_pick, {}).get("status") == "notstarted":
            sc = f"{pmx['score']}" if pmx.get("score") else ""
            once(f"X|{key}", lambda al=al, sc=sc:
                 f"❌ OLVIDA EL AVISO: {al['a']} {sc} {al['b']}\n"
                 f"Ya no hay pick seguro para {al['pick'].upper()} ({al['hora']}). NO apuestes.")
        alerted.pop(key)

    # anular picks ya enviados si, antes de empezar, los datos han cambiado
    status = {p["id"]: p["status"] for p in parsed}
    changed = False
    for pk in picks:
        if pk.get("resultado") or status.get(pk["match_id"]) != "notstarted":
            continue
        if pk.get("tipo") == "OFICIAL B":
            continue                    # seguimiento aparte: no se anula
        dud = pk["match_id"] not in checked and any(
            pk["match_id"] in {m.mid for m in g.matches} and getattr(g, "dudoso", False)
            for g in groups)
        if (pk["match_id"] in checked and (pk["match_id"], pk["pick"]) not in valid) or dud:
            pk["resultado"] = "A"
            changed = True
            if pk.get("enviado") == "no":
                continue
            send(f"❌ PICK ANULADO: {pk['pick'].upper()} vs {pk['rival']} · {pk['hora']}\n"
                 f"Los datos del grupo han cambiado. NO apostar.")
    if changed:
        save_picks()


def main():
    load_state()
    load_picks()
    if state.get("started") != VERSION:
        startup_report()
        state["started"] = VERSION
        save_state()
    end = time.time() + RUN
    while True:
        try:
            tick()
            state["last_error"] = ""
            state["fails"] = 0
        except Exception as ex:
            err = f"{type(ex).__name__}: {ex}"
            traceback.print_exc()
            state["fails"] = state.get("fails", 0) + 1
            # 429 (demasiadas peticiones) o fallos sueltos de red: reintentar en
            # silencio; solo avisar si el problema dura ~10 intentos seguidos.
            # cortes de red / respuestas a medias: reintentar en silencio
            transient = any(x in err for x in (
                "429", "timed out", "URLError", "502", "503", "504", "500",
                "IncompleteRead", "RemoteDisconnected", "ConnectionReset",
                "ConnectionAborted", "JSONDecodeError", "BadStatusLine",
                "ProtocolError", "SSLError", "EOF occurred", "HTTP Error 5"))
            # (HTTP Error 5xx = servidor de la API caido un rato, p. ej. 521)
            if transient:
                if state["fails"] == 10:
                    send(f"⚠️ El avisador lleva ~10 min sin poder leer Zyla: {err[:200]}")
                # con un partido decisivo en juego no se puede esperar 1 minuto:
                # se reintenta enseguida para no llegar tarde al pick
                time.sleep(10 if time.time() - HOT.get("last", 0) < 600 else 60)
            elif err != state["last_error"]:
                send(f"⚠️ Error avisador: {err[:300]}")
                state["last_error"] = err
        save_state()
        wait = POLL_FAST if HOT["on"] else POLL
        if "--loop" not in sys.argv and time.time() + wait > end:
            break
        if HOT["ids"]:
            mid = watch_hot(wait)
            if mid:
                # acaba de terminar el partido decisivo (o un set que lo puede cerrar):
                # AVISO RAPIDO solo de ese grupo y, justo despues, la vuelta normal
                try:
                    tick(rapido=mid)
                except Exception as ex:
                    print("WARN aviso rapido", ex)
                save_state()
                continue
            continue
        time.sleep(wait)


def watch_hot(seconds):
    """Vigila los partidos decisivos uno a uno: cada POLL_EVENT segundos y, al final
    de un set importante (el que deja a alguien a 2 sets o cierra el partido), cada
    POLL_EVENT_RAPIDO. Devuelve el id del partido en cuanto termina DE VERDAD (BetsAPI
    lo da por terminado, los sets cuadran uno a uno y se ve igual en 2 lecturas
    seguidas), o en cuanto acaba un set y queda 2-x (para el 🔔 al instante).
    Si no pasa nada en 'seconds', devuelve None."""
    stop = time.time() + seconds
    while time.time() < stop:
        cierre = False
        confirmar = False
        esperar_lista = False
        for mid in list(HOT["ids"]):
            ev = tt_feed.event(mid)
            if ev is None:
                continue            # no se ha podido leer ahora: se reintenta enseguida
            FRESH[str(mid)] = ev
            p = tt_feed.parse(ev)
            if p and p["final"]:
                FIN_VISTO[mid] = FIN_VISTO.get(mid, 0) + 1
                if FIN_VISTO[mid] == 1:
                    FIN_T[mid] = time.time()
                if FIN_VISTO[mid] >= 2 and str(mid).startswith("b") and \
                        tt_betsapi.sigue_en_juego(mid, tt_feed.TOURNAMENT_ID):
                    print("Terminado en un sitio pero aun EN JUEGO en otro, se espera:", mid)
                    esperar_lista = True
                    continue
                if FIN_VISTO[mid] >= 2:
                    if str(mid).startswith("b"):
                        tt_betsapi.aceptar(ev)   # 3 comprobaciones superadas
                    print("Partido decisivo terminado:", mid, p["final"],
                          f"(confirmado en {time.time() - FIN_T.get(mid, time.time()):.0f} s)",
                          json.dumps(tt_betsapi.CRUDO.get(str(mid)), ensure_ascii=False)[:600])
                    FIN_VISTO[mid] = 0
                    return mid
                confirmar = True    # 2.a lectura enseguida, antes de dar nada por hecho
                continue
            FIN_VISTO[mid] = 0
            if not p or not p["live"]:
                continue
            sets = p["live"]
            antes = HOT_SETS.get(mid)
            HOT_SETS[mid] = sets
            if max(sets) >= 2 and antes is not None and antes != sets:
                print("Set decisivo:", mid, sets)
                return mid
            # el set en juego esta avanzado (8+ puntos) y al acabar alguien queda
            # a 2 sets o termina el partido: mirar cada pocos segundos
            cur = sum(sets) + 1
            pts = (_n((ev.get("homeScore") or {}).get(f"period{cur}")),
                   _n((ev.get("awayScore") or {}).get(f"period{cur}")))
            if max(sets) >= 1 and max(pts) >= 8:
                cierre = True
        if confirmar:
            time.sleep(1.5)
            continue
        if esperar_lista:
            time.sleep(3)
            continue
        rapido_ok = cierre and tt_betsapi.llamadas_hora() < BETSAPI_MAX_HORA
        time.sleep(POLL_EVENT_RAPIDO if rapido_ok else POLL_EVENT)
    return None


def _n(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    if "--reset" in sys.argv:
        state["started"] = False
    if "--resumen" in sys.argv:          # resumen a mano: python tt_alerts.py --resumen
        load_picks()
        send(summary_text(datetime.now(tt_feed.TZ).strftime("%Y-%m-%d")))
        sys.exit()
    main()
