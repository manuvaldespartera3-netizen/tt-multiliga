# tt-multiliga

Bot de apuestas de tenis de mesa para TT Cup, Liga Pro Checa y TT Elite Series.
Independiente del TT Avisador.

## Fase 1: backtest

`mb_backtest.py` calcula, para cada partido y con los datos de 2 horas antes:

- Elo dinámico (partido a partido, también los del mismo día)
- Forma ajustada (últimos 10 partidos frente a lo esperado)
- Lo que lleva hoy cada jugador
- H2H de los últimos 90 días

El modelo se ajusta con la primera mitad del periodo y se mide solo en la segunda.
Pick = prob × cuota − 1 ≥ umbral. El informe da win rate, beneficio y **yield**
por umbral, rango de cuota y mitades.

Se lanza en **Actions → Backtest multiliga → Run workflow**. El informe llega a
Telegram (si están los secretos) y queda en `resultados/<liga>/`.

## Secretos (Settings → Secrets and variables → Actions)

| Nombre | Valor |
|---|---|
| `ZYLA_API_KEY` | clave de Zyla (obligatorio) |
| `TELEGRAM_TOKEN` | token del bot de Telegram (opcional) |
| `MY_CHAT_ID` | tu id de Telegram (opcional) |
