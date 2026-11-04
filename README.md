# tt-multiliga

Bot de apuestas de tenis de mesa para TT Cup, Liga Pro Checa y TT Elite Series.
Independiente del TT Avisador.

## Fase 1: validación

`mb_backtest.py` mide, con los datos de 2 horas antes de cada partido:

- 🔥 **Regla en caliente:** el pick ya ha jugado hoy y el rival todavía no.
- 🔥🧠 **Regla + modelo:** la regla y, además, el modelo ve valor (≥5% / ≥10%).
- 🧠 **Modelo general:** solo como referencia.

El modelo usa Elo dinámico, forma ajustada, lo que lleva hoy cada jugador y el H2H
de 90 días. Se re-entrena cada 15 días solo con partidos anteriores.

Periodos: arranque (solo aprende), **validación (nunca vista)** y ya vistos
(los últimos 30 días, donde se encontró la regla en TT Cup).

Veredicto por liga y estrategia (sobre la validación):

- 🟢 **CONFIRMADA**: ≥300 picks, yield > 0, rango probable (90%) todo positivo y
  al menos 2 de cada 3 meses en positivo.
- 🟡 **PROMETEDORA**: gana, pero no cumple todo lo anterior.
- 🔴 **DESCARTADA**: no gana.

Se lanza en **Actions → Backtest multiliga → Run workflow**. Las ligas van una
detrás de otra. El informe y `picks_regla.csv` llegan a Telegram y quedan en
`resultados/<liga>/`.

## Secretos (Settings → Secrets and variables → Actions)

| Nombre | Valor |
|---|---|
| `ZYLA_API_KEY` | clave de Zyla (obligatorio) |
| `TELEGRAM_TOKEN` | token del bot de Telegram (opcional) |
| `MY_CHAT_ID` | tu id de Telegram (opcional) |
