"""
mb_marcas.py - Marca de color en cada aviso de Telegram, para saber de un vistazo
que picks tienen ya tu cuota de BetCris y cuales no.

La primera y la ultima linea del aviso cambian solas:
  ⚪⚪ PENDIENTE · falta tu cuota ⚪⚪      (aun no has hecho nada)
  🟢🟢 APOSTABLE · BetCris 1.91 🟢🟢        (tu cuota llega al minimo)
  🟡🟡 DESCONFIAR · BetCris 2.60 🟡🟡       (valor >30%: sospechoso)
  🔴🔴 NO LLEGA · BetCris 1.70 🔴🔴         (tu cuota no llega al minimo)
  ⚫⚫ NO ESTÁ EN BETCRIS ⚫⚫               (respondiste "no")
Respondiendo al aviso: con la cuota -> se apunta · "no" -> no esta en BetCris ·
"si" -> vuelve a pendiente (por si aparece a ultima hora; tambien vale poner la
cuota directamente). Los avisos antiguos con boton siguen funcionando.

Lo usan mb_seguimiento.py (TT Cup y Elite) y mb_rating_vivo.py (Rating Liga Pro).
TEXTOS se guarda en el estado de cada bot: es el texto del aviso sin la primera
linea, para poder reescribirlo.
"""
import json
import urllib.error
import urllib.request

TEXTOS = {}                  # "canal:msg" -> cuerpo del aviso (sin la linea de color)
MAX_TEXTOS = 300


def cabecera(p):
    """Linea de color, con dos bolitas delante y dos detras."""
    t = _cabecera(p)
    b = t[0] * 2
    return f"{b} {t[2:]} {b}"


def _cabecera(p):
    if p.get("no_esta") == "si":
        return "⚫ NO ESTÁ EN BETCRIS"
    try:
        cb = float(p.get("cuota_betcris") or 0)
    except ValueError:
        cb = 0
    if cb:
        d = p.get("decision_betcris")
        if d == "FUERTE":
            return f"🟢 APOSTABLE ⭐⭐ · BetCris {cb:.2f}"
        if d == "APUESTA":
            return f"🟢 APOSTABLE · BetCris {cb:.2f}"
        if d == "SOSPECHOSO":
            return f"🟡 DESCONFIAR · BetCris {cb:.2f} (valor >30%)"
        return f"🔴 NO LLEGA · BetCris {cb:.2f}"
    return "⚪ PENDIENTE · falta tu cuota"


def teclado(p):
    """Sin botones: todo se hace respondiendo al aviso (lo mas fiable). Al
    reescribir un aviso antiguo, esto le quita el boton que tenia."""
    return {"inline_keyboard": []}


def texto(p, cuerpo):
    c = cabecera(p)
    return f"{c}\n{cuerpo}\n{c}"


def orden(texto_usuario):
    """'no' / 'no está' / 'x'  -> 'no' · 'si' / 'sí está' / 'deshacer' -> 'si' ·
    otra cosa -> None. Devuelve (orden, palabras que sobran = apellido)."""
    import unicodedata
    t = unicodedata.normalize("NFKD", texto_usuario or "").encode("ascii", "ignore").decode().lower()
    w = [x for x in "".join(c if c.isalnum() else " " for c in t).split()]
    if not w:
        return None, []
    if w[0] in ("no", "x", "noesta", "nohay"):
        resto = [x for x in w[1:] if x not in ("esta", "hay", "en", "betcris", "el", "partido")]
        return "no", resto
    if w[0] in ("si", "deshacer", "siesta"):
        resto = [x for x in w[1:] if x not in ("esta", "hay", "en", "betcris", "el", "partido")]
        return "si", resto
    return None, []


def color(p):
    """Las dos bolitas del estado (para las confirmaciones)."""
    return cabecera(p)[:2]


def es_blanco(p):
    return not p.get("cuota_betcris") and p.get("no_esta") != "si"


def _api(token, metodo, datos):
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{metodo}",
                                 data=json.dumps(datos).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode()).get("result")


def _clave(canal, msg):
    return f"{canal}:{msg}"


def enviar(token, chat, canal, cuerpo, p, reply_to=None):
    """Manda el aviso con su linea de color y su boton. Devuelve el id del mensaje."""
    if not (token and chat):
        print(cabecera(p) + "\n" + cuerpo)
        return None
    datos = {"chat_id": chat, "text": texto(p, cuerpo)}
    if reply_to:
        datos["reply_to_message_id"] = reply_to
    for intento in range(3):
        try:
            res = _api(token, "sendMessage", datos)
            msg = res.get("message_id") if res else None
            if msg:
                TEXTOS[_clave(canal, msg)] = cuerpo
                while len(TEXTOS) > MAX_TEXTOS:
                    TEXTOS.pop(next(iter(TEXTOS)))
            return msg
        except urllib.error.HTTPError as ex:
            if ex.code == 400 and "reply_to_message_id" in datos:
                datos.pop("reply_to_message_id")
                continue
            print("WARN telegram", ex)
        except Exception as ex:
            print("WARN telegram", ex)
    return None


def actualizar(token, chat, canal, p):
    """Reescribe la linea de color (y el boton) del aviso de ese pick."""
    msg = str(p.get("msg") or "")
    cuerpo = TEXTOS.get(_clave(canal, msg))
    if not (token and chat and msg and cuerpo):
        return False                 # avisos de antes de este cambio: no se tocan
    try:
        _api(token, "editMessageText", {"chat_id": chat, "message_id": int(msg),
                                        "text": texto(p, cuerpo),
                                        "reply_markup": teclado(p)})
        return True
    except urllib.error.HTTPError as ex:
        if ex.code == 400:
            return True              # "message is not modified": ya estaba bien
        print("WARN editar", ex)
    except Exception as ex:
        print("WARN editar", ex)
    return False


def contestar_boton(token, cq_id, texto):
    try:
        _api(token, "answerCallbackQuery", {"callback_query_id": cq_id, "text": texto})
    except Exception as ex:
        print("WARN boton", ex)


def boton(u, picks, chat):
    """Si la actualizacion es un boton pulsado: (pick, accion) o None."""
    cq = u.get("callback_query")
    if not cq:
        return None
    if str(((cq.get("message") or {}).get("chat") or {}).get("id")) != str(chat):
        return None
    accion, _, pid = str(cq.get("data", "")).partition(":")
    msg = str((cq.get("message") or {}).get("message_id", ""))
    pk = next((p for p in picks if str(p.get("msg")) == msg and str(p["id"]) == pid), None) \
        or next((p for p in picks if str(p["id"]) == pid), None)
    return cq, pk, accion


def aplicar_boton(token, chat, canal, cq, pk, accion):
    if not pk:
        contestar_boton(token, cq["id"], "No encuentro ese pick")
        return
    if accion == "noesta":
        pk["no_esta"] = "si"
        contestar_boton(token, cq["id"], "⚫ Marcado: no está en BetCris")
    elif accion == "siesta":
        pk["no_esta"] = ""
        contestar_boton(token, cq["id"], "⚪ Vuelve a pendiente")
    actualizar(token, chat, canal, pk)


def texto_pendientes(picks, hoy, ayer):
    """Lista de los ⚪ de hoy (y de ayer) para el comando 'pendientes'."""
    blancos = [p for p in picks if es_blanco(p) and p.get("fecha") in (hoy, ayer) and p.get("msg")]
    if not blancos:
        return "✅ No tienes ningún pick ⚪ pendiente de cuota (hoy y ayer)."
    blancos.sort(key=lambda p: (p["fecha"], p["hora"]))
    L = [f"⚪ {len(blancos)} picks sin tu cuota:"]
    for p in blancos:
        dia = "" if p["fecha"] == hoy else " (ayer)"
        L.append(f"  {p['hora']}{dia} · {p['pick'].upper()} vs {p['rival']}")
    L.append("Responde a cada aviso con la cuota (o «no» si no está en BetCris).\n"
             "También vale escribir aquí: apellido cuota (ej. kadavy 1,85) o «no kadavy».")
    return "\n".join(L)
