# Spec — Plataforma de clases de inglés con IA

> Documento para pasarle a Claude Code como brief inicial del proyecto.
> Codename: `tutor-en`

---

## 0. Contexto y objetivo

Usuaria única (por ahora): desarrolladora hispanohablante, ex-B2, oxidada sobre todo en producción oral. Objetivo real: llegar a poder sostener una entrevista técnica y un daily en inglés. Objetivo secundario: que la plataforma sirva como proyecto de portfolio y eventual producto.

**El problema que resuelve y que las apps existentes no resuelven:** ChatGPT/Claude en voice mode son buenos tutores pero no tienen memoria estructurada. Te corrigen el mismo error cuarenta veces, nunca te hacen practicar justo lo que fallás, y no hay progresión. Esta plataforma es un tutor con **expediente del alumno**.

**Todo el código, comentarios, commits, UI y README van en inglés.** La usuaria es hispanohablante y esto es parte del entrenamiento.

---

## 1. Principio de diseño central

> El chat es la interfaz. El expediente es el producto.

Cada interacción produce evidencia. La evidencia se convierte en `ErrorItem` y `VocabItem`. Esos ítems determinan qué contiene la clase de mañana. Si una feature no alimenta ni consume ese ciclo, no va en la v1.

Consecuencia práctica: **nunca generar una clase con un prompt genérico.** Cada generación de clase recibe como contexto el estado actual del expediente.

---

## 2. Stack

- **Backend:** Django 5.x + PostgreSQL
- **Frontend:** Django templates + HTMX + Tailwind. Sin SPA. JS vanilla solo para captura y reproducción de audio.
- **LLM:** OpenAI API
  - Tutor en vivo y generación de contenido: `gpt-4o` (calidad importa en el contenido pedagógico)
  - Analizador post-clase: `gpt-4o` con `response_format=json_schema`
  - STT: `gpt-4o-transcribe`
  - TTS: `gpt-4o-mini-tts`
- **Deploy:** Railway
- **Jobs:** para v1, procesamiento síncrono al cerrar la clase (tarda 10-20s, se muestra un spinner). No meter Celery todavía.

Costo estimado con uso diario de ~30 min: pocos dólares al mes. No optimizar por costo en v1.

---

## 3. Modelo de datos

```
Learner
  - user (OneToOne)
  - cefr_speaking, cefr_listening, cefr_reading, cefr_writing  (choices A1..C2)
  - target_level                   # 'B2'
  - placement_done (bool), placement_notes (text)
  - goal_statement (text)          # "technical interviews, daily standups"
  - native_language = 'es'
  - created_at

Track                              # 'work' | 'general'
  - slug, name, description

Topic
  - track (FK)
  - title                          # "Explaining system architecture"
  - description
  - seed_vocabulary (JSONField)    # lista de términos objetivo
  - is_active

GrammarTopic                       # el syllabus A2→B2, ver §7b
  - slug, title                    # "present-perfect-since-for"
  - cefr_level                     # nivel en el que se introduce
  - order                          # posición en el programa
  - summary_es (text)              # explicación contrastiva ES→EN
  - examples (JSONField)           # [{wrong, right, note_es}]
  - related_subcategories (JSON)   # ['tense','aspect'] → enlaza con ErrorItem

LearnerGrammarTopic
  - learner (FK), topic (FK)
  - status                         # 'not_started' | 'introduced' | 'practicing' | 'mastered'
  - introduced_in (FK Lesson, nullable)
  - times_targeted, times_avoided

Checkpoint                         # examen mensual, ver §7c
  - learner (FK)
  - taken_at
  - results (JSONField)            # por skill: estimate, evidence, gaps_to_target
  - report_es (text)

Lesson
  - learner (FK)
  - track (FK)
  - topic (FK, nullable)
  - grammar_topic (FK, nullable)   # el punto de la mini-lección de hoy
  - skill                          # 'speaking' | 'listening' | 'reading' | 'writing' | 'checkpoint'
  - status                         # 'planned' | 'in_progress' | 'completed' | 'analyzed'
  - plan (JSONField)               # el plan generado, ver §4
  - started_at, completed_at
  - duration_seconds

Turn                               # un intercambio dentro de la clase
  - lesson (FK)
  - role                           # 'tutor' | 'learner'
  - text                           # transcripción o texto escrito
  - audio_file (nullable)
  - audio_duration_ms (nullable)
  - word_count
  - created_at
  - sequence

ErrorItem                          # EL CORAZÓN DEL SISTEMA
  - learner (FK)
  - category                       # ver taxonomía §6
  - subcategory
  - learner_produced (text)        # exactamente lo que dijo/escribió
  - correction (text)
  - explanation (text)             # en español, breve, contrastivo con el español
  - source_turn (FK, nullable)
  - source_lesson (FK)
  - confidence                     # 'high' | 'medium' | 'low'
  - occurrences (int, default 1)
  - srs_box (int, default 0)       # 0..5
  - next_review_at (date)
  - status                         # 'active' | 'mastered' | 'dismissed'
  - created_at, last_seen_at

VocabItem
  - learner (FK)
  - term, definition_en, example_sentence
  - track (FK, nullable)
  - status                         # 'target' | 'emerging' | 'acquired'
  - srs_box, next_review_at
  - times_produced (int)           # cuántas veces la usó espontáneamente

LessonReport
  - lesson (OneToOne)
  - summary_es (text)              # qué pasó en la clase, 3-4 líneas
  - strengths (JSONField)
  - focus_next (JSONField)
  - fluency_wpm (float, nullable)
  - filler_ratio (float, nullable)
  - new_errors_count, recycled_errors_count, errors_avoided_count
  - raw_analysis (JSONField)       # respuesta cruda del analizador
```

**Índices necesarios:** `ErrorItem(learner, status, next_review_at)`, `VocabItem(learner, status, next_review_at)`, `Lesson(learner, -started_at)`.

---

## 4. Flujo de una clase

### 4.1 La clase ya está preparada cuando abrís la app

No hay botón "generar clase". Un management command (`prepare_next_lesson`, corrido por cron de Railway a las 3 AM, o lazy la primera vez que se abre el dashboard si todavía no existe) deja la clase de hoy en estado `planned`. Como llegar al aula y que la profe ya tenga el plan.

La preparación decide sola:

1. **Skill** — la que tenga más días sin practicar, con speaking al menos 3 veces por semana.
2. **Track** — alterna work / general; nunca más de 2 seguidas del mismo.
3. **Grammar topic de la mini-lección** — ver §7b.
4. **Duración** — 20 min por defecto.

El dashboard muestra la clase preparada con un botón "Start lesson" y otro "Change skill or topic" que abre el picker manual (skill / track / duración / topic). Si la usuaria cambia algo, se regenera el plan solo para ese día.

### 4.1b Estructura de toda clase (para que se sienta como una clase real)

Cada plan tiene cinco fases con tiempos. El runner muestra la fase actual y el tutor sabe en cuál está.

| Fase | Min (de 20) | Qué pasa |
|---|---|---|
| **Warm-up** | 3 | Charla libre, corta, sobre el día. Sin corrección. Bajar la ansiedad. |
| **Mini-lesson** | 4 | La profe **enseña** el `grammar_topic` del día: explica en inglés simple, da 3 ejemplos contrastados con el español, y pide a la alumna que produzca 3-4 frases propias con la estructura. Aquí sí hay corrección explícita e inmediata. |
| **Practice** | 9 | La actividad principal de la skill (§5), diseñada para que la estructura de la mini-lección y los `due_errors` aparezcan naturalmente. |
| **Drill** | 3 | Preguntas rápidas y directas dirigidas a los `due_errors`. Tono más seco. |
| **Wrap-up** | 1 | La profe dice 2 cosas que salieron bien y 1 que va a ver mañana. Cierre humano. |

Para listening / reading / writing la mini-lesson es texto (una tarjeta explicativa con ejemplos + 3 frases para completar) y el warm-up se reduce a 1 minuto.

### 4.2 Generación del plan

Al iniciar, el `LessonPlanner` construye el contexto:

```python
context = {
    "cefr": learner.cefr_for(skill),
    "goal": learner.goal_statement,
    "topic": selected_topic,
    "duration_min": duration,
    "grammar_topic": selected_grammar_topic,   # el punto de la mini-lección
    "due_errors": ErrorItem.objects.due_for(learner)[:8],
    "due_vocab": VocabItem.objects.due_for(learner)[:10],
    "recent_topics": last_5_topic_titles,   # para no repetir
    "target_level": learner.target_level,
}
```

Y pide al LLM un plan en JSON. Estructura del plan según skill (ver §5). El plan se guarda en `Lesson.plan` y **no se regenera**: si la usuaria recarga, retoma el mismo plan.

Regla dura: **los `due_errors` tienen que estar embebidos en el contenido de la clase de forma natural**, no como un ejercicio de gramática aparte. Si arrastra el error "I have 5 years working here" → el tutor tiene que llevar la conversación a que hable de su experiencia laboral. Eso va explícito en el prompt del planner.

### 4.3 Ejecución

Ver §5, distinto por skill.

### 4.4 Cierre

Botón "End lesson". Dispara el analizador (§6), muestra spinner, y al terminar redirige al `LessonReport`.

---

## 5. Implementación por skill

### 5.1 Speaking

El más importante. Loop push-to-talk:

1. Botón grande de micrófono. `MediaRecorder` captura audio (webm/opus).
2. Al soltar: POST del blob a `/lessons/<id>/turn/`.
3. Backend: transcribe con `gpt-4o-transcribe`.
   - **Importante:** pasar un `prompt` de transcripción pidiendo transcripción verbatim, incluyendo muletillas y falsos comienzos. Por defecto los modelos de STT "limpian" la gramática, y eso destruye la señal que necesitamos para detectar errores. Si aun así limpia demasiado, aceptarlo y bajar la `confidence` de los errores de speaking a `medium`.
   - Guardar `audio_duration_ms` y `word_count` → de ahí sale el WPM.
4. Backend arma el mensaje al tutor con el historial de turnos + el plan + instrucción de sistema (§9.1).
5. Respuesta del tutor → TTS → se devuelve `{text, audio_url}`.
6. Frontend reproduce el audio y muestra el texto en la transcripción lateral.

**Corrección en vivo: mínima.** El tutor solo interrumpe si el error bloquea la comprensión. Si no, sigue la conversación y usa *recast* (repite lo que dijo bien formulado, sin señalarlo). La corrección explícita es trabajo del reporte post-clase. Esto va explícito en el prompt.

**Modo drill (últimos 5 min):** si el plan lo incluye, el tutor pasa a preguntas rápidas dirigidas a los `due_errors`. Cambia el tono: más directo, más repetición.

### 5.2 Listening

1. El planner genera un texto: monólogo o diálogo de 2 voces, al nivel CEFR, sobre el topic, sembrado con `due_vocab`.
2. TTS lo convierte a audio. Para diálogos, dos voces distintas del set de OpenAI.
3. **La usuaria no ve el texto.** Reproduce el audio (máximo 2 escuchas, contador visible).
4. Responde 4-6 preguntas de comprensión: 2 de gist, 3 de detalle, 1 de inferencia.
5. Al enviar: se corrige, se revela la transcripción con las respuestas resaltadas en el texto.
6. Opcional: velocidad de reproducción 0.85x / 1.0x. Empezar en 1.0.

### 5.3 Reading

1. Texto generado de 300-500 palabras al nivel CEFR + un poco más (i+1), sobre el topic.
   - Para track `work`: formato realista — un issue de GitHub, un fragmento de doc técnica, un post de blog de ingeniería, un email de un manager.
2. Glosario lateral con los términos objetivo (click en palabra → definición inline, y se crea/actualiza el `VocabItem`).
3. Preguntas de comprensión + una pregunta de producción escrita corta que fuerza a usar 2-3 términos del glosario.

### 5.4 Writing

1. Prompt de escritura contextualizado. Track `work`: "Write a PR description for...", "Reply to this Slack message from your tech lead", "Write the README intro for AutomatizaApp".
2. Textarea con contador de palabras y objetivo (150-250 palabras).
3. Al enviar, el corrector devuelve:
   - **Versión corregida con diff visual** (rojo tachado / verde). Renderizar server-side con `difflib` sobre la respuesta del LLM, no confiar en que el LLM genere HTML.
   - Cada corrección con su explicación breve en español.
   - Una **versión "upgraded"**: cómo lo diría un nativo del ámbito profesional. Esta es la más valiosa; separarla visualmente.
4. Todos los errores entran al pipeline con `confidence='high'` (writing es la señal más limpia que tenemos).

---

## 6. El analizador post-clase

Corre al cerrar la clase. Recibe la transcripción completa + el plan + los `due_errors` que estaban targeteados. Devuelve JSON estructurado (usar `json_schema`, no confiar en parsing libre).

### Taxonomía de errores (cerrada, no dejar que el LLM invente categorías)

| category | subcategory |
|---|---|
| `grammar` | `tense`, `aspect`, `articles`, `prepositions`, `word_order`, `agreement`, `conditionals`, `modals`, `plurals` |
| `vocabulary` | `wrong_word`, `false_friend`, `l1_interference`, `register`, `collocation` |
| `pronunciation` | `phoneme`, `word_stress`, `sentence_stress` — siempre `confidence='low'`, inferido de la transcripción |
| `fluency` | `fillers`, `self_correction`, `long_pause`, `circumlocution` |
| `discourse` | `connectors`, `politeness`, `directness` |

### Seed de errores típicos ES→EN

Sembrar el prompt del analizador con estos patrones para que los priorice (son los que casi todo hispanohablante arrastra):

- `I have 25 years` → `I am 25`
- `I have 5 years working here` → `I've been working here for 5 years`
- Presente simple donde va present perfect / present continuous
- Omisión de sujeto (`Is raining`)
- `people is`, `the people are` con artículo de más
- Preposiciones: `depends of`, `in Monday`, `arrive to`
- Falsos amigos: `actually`, `eventually`, `assist`, `realize`, `sensible`, `library`, `constipated`
- Orden adjetivo-sustantivo
- `explain me`, `it depends of`
- Traducción literal de "no" enfático, uso excesivo de `very`

### Salida del analizador

```json
{
  "summary_es": "...",
  "strengths": ["..."],
  "errors": [
    {
      "category": "grammar",
      "subcategory": "tense",
      "learner_produced": "I work here since 2021",
      "correction": "I've been working here since 2021",
      "explanation_es": "En español usás presente con 'desde'. En inglés, acción que empezó en el pasado y sigue = present perfect continuous.",
      "confidence": "high",
      "is_recycled": false
    }
  ],
  "recycled_error_ids_avoided": [12, 45],
  "new_vocabulary_produced": ["..."],
  "vocabulary_gaps": ["..."],
  "focus_next": ["..."],
  "cefr_signal": {"skill": "speaking", "estimate": "B1+", "reasoning": "..."}
}
```

### Post-procesamiento

- **Deduplicación:** antes de crear un `ErrorItem`, buscar uno activo del mismo `(category, subcategory)` con `learner_produced` semánticamente similar. Si existe → `occurrences += 1`, `srs_box = max(0, srs_box - 1)`, `next_review_at = hoy + 1 día`. Para la similitud, empezar simple: mismo subcategory + solapamiento de tokens > 0.5. No meter embeddings en v1.
- **`recycled_error_ids_avoided`:** los errores que estaban targeteados y que NO cometió → `srs_box += 1`, se recalcula `next_review_at`. Si `srs_box >= 5` → `status='mastered'`. **Esta es la métrica de progreso real de la plataforma.**
- **`cefr_signal`:** no actualizar el CEFR automáticamente en cada clase. Acumular las señales y actualizar el nivel solo cuando 3 clases consecutivas coinciden en un nivel distinto al guardado.

---

## 7. SRS

Leitner simple, sin sobreingeniería:

```
box:      0   1   2   3   4   5
días:     1   2   4   8   16  mastered
```

Acierto → sube de caja. Error → vuelve a caja 0.

Un `ErrorItem` es "due" si `next_review_at <= hoy` y `status='active'`.

---

## 7b. El syllabus A2→B2 (lo que una profe tiene en la cabeza)

El expediente de errores es reactivo. Una profe real además tiene un **programa**. Los dos se cruzan.

`GrammarTopic` se siembra con el programa gramatical del CEFR de A2 a B2, en orden. Referencia: el *English Grammar Profile* (British Council / Cambridge). Lista mínima para el seed, ~30 topics:

**A2 (consolidar):** present simple vs continuous · past simple (regular / irregular) · going to vs will · comparatives y superlativos · countable / uncountable + some / any / much / many · must / have to / should · there is / there are · frequency adverbs y su posición · preposiciones de tiempo y lugar (in / on / at).

**B1 (el grueso):** present perfect vs past simple · present perfect con since / for · present perfect continuous · past continuous vs past simple · used to · first y second conditional · modales de deducción (must be / can't be / might) · verbos + gerundio o infinitivo · phrasal verbs comunes · question tags · relative clauses (who / which / that) · too / enough · artículos con sustantivos generales · voz pasiva (present / past).

**B2 (el objetivo):** past perfect · third conditional y mixed conditionals · reported speech · wish / if only · modales del pasado (should have / could have) · voz pasiva avanzada (it is said that…) · future perfect y continuous · inversión básica (not only… but also) · cleft sentences (what I need is…) · linkers de discurso (whereas, although, despite, in order to) · registro formal vs informal en email.

### Cómo se elige la mini-lección de cada día

El `prepare_next_lesson` aplica esta regla, en orden:

1. Si hay un `ErrorItem` con `occurrences >= 3` cuya subcategory mapea a un `GrammarTopic` no dominado → ese topic. **El error manda sobre el programa.**
2. Si no, el siguiente topic del programa con `status='not_started'`.
3. Cada 5 clases, en vez de topic nuevo, se repasa uno en `practicing` que lleve más tiempo sin aparecer.

Un topic pasa a `mastered` cuando fue targeteado en 4 clases y evitado en 3 de ellas.

### La pantalla Grammar

Dos bloques, uno arriba del otro:

1. **Your program to B2:** los topics del syllabus en orden, agrupados por nivel, con estado (not started / introduced / practicing / mastered). Al tocar uno: explicación contrastiva ES→EN, ejemplos, y un botón "Drill this now" que abre un mini-runner de 5 min solo con esa estructura.
2. **Your recurring errors:** los patrones que salen de `ErrorItem`, como en el prototipo.

La barra "progress to B2" del dashboard = topics mastered / total topics hasta B2.

---

## 7c. Seguimiento de nivel

Tres mecanismos, cada uno con un rol distinto:

**1. Placement (una vez).** En el onboarding la app pide el resultado del **EF SET** (externo, gratis, 50 min, da nivel por listening y reading) y lo carga a mano. Para speaking y writing, la primera clase es un `checkpoint` corto (ver abajo) que estima el nivel. Se guarda en `placement_notes` para poder comparar después. No inventar un placement propio en v1: EF SET es mejor que cualquier cosa que se pueda armar en un fin de semana.

**2. Señal continua (cada clase).** El analizador devuelve `cefr_signal` por skill. Se acumula en `LessonReport`. El `cefr_<skill>` del `Learner` **solo se actualiza cuando 3 clases consecutivas de esa skill coinciden en un nivel distinto al guardado.** Un mal día no baja el nivel; un buen día no lo sube.

**3. Checkpoint (cada 4 semanas).** Una clase especial de 30 min, `skill='checkpoint'`, que el `prepare_next_lesson` programa automáticamente cuando pasaron 28 días del último. Cubre las cuatro skills en versión corta:
- Listening: audio de 2 min + 4 preguntas
- Reading: texto de 250 palabras + 4 preguntas
- Writing: 120 palabras sobre un prompt de trabajo
- Speaking: 6 min de conversación con preguntas de dificultad creciente hasta que la alumna se traba

El analizador del checkpoint tiene un prompt aparte: no busca errores para el expediente, **evalúa nivel** con los descriptores CEFR *can-do* por skill, y devuelve por cada una: `estimate`, `evidence` (3 frases de la alumna que justifican el nivel) y `gaps_to_target` (qué le falta concretamente para B2). Eso se muestra como un reporte en español, tipo devolución de profe: "estás acá, esto es lo que te separa de B2".

El resultado del checkpoint sobreescribe los `cefr_<skill>` directamente (a diferencia de la señal continua). Es la medición "oficial".

---

## 8. Pantallas (v1)

**Hay un prototipo HTML navegable (`tutor-en-prototype.html`) que fija el layout, la paleta y la tipografía. Reproducirlo en Django templates + Tailwind; no rediseñar.**

Sidebar con dos grupos: *Practice* (Speaking, Listening, Reading, Writing) y *Your English* (Grammar, Vocabulary, My errors, Progress), más *Today* arriba.

1. **Today** — la clase preparada con los errores que ataca y el grammar topic del día, racha, nivel por skill con barra "progress to B2", últimas clases. Botones: "Start lesson", "Change skill or topic".
2. **Lesson runner** — una plantilla por skill, todas con el indicador de fase (warm-up / mini-lesson / practice / drill / wrap-up) y timer. Speaking: mic grande, transcripción con recasts en verde, panel lateral con plan y estado de errores targeteados (missed / pending / avoided).
3. **Lesson report** — summary, fortalezas, errores nuevos vs recuperados, errores evitados, botón "Practice these now".
4. **Grammar** — programa A2→B2 arriba, errores recurrentes abajo (§7b).
5. **Vocabulary** — tarjetas por estado target / emerging / acquired.
6. **My errors** — tabla filtrable con cajitas SRS, dismiss manual.
7. **Progress** — lecciones, errores dominados, % de errores evitados (el número grande), WPM y fillers, y el último checkpoint con su devolución.

Diseño según el prototipo: fondo tinta azul-gris (no negro), texto crema, un solo acento ámbar para lo pendiente, verde solo para progreso, rojo solo para errores nuevos. Sans para la interfaz, serif para todo el texto en inglés que se lee o se escucha. Sin gamificación infantil, sin mascotas, sin confetti.

---

## 9. Prompts de sistema

Los tres son archivos de texto versionados en `tutor/prompts/`, no strings hardcodeados. Se cargan con un helper que permite override por env var para poder iterar rápido.

### 9.1 Tutor en vivo (speaking)

Debe incluir, sí o sí:

- Rol: tutora de inglés experimentada, nativa, que enseña a hispanohablantes profesionales.
- Nivel objetivo del alumno y adaptación del input a i+1: no simplificar de más, no usar vocabulario 2 niveles por encima.
- **Consciente de la fase.** Recibe la fase actual en cada turno. En *warm-up* y *practice*: no corregir explícitamente salvo que el error impida la comprensión, usar recast. En *mini-lesson* y *drill*: corrección explícita e inmediata, en inglés simple, con el ejemplo correcto y pedido de repetición. En *wrap-up*: dos cosas buenas y una a trabajar, y despedirse.
- **En la mini-lesson enseña, no conversa.** Explica el `grammar_topic` con el `summary_es` como base pero hablando en inglés, da los 3 ejemplos, pide 3-4 frases propias a la alumna, corrige cada una.
- **Regla de turnos:** las respuestas del tutor son cortas. Máximo 3-4 oraciones. El alumno tiene que hablar el 70% del tiempo. Terminar casi siempre con una pregunta abierta.
- Los `due_errors` targeteados de esta clase: llevar la conversación hacia contextos donde esas estructuras sean necesarias.
- **Nunca hablar en español.** Si la alumna no entiende, reformular más simple en inglés.
- Si la alumna se queda trabada más de un turno: dar la palabra o la estructura y seguir. No dejar que se hunda.

### 9.2 Planner

- Recibe el contexto de §4.2, devuelve JSON con el plan.
- Regla: los `due_errors` se embeben en el contenido de forma natural, no como ejercicios sueltos.
- Regla: no repetir topics de `recent_topics`.
- Estructura del plan varía por skill; definir un `json_schema` por skill.

### 9.3 Analizador

- Recibe transcripción completa + errores targeteados.
- Taxonomía cerrada (§6). Rechazar categorías inventadas en validación.
- Sembrado con la lista de errores típicos ES→EN.
- **Anti-inflación:** no reportar como error algo que es simplemente una forma menos idiomática pero correcta. Máximo 8 errores por clase, priorizados por impacto comunicativo. Es mejor 4 errores accionables que 15 que abruman.
- Las `explanation_es` son contrastivas: explicar *por qué el español induce ese error*. Esa es la explicación que se pega.

---

## 10. Scope

Tres fines de semana. Cada uno termina con algo usable de verdad.

### Fin de semana 1 — una clase de speaking completa, de punta a punta
- Modelos + migraciones + admin
- Auth (usuario único, `createsuperuser` alcanza)
- Seed: tracks, ~20 topics (10 work, 10 general), los ~30 `GrammarTopic` del syllabus
- `prepare_next_lesson` (lazy al abrir el dashboard; el cron viene después)
- Planner → **speaking runner con las 5 fases** funcionando end to end
- Analizador post-clase + `ErrorItem` con deduplicación
- Lesson report
- Deploy en Railway

**Criterio de éxito:** dar una clase de speaking de 20 minutos que se sienta como una clase (warm-up, mini-lección, práctica, drill, cierre) y que el reporte muestre errores reales y accionables.

### Fin de semana 2 — las otras 3 skills + el ciclo cerrado
- Runners de listening, reading y writing (con mini-lesson en texto)
- SRS: cálculo de `due`, inyección en el planner, promoción/degradación de cajas
- Regla de selección de mini-lesson (§7b) y estados de `LearnerGrammarTopic`
- Today con racha, nivel por skill y barra a B2
- Pantallas Grammar, Vocabulary, My errors
- Cron de Railway para `prepare_next_lesson`

**Criterio de éxito:** que un error cometido en writing el lunes aparezca targeteado en la clase de speaking del jueves, y que Grammar muestre el programa avanzando.

### Fin de semana 3 — nivel y pulido
- Checkpoint mensual con su analizador y reporte
- Onboarding con carga del EF SET
- Señal continua de CEFR con la regla de 3 clases
- Pantalla Progress
- Pulido de UI contra el prototipo, mobile

**Criterio de éxito:** correr un checkpoint y recibir una devolución que diga concretamente qué te falta para B2.

### Explícitamente fuera de v1
- Realtime API / conversación con interrupciones
- Multi-usuario, onboarding, billing
- Embeddings para deduplicación semántica
- Celery / workers
- App móvil
- Análisis real de pronunciación (requiere forced alignment, es otro proyecto)
- Cualquier feature social

---

## 11. Criterios de aceptación

1. Una clase de speaking de 15 min genera al menos 3 `ErrorItem` con corrección y explicación coherentes.
2. Un `ErrorItem` cometido dos veces tiene `occurrences=2` y no dos filas duplicadas.
3. Un error targeteado y no cometido sube de `srs_box` y su `next_review_at` se aleja.
4. El plan de una clase incluye visiblemente al menos 2 de los `due_errors` en su contenido.
5. Las 4 skills escriben en la misma tabla `ErrorItem` con la misma taxonomía.
6. Toda clase pasa por las 5 fases y el tutor cambia de comportamiento en la mini-lesson y el drill (corrección explícita) vs practice (recast).
7. Un `ErrorItem` con 3 ocurrencias fuerza su `GrammarTopic` como mini-lección de la próxima clase, por encima del programa.
8. Un checkpoint devuelve por cada skill un nivel, 3 frases de evidencia y los gaps concretos hacia B2.
9. La app entera está en inglés.
10. Deployada en Railway y usable desde el celular.

---

## 12. Notas de implementación

- **Empezar por el analizador, no por la UI.** Escribir una transcripción de prueba a mano, hacer que el analizador devuelva JSON válido y útil, y recién ahí construir alrededor. Si el analizador es flojo, el producto entero no sirve, y es la parte más fácil de subestimar.
- Guardar siempre `raw_analysis` completo. Vas a querer re-procesar clases viejas cuando mejores el prompt.
- Los audios ocupan espacio: guardar solo los de los últimos 30 días, o solo la transcripción si el storage aprieta.
- Manejar el error de la API con gracia: si falla el TTS, mostrar el texto igual y seguir. Nunca perder una clase por un timeout.
- Tests: uno de integración sobre el post-procesamiento del analizador (dedup, SRS) con un JSON fixture. El resto puede esperar.

---

## 13. Secuencia de construcción, módulo por módulo

Orden estricto. Cada paso deja algo que corre. No pasar al siguiente sin cumplir el "listo cuando".

### Fin de semana 1

| # | Módulo | Qué se hace | Listo cuando |
|---|---|---|---|
| 1 | `core` | Proyecto Django, settings por env (`django-environ`), PostgreSQL local, Tailwind vía CDN por ahora, HTMX. Un `base.html` con el sidebar del prototipo. | `runserver` muestra el layout vacío con sidebar. |
| 2 | `learners` | Modelo `Learner` + `Track`, `Topic`, `GrammarTopic`, `LearnerGrammarTopic`. Migraciones. Admin. Fixture `seed.json` con tracks, 20 topics, 30 grammar topics. | `loaddata seed` carga todo y se ve en el admin. |
| 3 | `lessons` (modelos) | `Lesson`, `Turn`, `ErrorItem`, `VocabItem`, `LessonReport`, `Checkpoint`. Managers `due_for(learner)`. Índices. | Tests de los managers pasan. |
| 4 | `ai/client.py` | Wrapper sobre la SDK de OpenAI: `chat_json(schema)`, `transcribe(file)`, `speak(text, voice)`. Reintentos, timeouts, logging de tokens. | Un script de prueba hace las 3 llamadas. |
| 5 | `ai/analyzer.py` | Prompt del analizador (§9.3) como archivo en `ai/prompts/analyzer.md`. `json_schema` con la taxonomía cerrada (§6). Función `analyze(lesson) -> AnalysisResult`. | **Probado contra `fixtures/transcript_sample.md`, una transcripción escrita a mano con 6 errores plantados. Devuelve al menos 4 con corrección y explicación coherentes.** Este es el paso más importante del proyecto. |
| 6 | `lessons/postprocess.py` | Convierte `AnalysisResult` en `ErrorItem` / `VocabItem`: deduplicación, `occurrences`, SRS (§7), promoción de `LearnerGrammarTopic`. | Test: correr el mismo análisis dos veces deja `occurrences=2` y una sola fila. |
| 7 | `ai/planner.py` | Prompt del planner (§9.2) + `prepare_next_lesson(learner)` con la regla de selección (§7b). Guarda `Lesson.plan` con las 5 fases. | Un management command crea la clase de hoy y el plan se lee bien en el admin. |
| 8 | `lessons/views` speaking | Runner de speaking: vista, template, JS de `MediaRecorder`, endpoint `POST /lessons/<id>/turn/` que transcribe → tutor → TTS → devuelve `{text, audio_url}`. Tutor prompt (§9.1) consciente de fase. Timer y cambio de fase en el frontend. | Una clase de 20 min de punta a punta, con voz. |
| 9 | `lessons/views` report | "End lesson" → `analyze` → `postprocess` → página de reporte. | El reporte muestra errores reales de la clase del paso 8. |
| 10 | deploy | Railway: Postgres, variables, `collectstatic`, storage de audios en volumen. | Se usa desde el celular. |

### Fin de semana 2

| # | Módulo | Qué se hace | Listo cuando |
|---|---|---|---|
| 11 | `today` | Dashboard: clase preparada con errores y grammar topic, "Start" / "Change skill or topic", picker manual, racha, nivel por skill. | Igual al prototipo. |
| 12 | writing | Runner: prompt contextual, textarea, corrección con diff (`difflib` server-side), versión "upgraded", errores al pipeline con `confidence='high'`. | Un error de writing aparece en My errors. |
| 13 | reading | Runner: texto generado, glosario con click → `VocabItem`, preguntas, producción corta. | — |
| 14 | listening | Runner: TTS con 2 voces para diálogo, 2 escuchas máximo, preguntas, revelado de transcripción. | — |
| 15 | mini-lesson en texto | Tarjeta de mini-lección para las 3 skills sin voz (explicación + 3 frases a completar), corregidas y enviadas al pipeline. | — |
| 16 | grammar | Pantalla Grammar: programa A2→B2 con estados + errores recurrentes. Mini-runner "Drill this now". | Un `ErrorItem` con 3 ocurrencias cambia la mini-lección de mañana. |
| 17 | errors + vocab | Pantallas My errors (tabla, filtros, dismiss) y Vocabulary (tarjetas por estado). | — |
| 18 | cron | `prepare_next_lesson` como cron de Railway a las 3 AM. | La clase existe antes de abrir la app. |

### Fin de semana 3

| # | Módulo | Qué se hace | Listo cuando |
|---|---|---|---|
| 19 | checkpoint | Runner de checkpoint (4 mini-pruebas) + `ai/level_assessor.py` con prompt aparte y descriptores CEFR. Reporte en español con evidencia y gaps. Programación automática a los 28 días. | Un checkpoint devuelve nivel + evidencia + gaps por skill. |
| 20 | onboarding | Pantalla única: carga del EF SET, objetivo, nivel target. Primer checkpoint corto para speaking/writing. | — |
| 21 | level tracking | Acumulación de `cefr_signal`, regla de 3 clases consecutivas, barra "progress to B2". | — |
| 22 | progress | Pantalla Progress: métricas, gráfico de errores evitados, WPM y fillers, último checkpoint. | — |
| 23 | pulido | Revisión de cada pantalla contra el prototipo, mobile, estados vacíos, manejo de errores de API. | — |

### Estructura de carpetas sugerida

```
tutor_en/
  config/            settings, urls, wsgi
  core/              base templates, sidebar, helpers
  learners/          Learner, Track, Topic, GrammarTopic
  lessons/           Lesson, Turn, ErrorItem, VocabItem, reports, runners, postprocess
  ai/                client.py, planner.py, analyzer.py, level_assessor.py, prompts/
  fixtures/          seed.json, transcript_sample.md
  static/            js/recorder.js, css
  templates/
```
