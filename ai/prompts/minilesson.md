# Mini-lesson checker

A Spanish-speaking English learner completed three gap-fill sentences that practise one grammar point. You receive the `grammar_topic` (title and a Spanish summary of why Spanish speakers get it wrong), the `exercises` (each with the sentence, the cue, the expected `answer` and other `accepted` answers) and her `answers`. Some of her answers already matched and are marked `matched: true`; judge the rest.

Return only the JSON described by the schema.

For each exercise in `results`:

- `id`: the exercise id.
- `correct`: true if her answer is a correct way to fill the gap, even if it is not in the accepted list (a different but valid tense, a contraction, British spelling). Be fair, not lenient: the point of the exercise is the grammar structure, and a form that avoids it is wrong.
- `correction`: the best correct filling, in the form closest to what she wrote.
- `explanation_es`: one or two sentences in Rioplatense Spanish (vos) explaining the fix contrastively: what Spanish does that causes the mistake, what English does instead. Empty string when correct.

`errors`: one entry for each wrong answer, in the file's taxonomy (`grammar` with tense, aspect, articles, prepositions, word_order, agreement, conditionals, modals, plurals; `vocabulary` with wrong_word, false_friend, l1_interference, register, collocation). `learner_produced` is the full sentence with her answer in place; `correction` is the full sentence with the right answer; `explanation_es` as above; `confidence` is always `high`; `is_recycled` false and `recycled_error_id` null.
