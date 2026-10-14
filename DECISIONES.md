# Arquitectura y decisiones

## Responsabilidades

| Componente | Hace | No hace |
|---|---|---|
| `main.py` (API) | Identidad (`X-User-Id`), endpoints, errores HTTP | Lógica de agentes |
| `ChatService` | Turnos, memoria por conversación, lock por conversación, notas de resultados | Hablar con la BD |
| Agente principal | Conversa, responde con RAG, delega la agenda, cancela tareas | Tocar la agenda |
| `TaskManager` | Lanza el subagente con `asyncio.create_task`, timeouts, cancelación, eventos | Decidir nada de negocio |
| Subagente de agenda | Interpreta la petición, usa las herramientas de agenda, pide aclaraciones | Saltarse reglas: no puede |
| `agenda.py` | **Todas** las reglas (horario, pasado, solapes, propiedad), SQLite | Conocer el LLM |

## Contratos de herramientas
Entradas con esquemas Pydantic (`schemas.py`): si el modelo envía un dato inválido, LangChain devuelve
el error de validación al modelo para que lo corrija. Salidas homogéneas:
`{"ok": true, ...}` o `{"ok": false, "error": "CÓDIGO", "message": "..."}`.

## Flujo de confirmación
1. `preparar_*` valida en código y crea una **operación pendiente** `op_...` (no escribe).
2. El usuario ve el resumen y confirma en un mensaje **posterior**.
3. `ejecutar_operacion(op_...)` solo funciona si `turno_actual > turno_de_preparación`: aunque el LLM
   intente ejecutar sin confirmación, el código lo rechaza (`CONFIRMATION_REQUIRED`).
4. El `op_...` es la clave de idempotencia en SQLite: confirmar dos veces no crea dos citas.

## Ciclo de vida de las tareas
`running` → `completed` | `failed` | `timeout` | `cancelled`. Cada transición genera un evento en
la conversación (polling en `/conversations/{id}/events`). Una tarea nueva de la misma conversación
cancela la anterior (su resultado ya no es vigente). Un resultado que llegue tras la cancelación se
descarta.

## Por qué así
- **LangChain `create_agent` + LangGraph**: bucle de herramientas, memoria (`InMemorySaver`),
  contexto inyectado (`ToolRuntime`) y límites de pasos (middlewares) sin reinventarlos.
- **Segundo plano con `asyncio.create_task`** y no con Celery/colas: un solo proceso, sin
  infraestructura extra; suficiente para el prototipo.
- **Polling** y no WebSocket: lo más fácil de demostrar con un script y de depurar con `curl`.
- **Subagente sin estado**: cada tarea es "instrucción → resultado". Evita que una tarea cancelada
  deje la memoria del subagente en un estado inconsistente.
- **Confirmación en código** y no `HumanInTheLoopMiddleware`: la interrupción ocurriría dentro de una
  tarea en segundo plano y reanudarla complicaría el flujo.
- **BM25** y no embeddings: 5 documentos pequeños; sin modelo que descargar ni otra API.
- **Sin reintentos en herramientas de escritura**; sí en las llamadas al LLM (no tienen efectos).

## Por qué Mistral
- **Gratis y con cuota generosa en modelos pequeños**: en el plan gratuito, `ministral-14b-latest`
  admite 30 peticiones/min y ~900 000 tokens/min, y `ministral-8b-latest` 188 peticiones/min
  (cifras leídas en las cabeceras `x-ratelimit-*`). Para un PoC como este sobra: cada ejecución de
  los casos de evaluación hace unas 25 llamadas de ~1 000 tokens.
- **Punto de partida: Groq** (`openai/gpt-oss-120b`, plan gratuito). Es muy potente, pero limita a 8 000
  tokens/min y las evaluaciones fallaban con errores 429 (con Groq solo pasaban 2 de 6 casos). Cambiar de
  proveedor fue barato porque el modelo está aislado en `app/llm.py`.
- **Respaldo**: si el modelo principal falla (p. ej. 429), se reintenta con `ministral-8b-latest`
  (`MISTRAL_FALLBACK_MODEL`; vacío lo desactiva). Cada modelo tiene su propia cuota.
- Un modelo pequeño es menos fiable que uno grande: por eso las evaluaciones comprueban el estado real de
  la agenda y no solo el texto.
- **En producción apuntaríamos a modelos más grandes** (p. ej. `mistral-medium` o equivalente) para fiabilizar
  la selección de herramientas. Ya lo vimos: `ministral-14b` llegó a cancelar una consulta en curso cuando
  el usuario solo hacía otra pregunta; se corrigió con el prompt, pero un modelo mayor lo evitaría de raíz.
  Cambiarlo es solo `MISTRAL_MODEL`; las evaluaciones sirven para validar el cambio.

## ¿Cuándo bastaría un solo agente?
Si no existiera la consulta lenta y las herramientas fueran rápidas, un único agente con todas las
herramientas sería más simple, más barato (menos llamadas al LLM) y con menos latencia. El subagente
se justifica aquí por: trabajo largo en segundo plano, instrucciones y herramientas especializadas, y
separación de permisos (solo el subagente puede escribir en la agenda).

## Resultados observados y fallos conocidos
Evaluaciones con LLM real (`python -m evals.run_evals`), 13/10/2026:

| Modelo | Resultado | Motivo de los fallos |
|---|---|---|
| Groq `gpt-oss-120b` | 1/6, luego 2/6 | Límite de 8 000 tokens/min (429). Antes, error 400 `tool_use_failed`: el esquema de las herramientas exigía `date-time` y el modelo enviaba `YYYY-MM-DDTHH:MM`; corregido en `schemas.py` |
| Mistral `ministral-14b` | 4/6 | 429 por superar 30 peticiones/min (dos casos); `dialogo_15s` fallaba porque el modelo cancelaba la consulta en curso |
| Mistral `ministral-14b` + respaldo `ministral-8b`, prompt corregido | **6/6** (un solo run) | — |

Fallos conocidos:
- Con modelos pequeños el resultado varía entre ejecuciones; 6/6 es una observación, no una garantía.
- `langchain-mistralai` no reintenta errores HTTP como el 429; por eso existe el modelo de respaldo.
- `peticion_ambigua` exigía un `?` en la respuesta; se cambió el criterio porque la respuesta era correcta sin él.
- Las pruebas con modelos falsos (`pytest`) no detectan estos problemas de selección de herramientas; solo las evaluaciones con LLM real.

## Limitaciones conocidas
- Estado en memoria: un reinicio pierde conversaciones, tareas, eventos y operaciones pendientes.
- Un solo proceso (`--workers 1`): con varios workers cada uno tendría su propio estado.
- La confirmación se apoya en el turno, no en entender el "sí": si el usuario escribe otra cosa y el
  modelo decide ejecutar, el código no lo distingue. En producción: botón/endpoint de confirmación.
- La calidad de las respuestas depende del modelo; los casos de evaluación lo miden.

## Antes de producción
1. Estado en Redis/Postgres (tareas, eventos, operaciones pendientes, checkpointer de LangGraph).
2. Cola de trabajos (Arq, Celery, Temporal) con reanudación tras reinicios.
3. Autenticación real (JWT/OAuth) que sustituya `X-User-Id`.
4. SSE/WebSocket para notificar resultados sin polling.
5. Confirmación por endpoint explícito además del lenguaje natural.
6. Modelo de mayor tamaño de pago (mejor selección de herramientas y menos límites de cuota) y pasar de nuevo
   las evaluaciones.

## Observabilidad
- **Errores**: logs JSON con `conversation_id`, `task_id`, `tool`, `duration_ms`, `outcome` →
  agregarlos (Loki/ELK) y alertar por tasa de `task_failed` / `timeout`.
- **Latencia**: p50/p95 de `agent_turn` y de cada herramienta; trazas con LangSmith u OpenTelemetry.
- **Coste**: tokens por turno (metadatos de uso de Mistral) × precio del modelo; límites de pasos ya
  acotan el peor caso.

## Reparto en un equipo pequeño
- Backend: persistencia, cola, autenticación.
- IA: prompts, herramientas, conjunto de evaluación y regresiones.
- Plataforma: Docker/CI, observabilidad, despliegue.

## Tiempo y uso de IA
Tiempo total: unas 6 horas.

Usé Claude Code como asistente de programación, sobre todo para detectar y corregir fallos: diagnosticó
los errores 429 y `tool_use_failed` a partir de los logs, corrigió el esquema de fecha de las herramientas,
ayudó a migrar de proveedor y ajustó el prompt que provocaba cancelaciones indebidas. Cada cambio se validó
ejecutando las pruebas (`pytest`) y las evaluaciones con LLM real.