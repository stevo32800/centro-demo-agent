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
Usé LangChain `create_agent` con LangGraph porque ya traen el bucle de herramientas, la memoria
(`InMemorySaver`), el contexto inyectado (`ToolRuntime`) y los límites de pasos (middlewares); no quería
reescribir eso.

La consulta lenta corre con `asyncio.create_task` y no con Celery o una cola: en este prototipo basta un
solo proceso y me ahorro infraestructura. Para entregar el resultado elegí polling en vez de WebSocket,
porque es lo más fácil de demostrar con un script y de depurar con `curl`.

El subagente no guarda estado: cada tarea es "instrucción → resultado". Así una tarea cancelada no deja su
memoria a medias. La confirmación la controlo en código y no con `HumanInTheLoopMiddleware`, porque la
interrupción ocurriría dentro de una tarea en segundo plano y reanudarla complicaría el flujo.

Para el negocio uso BM25 en lugar de embeddings: son 5 documentos pequeños y así no hace falta otro
modelo ni otra API. Las herramientas de escritura no se reintentan; las llamadas al LLM sí, porque no tienen
efectos secundarios.

## Por qué Mistral
Empecé con Groq (`openai/gpt-oss-120b`, plan gratuito). Es un modelo potente, pero el plan gratuito limita a
8 000 tokens por minuto y las evaluaciones fallaban con errores 429 (solo pasaban 2 de 6 casos). Cambiar de
proveedor fue barato porque el modelo está aislado en `app/llm.py`.

Me quedé con Mistral porque es gratis y da una cuota generosa en modelos pequeños. En mi cuenta, según las
cabeceras `x-ratelimit-*`, `ministral-14b-latest` admite 30 peticiones por minuto y unos 900 000 tokens por
minuto, y `ministral-8b-latest` 188 peticiones por minuto. Para un PoC como este sobra: cada ejecución de
las evaluaciones hace unas 25 llamadas de unos 1 000 tokens.

Si el modelo principal falla (por ejemplo con un 429), la llamada se repite con `ministral-8b-latest`
(`MISTRAL_FALLBACK_MODEL`; vacío lo desactiva). Cada modelo tiene su propia cuota.

Un modelo pequeño es menos fiable que uno grande, y por eso las evaluaciones comprueban el estado real de la
agenda y no solo el texto. En producción apuntaría a modelos más grandes (por ejemplo `mistral-medium` o
equivalente) para que la selección de herramientas sea más fiable. Ya lo vi: `ministral-14b` llegó a
cancelar una consulta en curso cuando el usuario solo hacía otra pregunta. Lo corregí con el prompt, pero un
modelo mayor lo evitaría de raíz. Cambiarlo es solo `MISTRAL_MODEL`, y las evaluaciones sirven para validar
el cambio.

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
- Con modelos pequeños el resultado varía entre ejecuciones: el 6/6 es una observación, no una garantía.
- `langchain-mistralai` no reintenta errores HTTP como el 429, y por eso añadí el modelo de respaldo.
- `peticion_ambigua` exigía un `?` en la respuesta. Cambié el criterio porque la respuesta era correcta sin él.
- Las pruebas con modelos falsos (`pytest`) no detectan problemas de selección de herramientas; solo lo hacen las evaluaciones con LLM real.

## Limitaciones conocidas
- Estado en memoria: un reinicio pierde conversaciones, tareas, eventos y operaciones pendientes.
- Un solo proceso (`--workers 1`): con varios workers cada uno tendría su propio estado.
- La confirmación se apoya en el turno, no en entender el "sí": si el usuario escribe otra cosa y el
  modelo decide ejecutar, el código no lo distingue. En producción: botón/endpoint de confirmación.
- La calidad de las respuestas depende del modelo; los casos de evaluación lo miden.

## Antes de producción
Lo primero que cambiaría, por orden de prioridad:
1. Guardar el estado fuera de la memoria del proceso (base de datos), para no perder conversaciones ni
   tareas al reiniciar.
2. Usar una cola para las tareas largas, de modo que se puedan retomar si el servidor se cae.
3. Poner una autenticación real en lugar del `X-User-Id` simulado.
4. Añadir un endpoint de confirmación explícito (human-in-the-loop real: una interfaz lo llamaría con un
   botón), en vez de depender de que el modelo entienda el "sí".
5. Usar un modelo más grande y de pago, y volver a pasar las evaluaciones.
6. Avisar al usuario cuando el resultado esté listo, sin que el cliente tenga que consultar cada pocos
   segundos.
7. Responder sin llamar al modelo a los casos evidentes (un "sí" o "no" corto, o una pregunta frecuente)
   para ahorrar coste y latencia, y dejar el LLM para el resto.

## Observabilidad
- **Errores**: los logs ya son JSON e incluyen la conversación, la tarea, la herramienta, la duración y el
  resultado. Los enviaría a Loki y los vería en Grafana, con una alerta si suben las tareas fallidas o con
  timeout.
- **Latencia**: mediría cuánto tarda cada turno del agente y cada herramienta, mirando el caso típico y
  también los más lentos, en un panel de Grafana.
- **Coste**: tokens usados por turno multiplicados por el precio del modelo. Los límites de pasos ya
  acotan el peor caso.
- **Seguimiento del agente**: para ver qué hizo el modelo en cada paso (prompts, herramientas elegidas)
  usaría LangSmith, que se integra con LangChain.

## Reparto en un equipo pequeño
Con tres personas lo repartiría así:
- Una persona de backend: base de datos, cola de tareas y autenticación.
- Una persona de IA: prompts, herramientas y evaluaciones, para detectar cuando un cambio de modelo
  empeora algo.
- Una persona de plataforma: Docker, despliegue y monitorización.

Empezaría por el estado en base de datos y las evaluaciones, porque lo demás se apoya en eso.

## Tiempo y uso de IA
Dediqué unas 6 horas en total.

Usé Claude Code como ayuda para programar, para elegir las herramientas y para depurar.
