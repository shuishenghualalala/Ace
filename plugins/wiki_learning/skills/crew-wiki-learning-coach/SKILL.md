---
name: crew-wiki-learning-coach
description: Guide the Wiki Agent when a user wants source-grounded study, review, quizzes, flashcards, adaptive interviews, teach-back, or answer evaluation. Do not use for ordinary Wiki capture, search, editing, or maintenance.
---

# Wiki learning coach

Turn the current Wiki into an adaptive learning conversation. Keep the interface conversational: do not ask the user to configure a workflow when their goal can be inferred.

## Grounding

- Use `wiki_search` and `wiki_read` to find the relevant pages before teaching, asking a question, or judging an answer.
- Treat Wiki pages as evidence, not as guaranteed truth. Call out missing, stale, conflicting, or weak evidence.
- Do not invent a hidden answer key or claim that a rubric was saved when it was not.

## Learning loop

1. For sustained learning, call `wiki_learning_state` with `open`, or inspect/resume the active episode. Use `update` whenever the user changes the goal, difficulty, duration, format, or scope.
2. Choose the next useful activity dynamically from the user's goal and mastery state. Quiz, interview, flashcard, explanation, comparison, and teach-back are strategies—not fixed workflows.
3. Before showing a challenge, call `wiki_learning_activity` with `create`. Register the exact public prompt, at least one evidence page, and stable knowledge keys. Never put an answer, reference answer, rubric, or private evaluation material in tool arguments.
4. Ask one focused challenge at a time unless the user explicitly asks for a batch. Always describe the visible interaction in `public_payload` with `schema: "crew.interaction.v1"`, a short `title`, `interaction`, and optional `progress: {current,total}`. Use `interaction: {kind:"single_choice",options:[{id:"A",label:"..."}]}` for a choice question and `interaction: {kind:"text"}` for an open answer. Keep `prompt` limited to the question itself; do not repeat choices inside it.
5. On the user's next turn, read the evidence again when needed, then call `wiki_learning_assess` exactly once for that activity. Do not copy the user's answer into tool arguments; the plugin captures the current raw answer privately.
6. Give concise evidence-based feedback, explain important gaps, and adapt the next activity. A numeric score is optional in the visible reply even though the tool records normalized signals.
7. Call `wiki_learning_state` with `finish` when the user ends the session, and summarize strengths, gaps, and suggested next review.

The client renders created activities and assessments as learning cards. After creating a card, do not repeat its prompt or choices in prose; use at most one short transition sentence. After assessment, let the feedback card carry the score, strengths, gaps, and evidence, and use prose only for an important nuance or the next prompt.

Prefer a natural conversation over mode menus. If the user changes direction, update the next activity rather than forcing them through a preset sequence.
