# Database Investigation

## Purpose
Answer questions about what the cameras have seen: detections, people,
cameras, times.

## When to Use
The user asks what the system holds or has observed.

## Context
The state block above: `referenced_entity` is the subject; `active_task` is a
summary that may lag.

## Available Tools
`resolve_person`, `list_cameras`, `query_database`, `modify_active_query`

## Process
1. If the request names a PERSON, resolve them first. The name as typed is
   rarely the name as stored, so a filter on it finds nothing — and "no rows"
   then looks identical to "no such person", which are different answers.
2. Ask the question in plain words.
3. Read what came back; decide if one more look is genuinely needed.
4. Explain what it means, not what the query did.

## Constraints
- Never write SQL. Describe the question; the system writes and checks it.
- Use ids exactly as a look-up returned them; never invent one.
- Zero rows can be correct. It is only suspicious when narrowed to a person.
- If a look-up finds nobody, say so rather than querying anyway.

## Output
An answer grounded in the rows returned — real names, times, cameras. Never
describe data you were not given.
