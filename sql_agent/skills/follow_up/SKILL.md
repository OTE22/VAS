# Follow-up Reference Resolution

## Purpose
Work out what a short message refers to.

## When to Use
Something already exists to refer to: "only camera 3", "the same in Arabic",
"that one", "go back".

## Available Tools
`get_task_state`, `list_my_documents`, `modify_active_query`

## Process
1. Decide what the reference points at: the last result, a document, or the
   current subject.
2. If genuinely unclear, check with a look-up before asking.
3. Apply the change to THAT. A fresh query for "make that a PDF" answers a
   question nobody asked.

## Constraints
- A follow-up modifies the current task; it does not start a new one.
- "The report in Arabic", "make it in English", "بالعربية" about something
  already produced is a TRANSLATION of that thing (`translate_document`).
  It never runs a query and never creates a new document.
- An explicit new subject REPLACES the old one: "Track Ali" after "Track
  Joey" is about Ali, and nothing of Joey survives.
- A pronoun keeps the existing subject.
- Refer only to results and documents from THIS conversation.

## Output
The refined work, described in terms of what changed.
