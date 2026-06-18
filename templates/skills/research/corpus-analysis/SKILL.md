---
name: corpus-analysis
description: |
  Generar análisis, cruces (cross-references) y comparaciones sobre la Research
  Library YA indexada por sección en HMK — y escribir el resultado DE VUELTA a la
  memoria para que la base crezca orgánicamente. Es un PLAYBOOK (cómo recuperar,
  sintetizar, citar y persistir), NO un análisis enlatado: el análisis concreto
  es criterio del agente sobre el contenido real y la pregunta.
version: 1.0.0
author: Local System
license: MIT
metadata:
  hermes:
    tags: [Research, Analysis, CrossReference, Corpus, HMK, Synthesis]
    related_skills: [librarian, library-acquisition]
prerequisites:
  commands: [python3]
---

# Análisis y cruces sobre el corpus

La Research Library está **indexada por sección** en `library.db` (cada `##` →
un chapter, tag `corpus/section/<topic>/<slug>`; lo hace `library-acquisition`).
Esto es el sustrato. Este skill es el **loop** para operar sobre él. El juicio
analítico (qué conectar, cómo encuadrar) es tuyo; esto te da la mecánica.

## El loop: recuperar → sintetizar → citar → write-back

### 1. Encuadrar
Definí la pregunta concreta (un análisis, una comparación entre autores/secciones,
un cruce temático). Sé específico — la calidad del retrieval depende de eso.

### 2. Recuperar (cross-sección / cross-libro)
```bash
./scripts/hmk memoryctl.py hybrid-pack --query "<pregunta>" --budget 2400 --limit 8 --threshold 0.35
```
- Usá **`hybrid-pack`** (léxico + semántico), no `search` solo.
- Trae secciones del corpus **y** memorias relacionadas (research, notas) — esa mezcla
  ES el material de cruce.
- `expand --id N` para el texto completo de una sección (salta al markdown en disco) y
  para ver sus `neighbors` (links ya existentes).

### 3. Sintetizar (tu criterio)
Compará, conectá, argumentá. Reglas:
- **Cada afirmación anclada** a una sección/memoria recuperada — nada inventado ni
  conexiones no sostenidas por el texto.
- Aceptá `null_retrieval`: si no hay base, decilo; no rellenes.
- Distinguí lo que dice una fuente vs tu inferencia.

### 4. Citar (doble)
- Corpus: *(Autor, Año, cap. X[, p. Y])*.
- Memoria: `[mem:N]`.

### 5. Write-back — el flywheel (OBLIGATORIO si el análisis tiene valor durable)
Guardá la síntesis como un **chapter nuevo** para que re-entre a la base y aparezca
en futuros retrievals:
```bash
./scripts/hmk memoryctl.py add-text \
  --shelf library \
  --title "Análisis: <tema>" \
  --raw "<síntesis con citas>" \
  --tags analysis,cross-ref,<topic> \
  --importance 0.7
./scripts/hmk memoryctl.py embed-backfill
```
Y **linkeá** el análisis a sus fuentes (los cruces se vuelven explícitos y navegables):
```bash
./scripts/hmk memoryctl.py link --src <analysis_id> --dst <section_id> --type cites --note "<por qué>"
```
`expand --id <analysis_id>` luego mostrará las fuentes como `neighbors`.

### 6. Entregar (opcional)
Texto, o lectura en voz vía el skill [`pdf-to-audio`] / la tool `text_to_speech`.

## Por qué así
El análisis no se pre-computa: emerge de la biblioteca poblada + la pregunta. Lo que
SÍ es estable es el loop (recuperar→sintetizar→citar→persistir) y que **lo derivado
se escribe de vuelta** — así la base crece con el conocimiento generado, no solo con
material ingerido. Para acumular sin duplicar, antes de escribir un análisis fijate si
ya existe uno equivalente (`hybrid-pack`) y, si corresponde, amplialo en vez de duplicar.
