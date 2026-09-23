# The grilling method, trimmed for a kickoff interview

The source method interviews someone relentlessly about a plan, one question at a time,
until a shared understanding exists, with a recommended answer attached to every question.
A kickoff interview keeps the relentlessness and the recommendations, and trades
one-at-a-time for batched rounds, because the user is answering configuration questions
whose answers rarely depend on each other and three rounds is the ceiling.

## The stance

You are not collecting preferences. You are trying to find the answer that, if wrong,
makes the generated pipeline do something the user did not want on a night nobody is
watching. Ask like that is the stake, because it is.

If a question can be answered by looking at the machine or the vault's folder list,
look instead of asking. Asking a user what plugins they have installed when the file is
readable wastes one of your questions and tells them you did not do your homework.

## Relentless follow-up on vague answers

An answer is vague when you cannot write the config value or the constraint line from it.
"PARA, roughly" is vague. "Numbered PARA, but Areas is called Domains" is not.

When an answer is vague, follow up inside the same round, once, with a concrete proposal:
"I will write `areas: "30 - Domains"`. Correct?" A proposal is easier to correct than a
blank, and the correction is the real answer. Do not ask an open question twice; convert
it into a yes-or-no on your best guess.

Signals that need a follow-up:

- A source named without a scope ("ingest my email"). Which labels, which senders, what
  goes to the inbox folder when nothing matches.
- A mission that describes a tool, not an outcome ("a place for my notes"). Ask what
  question the vault should be able to answer a year from now.
- A constraint waived without a reason. The reason is the record; without it the waiver
  is indistinguishable from an accident.
- "Whatever you recommend" for the no-nudge question. That one has no default; a
  practice the user wants kept quiet is something only they know. Ask once more, offer
  "none" explicitly, and record the answer.

## Batching

Group questions by what they unlock, not by topic. Round 1 unlocks the folder table.
Round 2 unlocks the capability list and routing. Round 3 unlocks the schedule and the
guardrails. A question whose answer changes a later round's options belongs in the
earlier round.

Within a round, ask at most four questions, each with options and a stated default. Put
the question most likely to need a follow-up first so the follow-up fits inside the
round. Never ask a question whose answer is already in the diagnostic output; restate
what you found and ask only for confirmation if it matters.

## Forcing explicit tradeoffs

Some answers cost something the user may not see:

- Scheduled versus on-demand: scheduled means unattended writes into the vault every
  night; on-demand means the pipeline only runs when they remember. Name both costs.
- Append-only raw: costs disk and means corrections are new files; buys a rebuildable
  wiki. If they waive it, say what they lose in one sentence and ask once more.
- Summary-only transcripts: costs a live API call when a detail is needed; buys a vault
  that stays small and cheap to search.
- Numbered folder prefixes: costs a little ugliness in links; buys a stable sort order
  in every file browser.

State the tradeoff, recommend a side, and let them choose. Do not pick for them on
anything that touches their data, and do not pretend a choice is free when it is not.

## When to stop

Stop when every config key has a value, every capability is selected or blocked with a
reason, every constraint is accepted or waived with a reason, the no-nudge list is
written (possibly as "none"), and the scheduler is chosen. That is the exit condition,
not "the user seems satisfied" and not "three rounds are used up."

If the exit condition is met after two rounds, do not ask a third. If it is not met after
three, write the kickoff with the gaps marked `OPEN:` in the relevant section, tell the
user which they are, and let them fill them before execution. An open question in a
document beats a fourth round of questions in a chat.

Before executing, restate every decision in one table and get one confirmation. That
table is the last cheap place to be corrected.
