---
category: general
priority: low
---

# How to write notes for the agents (template)

This file only explains the notes format. It contains no trading instruction
and can be ignored when deciding a trade.

Every `.md` or `.txt` file in this folder is loaded into the agents' memory
when the app starts, and the strategy and news agents search it with
`query_user_notes`. One file can hold one note, or several notes separated by a
line containing only three dashes.

Start a note with a small header to label it:

    ---
    category: instruction
    priority: high
    ---
    Say what to do, when it applies, and why.

- `category`: instruction, observation, market_insight, strategy_hint or general.
- `priority`: critical, high, normal or low.

Keep each note short and specific. Delete a note when it stops being true: the
agents cannot tell an old note from a current one. Copy this file to start a
new note, or delete it once you have notes of your own.
