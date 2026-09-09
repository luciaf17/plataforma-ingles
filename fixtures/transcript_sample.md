# Sample speaking transcript

Hand-written transcript of a 20-minute speaking lesson, used to test the analyzer.
Skill: speaking. Learner level: B1+. Track: work.
Topic: Walking an interviewer through a system's architecture.
Mini-lesson: present perfect with since / for.

Six errors are planted on purpose (see `PLANTED` in `ai/management/commands/analyze_sample.py`):

1. `I work here since 2021` — grammar / tense (present perfect with since)
2. `it depends of the client` — grammar / prepositions
3. `actually I'm working in a ERP` — vocabulary / false_friend
4. `the client don't trust the numbers` — grammar / agreement
5. `I will explain you the architecture` — grammar / word_order
6. `I have three years working with Django` — vocabulary / l1_interference (or grammar / tense)

Lines starting with `Tutor:` and `Learner:` are turns. Lines in `[brackets]` are phases.

---

[warm_up]
Tutor: Hi Lu! Good to see you. How has your week been so far?
Learner: Hi! It was okay, a bit busy. We have a, eh, a release on Thursday and the client change some things at the last minute, so... yeah. Busy.
Tutor: Last-minute changes, the classic. Did the release go out in the end?
Learner: Yes, we deploy on Thursday night and it was fine. Only one small bug on Friday.
Tutor: That sounds like a win. Let's warm up a bit more: what did you do this weekend to recover?
Learner: I went to Córdoba with my boyfriend, we stayed in a small town near the mountains. Very quiet, no internet. It was perfect.

[mini_lesson]
Tutor: Lovely. Today's mini-lesson is one I think you'll find useful for interviews: present perfect with "since" and "for". In Spanish you say "trabajo acá desde 2021", present tense. In English, if something started in the past and is still true, we use present perfect: "I've worked here since 2021." Or with a duration: "I've worked here for three years." Can you give me a sentence about yourself with "since"?
Learner: Okay. I have worked with Python since 2019.
Tutor: Perfect, exactly right. Now one with "for".
Learner: I have lived in my apartment for two years.
Tutor: Great. And one more about your current project, with "since" or "for".
Learner: Mmm. We have used Postgres since the beginning of the project.
Tutor: Excellent, all three correct. Keep that structure in mind, because I'm going to ask you about your experience during the practice.

[practice]
Tutor: Let's move into the interview. I'm the CTO, and I'd like you to walk me through the system you're building. Start with a bit of background: what's your role and how long have you been on it?
Learner: Sure. I'm a backend developer and I work here since 2021. Actually I'm working in a ERP for a company that sells materials for construction, and I have three years working with Django, so the stack is Django and Postgres.
Tutor: Got it. So you've been there since 2021, that's a solid stretch. Tell me about the ERP: what problem does it solve for them?
Learner: Before, they use Excel for everything, quotes, stock, invoices. The problem is that they lose information and two people can, eh, edit the same file. So the ERP centralize everything.
Tutor: Makes sense. I will play the sceptical CTO now: why build it instead of buying an off-the-shelf ERP?
Learner: Good question. It depends of the client. This client has very specific rules for the prices, they depend of the quantity and of the region, and the commercial ERPs don't handle that well. Also they wanted integration with WhatsApp.
Tutor: Interesting. Walk me through the architecture, then. How does a WhatsApp message become a quote?
Learner: Okay, I will explain you the architecture. The message arrive to a webhook, we put it in a queue, and a worker process it. The worker use an LLM to extract the products and quantities, but the price is calculated by a deterministic engine, not by the LLM.
Tutor: Why keep the LLM out of the pricing?
Learner: Because the client don't trust the numbers if the AI touch the price. We had a demo where the model invented a discount and, eh, that was it. So the pricing engine is pure Python with rules.
Tutor: That's a very good story for an interview, by the way. What about scale? What happens if a hundred messages arrive at the same time?
Learner: The queue handle it. We use Redis with, mmm, with a library, I don't remember the name now... the one that is like Celery but more simple. Anyway, the workers scale horizontally and each message is independent, so is not a problem.
Tutor: RQ, maybe? Doesn't matter. What was the hardest technical decision in this project?
Learner: I think the decision of the queue. At the beginning we process everything in the request, synchronous, and when the PDFs of the price lists were very big, the request timeout. So I moved the parsing to a background job. It was the correct decision but it took, like, two weeks.
Tutor: And if you could start over, what would you do differently?
Learner: I would add tests since the beginning. We have tests now but we added them late, and some bugs in production could be avoided with tests.

[drill]
Tutor: Quick round now, short answers. How long have you worked with Django?
Learner: I have worked with Django for three years.
Tutor: Good. Does the price depend on the region?
Learner: Yes, it depends on the region and the quantity.
Tutor: Better. Say the sentence again: "the client doesn't trust..."
Learner: The client doesn't trust the numbers if the AI touches the price.
Tutor: Excellent. One more: what are you working on right now?
Learner: Right now I'm working on the reports module.
Tutor: And "currently" or "actually"?
Learner: Currently. "Actually" is "en realidad".

[wrap_up]
Tutor: That's it for today. Two things that went really well: your explanation of why the pricing engine is deterministic was clear and confident, an interviewer would love it. And all three of your "since / for" sentences in the mini-lesson were perfect. One thing for tomorrow: "depend on", not "depend of". It came up twice. See you tomorrow!
Learner: Thank you! See you tomorrow.
