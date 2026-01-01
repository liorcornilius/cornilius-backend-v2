
# Logs Table — Design, Semantics, and Constraints

This document describes the `public.logs` table: its purpose, column semantics, constraints, time calculations, and invariants.

It is written to be **agent-friendly**: an automated agent should be able to read this file and correctly apply logic (evaluation, aggregation, backfills) without guessing.

---

## Purpose

The `logs` table stores **time-stamped measurements or events** for a given **user** and **tracker**.

A single log row represents:

> “At time **T**, tracker **X** for user **U** had value **V**.”

Logs are **raw facts**.  
They do **not** store goals, progress, streaks, or evaluation results.

---

## Core Design Principles

1. **Logs are immutable facts**
   - Logs describe what happened, not how it was evaluated.
   - Reprocessing logs should always be safe.

2. **Logs do NOT reference goals**
   - Goals evaluate logs indirectly via `tracker_id` and time windows.
   - This allows multiple goals per tracker and backfills.

3. **Exactly one value per log**
   - A log represents one measurement, not multiple metrics.
   - Multi-metric payloads must live inside `value_json`.

4. **Typed columns for hot paths**
   - Numeric, text, and JSON values are stored in typed columns.
   - JSON is allowed only for optional or rarely queried data.

---

## Table: `public.logs`

---

## Columns

### Identity

- `id uuid PRIMARY KEY DEFAULT gen_random_uuid()`
  - Unique identifier for the log row.

---

### Ownership & Relationship

- `user_id uuid NOT NULL`
  - Owner of the log.

- `tracker_id uuid NOT NULL`
  - Tracker this log belongs to (weight, running, expenses, etc.).

> Logs are typically queried by `(user_id, tracker_id, timestamp range)`.

---

### Value Payload (Exactly One Required)

The table supports flexible payloads via **three mutually exclusive columns**:

- `value_number numeric NULL`
- `value_text text NULL`
- `value_json jsonb NULL`

Postgres guarantees:
- `value_text` is always a string
- `value_json` is always valid JSON
- `value_number` is numeric

**Invariant:** exactly ONE of these columns must be non-null.

Recommended constraint:
```sql
((value_number is not null)::int +
 (value_text   is not null)::int +
 (value_json   is not null)::int) = 1








# Goals Table  — Design & Constraints

 **Goals** table used to define how user progress is evaluated over time.

The table is **declarative**:
- It defines *what success means*
- It does **not** store results, progress, or state

Evaluation output belongs in a separate table (e.g. `goal_evaluations`).

---

## Design Principles

1. **One tracker → multiple goals**
   - A tracker (weight, running, finance, etc.) can have many goals.
   - Example for weight:
     - “Lose 0.5kg per week”
     - “Reach 90kg”
     - “Maintain ±1kg for 30 days”

2. **No computed state**
   - No `goal_reached`, no streaks, no counters.
   - Goals define rules only.

3. **Strict constraints**
   - Invalid goal definitions should fail at insert time.
   - The DB enforces correctness, not just application code.

4. **Milestone-ready**
   - Goals can be grouped and sequenced for milestone / program logic.

---

## Supported Goal Types

`measure_type` defines how the goal is evaluated.

| Type | Meaning | Example |
|---|---|---|
| `frequency` | Count qualifying events per period | Run ≥5km **3× per week** |
| `delta` | Change over a period | Lose **0.5kg per week** |
| `target` | Reach a value | Reach **90kg** |

Each type has **different required columns**, enforced by constraints.

---

## Table Overview

**Table:** `public.goals`

### Identity & Ownership

| Column | Type | Notes |
|---|---|---|
| `id` | uuid | Primary key |
| `user_id` | uuid | Goal owner |
| `tracker_id` | uuid | Tracker being evaluated |

---

### Grouping & Milestones

| Column | Type | Notes |
|---|---|---|
| `goal_group_id` | uuid | Groups goals into programs/milestones |
| `sequence_index` | int | Order inside group (0 = first) |

Used for milestone chains and progressive goals.

---

### Core Goal Definition

| Column | Type | Notes |
|---|---|---|
| `measure_type` | text | `frequency`, `delta`, `target` |
| `direction` | text | `up` or `down` |
| `unit` | text | kg, km, $, etc. |
| `agg_method` | text | `min`, `max`, `avg`, `sum`, `last` |

---

### Period / Recurrence

| Column | Type | Notes |
|---|---|---|
| `interval_value` | int | Period size (e.g. 1) |
| `interval_unit` | text | `h`, `d`, `w`, `m`, `y` |

Required for:
- `frequency`
- `delta`

Not required for:
- `target`

---

### Numeric Goal Fields

| Column | Type | Used by | Meaning |
|---|---|---|---|
| `frequency` | int | frequency | Required count per period |
| `delta_goal` | numeric | delta | Required change per period |
| `goal_end_point` | numeric | target | Final target value |
| `goal_start_point` | numeric | optional | Fixed baseline (rare) |

---

### Threshold Logic

| Column | Type | Meaning |
|---|---|---|
| `threshold_type` | text | `gte`, `lte`, `between`, `eq` |
| `threshold` | numeric | Used for non-between comparisons |
| `threshold_min` | numeric | Lower bound (between) |
| `threshold_max` | numeric | Upper bound (between) |

---

### Time Window

| Column | Type | Meaning |
|---|---|---|
| `starts_at` | timestamptz | Evaluation start |
| `ends_at` | timestamptz | Optional end |

---

### Lifecycle & Metadata

| Column | Type | Meaning |
|---|---|---|
| `is_active` | boolean | Enables/disables evaluation |
| `meta` | jsonb | UI hints, notes, experiments |
| `created_at` | timestamptz | Created time |
| `updated_at` | timestamptz | Last update |

---

## Constraints & Validity Rules

### Measure Type Requirements

- **frequency**
  - `frequency IS NOT NULL`
  - `interval_value IS NOT NULL`
  - `interval_unit IS NOT NULL`

- **delta**
  - `delta_goal IS NOT NULL AND delta_goal > 0`
  - `interval_value IS NOT NULL`
  - `interval_unit IS NOT NULL`

- **target**
  - `goal_end_point IS NOT NULL`

---

### Threshold Rules

- `threshold_type = 'between'`
  - `threshold_min` and `threshold_max` required
  - `threshold_min ≤ threshold_max`

- Other threshold types
  - `threshold` required

---

### General Validity Rules

- `frequency > 0` (if present)
- `interval_value > 0` (if present)
- `sequence_index ≥ 0` (if present)
- `ends_at > starts_at` (if present)

---

## What This Table Does NOT Store

❌ Progress  
❌ Streaks  
❌ “Goal reached” flags  
❌ Period results  

Those belong in **evaluation tables** (e.g. `goal_evaluations`).

---

## Examples

### Frequency Goal — Running
> Run ≥5km **3× per week**

- `measure_type = 'frequency'`
- `frequency = 3`
- `interval_value = 1`
- `interval_unit = 'w'`
- `threshold_type = 'gte'`
- `threshold = 5`

---

### Delta Goal — Weight Loss
> Lose **0.5kg per week**

- `measure_type = 'delta'`
- `delta_goal = 0.5`
- `direction = 'down'`
- `agg_method = 'last'`
- `interval_value = 1`
- `interval_unit = 'w'`

---

### Target Goal — Milestone
> Reach **90kg**

- `measure_type = 'target'`
- `goal_end_point = 90`
- `direction = 'down'`
- `threshold_type = 'lte'`

---

## Why This Design

- Supports milestones and programs
- Prevents invalid goal definitions
- Keeps evaluation logic simple
- Scales to new goal types
- Avoids schema rewrites later

---

**Rule of thumb:**  
If a value can change over time → it does NOT belong in this table.
