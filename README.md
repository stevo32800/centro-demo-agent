# Centro Demo — agente conversacional con subagente de agenda

Prototipo de la prueba técnica *AI Tech Lead*: un agente principal (FastAPI + LangChain + Mistral) que
responde sobre el negocio con RAG (BM25) y delega la agenda a un subagente que trabaja **en segundo
plano**, de modo que la conversación sigue disponible durante la consulta lenta (15 s).

## Arranque rápido

```bash
cp .env.example .env          # y pon tu MISTRAL_API_KEY
# Opción A: Docker
docker compose up --build
# Opción B: local (Python 3.10+)
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

### Clave de API de Mistral (gratuita)
1. Crea una cuenta y genera una clave en <https://admin.mistral.ai/organization/api-keys>.
2. Pégala en `.env` como `MISTRAL_API_KEY=...`. El modelo por defecto es `ministral-14b-latest`
   (con `ministral-8b-latest` de respaldo, ver `DECISIONES.md`).

Comprobación: `curl localhost:8000/health`

## Identidad (simulada)
Cada petición lleva la cabecera `X-User-Id` (`ana` o `luis`). La fija el cliente HTTP y el servidor
la inyecta en el contexto de las herramientas: el modelo no la ve ni puede cambiarla.

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Estado, modelo y nº de tareas en curso |
| POST | `/chat` | `{conversation_id?, message}` → `{conversation_id, reply, started_tasks}` |
| GET | `/conversations/{id}/events?after=N` | Eventos de tareas con `seq > N` (polling) |
| GET | `/tasks/{id}` | Estado, resultado o error de una tarea |
| DELETE | `/tasks/{id}` | Cancela una tarea en curso |
| GET | `/appointments` | Citas activas del usuario (estado real, sin LLM) |

```bash
curl -s localhost:8000/chat -H "X-User-Id: ana" -H "Content-Type: application/json" \
     -d '{"message": "¿Qué horarios hay libres el próximo jueves?"}'
```

## Datos de demostración
Al arrancar se crean `ana` (Ana Pérez) y `luis` (Luis Gómez) y tres citas en el próximo lunes/martes
(`app/seed.py`). La agenda está en `data/agenda.db`; bórrala para volver al estado inicial.

## Demostración de la consulta en segundo plano
```bash
python scripts/demo.py            # con el servidor arrancado
```

## Pruebas
```bash
pytest -q                          # reglas de agenda, tareas y API con modelos FALSOS (sin red)
python -m evals.run_evals          # 6 casos con el LLM REAL de Mistral (necesita MISTRAL_API_KEY)
```

### Qué usa modelos falsos y qué usa un modelo real
| Caso exigido | `pytest` (modelos falsos, sin red) | `evals` (LLM real) |
|---|---|---|
| Reserva confirmada | `test_booking_with_confirmation_and_repeated_confirmation` | `reserva_confirmada` |
| Petición ambigua | — | `peticion_ambigua` |
| Herramienta con timeout | `test_tool_timeout_is_reported` | `timeout` |
| Reserva repetida | `test_booking_with_confirmation_and_repeated_confirmation`, `test_idempotent_create` | `reserva_repetida` |
| Diálogo durante la consulta de 15 s | `test_conversation_stays_available_during_slow_lookup` | `dialogo_15s` |
| Dos usuarios concurrentes | `test_identity_and_isolation`, `test_two_conversations_progress_in_parallel` | `dos_usuarios` |

Resultados observados y fallos conocidos: ver `DECISIONES.md`.

## Reinicios
Las citas persisten en SQLite. Las conversaciones, tareas, eventos y operaciones pendientes viven en
memoria: tras un reinicio se pierden (las tareas en curso no se reanudan y hay que volver a confirmar).
Ver `DECISIONES.md`.