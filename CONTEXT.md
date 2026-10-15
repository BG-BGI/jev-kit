# jev-kit

Guards, memory and decision plumbing wrapped around a coding agent, with Jev
(TypeSafe System One) as the cheap calibrated judge and Claude as the author.

## Language

**Experience graph**:
The record of what happened while working: episodes and the knowledge
distilled from them.
_Avoid_: knowledge graph (ambiguous with the code graph), memory

**Code graph**:
The graphify-out map of what the code is: structure, communities, cross-file
relationships.
_Avoid_: knowledge graph

**Bucket**:
One of the eight experience-graph node types: failure, fix, pattern, danger,
decision, constraint, doc, env.
_Avoid_: category, tag

**Episode**:
A time-stamped bucket node born from one concrete session event.
_Avoid_: memory, entry

**Pattern**:
A distilled "do X when Y", only ever promoted from three or more supporting
episodes, never written directly.

**Constraint**:
A standing rule the human stated explicitly, stored verbatim with
provenance, never expiring, and exempt from the confidence gate; a detected
correction only triggers an offer, never a write.
_Avoid_: preference, rule (reserved for Airlock's rules table)

**Candidate**:
An event a local rule nominated for storage that Jev has not yet judged.

**Scope**:
A node's visibility: one repo, this machine, or global. One database, scope
filtered at read time.

**Error signature**:
The exact identity of a failure: a hash of tool name, exception type and the
placeholder-normalized message head, with no stack frames.
_Avoid_: fingerprint, error hash

**Entity**:
A prose-free anchor node edges attach to: a file path, component, command,
error signature or skill name.

## Relationships

- A **fix** always carries a `fixes` edge to a **failure**
- A **pattern**'s trust is the count of its `evidence-for` edges from
  **episodes**
- Every **bucket** node carries `applies-to` edges to **entities**
- The **experience graph** and the **code graph** are separate stores, joined
  only by **entity** names

## Example dialogue

> **Dev:** "The agent succeeded at the deploy, store a success node?"
> **Domain expert:** "There is no success bucket. If it resolved a recorded
> **failure**, it is a **fix**; if it is the third time this approach worked,
> it is evidence for a **pattern**; otherwise it is noise."

## Flagged ambiguities

- "knowledge graph" was used for both the code graph and the experience
  graph — resolved: two distinct stores, say which one. The component dir is
  `knowledge/`, but the thing it holds is the **experience graph**, the same
  way `airlock/` holds the guard.
- "rule" means an Airlock rules-table entry (R1..Rn), never a human
  **constraint** — resolved: use **constraint** for the human's standing
  instructions.
