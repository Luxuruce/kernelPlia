<div align="center">

# kernelPlia

**A clause-level regulatory Q&A kernel. It is built not to "sound right", but to stay silent when it can't back an answer.**

<sub>V0 live · 263 clause units · 0 invented clause numbers · ~¥0.004 per question</sub>

**English** · [简体中文](README_ZH.md)

[![Live demo](https://img.shields.io/badge/Live_demo-kernelplia.vercel.app-000?logo=vercel)](https://kernelplia.vercel.app)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

[Live demo](https://kernelplia.vercel.app) · [Quick start](#quick-start) · [Design](#three-uncommon-design-choices) · [Numbers](#results) · [My role](#my-role-and-product-decisions) · [Scope](#whats-not-in-this-repo)

<!-- TODO 截图到位后取消注释 / uncomment once the screenshot is added: replace with a hero screenshot or GIF (question → clause-level citations → highlighted source page)
<img src="docs/images/hero.png" width="860" alt="kernelPlia answers with clause-level citations and highlights the source lines in the Official Journal">
-->

</div>

---

## The problem

Exporters to the EU face several regulations at once: PPWR, the Batteries Regulation, CBAM, EUDR, REACH / RoHS. The same four problems keep coming up: **the same facts are re-entered for every form, conclusions get hard-coded as attributes, the law hides inside prompts, and answers can't point to their source.**

kernelPlia answers one question: **what does a given regime require, at a given time, of a given party, for a given object, and which provision says so.** You ask in plain language. It gives a conclusion and pins every statement to a specific article, paragraph and point. Click a citation to open the Official Journal page with the cited lines highlighted.

The first corpus is the EU Packaging and Packaging Waste Regulation (PPWR, Regulation (EU) 2025/40). The demo UI is in Chinese.

## Try it

Open **[kernelplia.vercel.app](https://kernelplia.vercel.app)** and ask, for example:

```text
包装里重金属最多能有多少？   (What is the maximum heavy-metal content allowed in packaging?)
```

> The live demo allows 5 questions per IP per day.

<!-- TODO 截图到位后取消注释 / uncomment once screenshots are added
<details>
<summary><b>More screenshots</b></summary>

| Clause citations + source highlight | Conflict surfaced and blocked | Clause relation graph |
|---|---|---|
| <img src="docs/images/citation.png" width="280" alt="Citations"> | <img src="docs/images/conflict.png" width="280" alt="Conflict block"> | <img src="docs/images/graph.png" width="280" alt="Clause graph"> |

</details>
-->

---

## How it works

```
question
   │
   ├─► exact clause-number lookup
   ├─► topic-term recall
   ├─► one-hop link expansion
   ▼
conflict check
   │
   ├── hit ──► surface and block (no model call)
   │
   └── miss ─► context assembly (10K / 15K budget gate)
                  │
                  ▼
               structured output + 6 server-side checks
                  │
                  ▼
               clause-level citations · authority ranking · source-page highlight
```

Retrieval relies mainly on a structured clause index, with vector search as a fallback. Every path resolves to the same set of clause anchors. **Applicability, authority conflicts and effective dates are computed by rules, never decided by similarity scores.**

---

## Three uncommon design choices

| Choice | In one line | Why |
|---|---|---|
| **No citation, no answer** | `citations` is a required schema field, backed by 6 server-side checks | Enforced by the schema, not requested in a prompt |
| **Conflicts are surfaced, not resolved** | When official documents disagree, show both sources and their authority first | Never silently pick a side for the user |
| **Trigger decoupled from recall** | Blocking depends only on the trigger clause's own score, with no idf | A red line must not drift as the index grows |

<details>
<summary><b>1. "No citation, no answer" is a schema guarantee</b></summary>

`citations` is a **required field** in the answer object, enforced at generation time by structured output (`json_schema` + strict).
This is different from "JSON mode", which only guarantees valid JSON, not the fields, required keys or enums.

Models can still invent clause numbers, so six server-side checks follow, each tied to a specific red line:

| | Check |
|---|---|
| 1 | Knowledge-mode answer with no citation → **not shown**; fallback text instead |
| 2 | Cited clause IDs must **exist in the index and be formally adopted**. Invented ones are dropped; if all are invented, nothing is shown |
| 3 | Citations are re-sorted by **legal authority**, taken from the index. A model labelling an FAQ as rank 1 doesn't count |
| 4 | Ranks 3 and 4 get a server-added note: "Commission interpretation, not legally binding" |
| 5 | Claiming "not covered by official documents" without reasoning → not shown |
| 6 | A known conflict is hit → no answer body |

**Better no answer than an unsupported one.**

</details>

<details>
<summary><b>2. Conflicts are surfaced and blocked, not auto-resolved</b></summary>

Official documents contradict each other. For example, an implementing decision exempts a category of packaging from reuse obligations, while official guidance published three months later doesn't mention the exemption in any of three relevant sections.

Most systems pick one answer. This kernel **returns no answer body**. It first shows both sources, their authority ranks, and which side the ranking favours. The user continues only after acknowledging this. The answer then still follows the authority ranking; **there is no "answer by the other source" option**.

The blocking turn **makes no model call**, so it costs almost nothing.

</details>

<details>
<summary><b>3. Triggering is decoupled from recall</b></summary>

Whether to block depends only on the **trigger clause's own** "self-sufficiency score" for the question, regardless of whether that clause made it into the recall set:

```
block ⟺ max(self-score over trigger_clause_ids) ≥ threshold
        ∧ (context terms empty ∨ question hits any of them)
```

The score is `max(len(term) ** 1.2 × field weight)`, with **no idf**.

Why no idf: tied to top-N or document frequency, the same question would sometimes block and sometimes not as the index grows and recall competition changes. Blocking is a red-line guarantee and **must not drift with index size**. High-frequency noise is suppressed instead by a hand-maintained stopword list (`demo_index/stopwords.yaml`) that is formally adopted and regression-tested on every change. Its values only change when someone edits that file.

</details>

<details>
<summary><b>Lessons learned: tokenisation, trimming order, baselines</b></summary>

**Tokenisation.** Chinese text used to be split into 2–6 character sliding n-grams. The question "我的包装重金属超标了会怎样？" (*What happens if my packaging exceeds heavy-metal limits?*) produced `包装重`, which crosses a word boundary in the question (包装|重金属, packaging|heavy metal) and a different one in an unrelated clause title about reuse (包装|重复, packaging|reuse). **Neither is a real word**, but the characters match, so the unrelated clause got past the conflict gate. Its conflict-linked clauses then pushed the top-ranked limit clause out of the context. **Asked about heavy metals, answered about reuse.** The fix: segment words with jieba first, then slide over the word sequence, joining only adjacent words and breaking at function words.

**Trimming order.** Over budget, items are dropped by "would losing this cause a wrong answer", not by "was this added by expansion". Tiers alone can't tell "derived item and its source are both present" from "the source was already dropped", so two rules were added: a derived item's sort key **follows its source's key** (so within a tier the source always comes first), and a rank 3/4 interpretation plus the rank 1/2 provision it expanded to form **one unit that can't be split when trimming**. Changing only the order would leave an interpretation without its legal basis whenever the cut falls between them. Adding only the binding wouldn't fix the inverted order.

**Baselines must not absorb total failures.** A baseline table of "known acceptable losses" has a trap: a test case that loses every expected citation can still be recorded, so the suite stays green while that case is completely broken. A separate hard limit ignores the baseline and fails directly: **no single case may lose all of its expected citations**.

</details>

---

## Results

Figures below are from the live version, which uses a private full index. This repo contains only a demo slice.

| Area | Result |
|---|---|
| Corpus | **263** PPWR clause units, each formally adopted by name; 232 rendered source pages |
| Recall | On 15 golden cases, recall misses **12 → 3** (~94% expected-citation recall); false blocks **1 → 0** |
| Invented clause numbers | **0** from the candidate models under the real nested schema |
| Latency and cost | ~**11.5 s** end to end, ~**¥0.004** (≈ US$0.0006) per question |
| Tests | **191** automated tests in the live version; **35** offline tests here (no model needed) |
| Operations | Per-IP daily limit; monthly budget alert at 70%, degrade at 100%; model fallback lite → pro → clause list only |

---

## My role and product decisions

I'm the product owner. I designed the full product, made all product calls, and took V0 to production. Some specific decisions:

- **Defined the red lines.** The design spec went through v1.9, with 23 recorded decisions and 15 red lines. "No citation, no answer" and "block on conflict" come from there.
- **Caught a metric trap.** One model configuration met the time-to-first-byte target (1.4 s) but took 245 s in total. I changed acceptance to a dual metric (first byte + total time), compared 4 configurations, and chose the lite model with deep thinking turned off.
- **Fixed recall by changing mechanisms, not thresholds.** A 54-combination parameter sweep showed the remaining misses were structural, so I changed the mechanism (bidirectional link expansion, stopwording the corpus's own name) instead of tuning thresholds.
- **Found the tokenisation bug by hand.** The "asked about heavy metals, answered about reuse" bug above came from my own testing.
- **Kept the Q&A real.** I rejected a "preset questions" demo. The home-page graph lights up only a third of the nodes; a node reveals its name only after it has been cited in an answer.
- **North-star metric:** expert minutes spent per deliverable.

<details>
<summary><b>Full product design and roadmap (V0 live; V1 / V2 not built)</b></summary>

**Two layers, six modules**

| Layer | Modules |
|---|---|
| Knowledge | K1 corpus & rights registry · K2 structural index & normative assertions · K3 retrieval & Q&A |
| Determination & evidence | J1 applicability · J2 gaps & data requests · J3 deliverables & ledger |

The kernel connects to the company's fact base only through one-way fact snapshots: **facts are shared, conclusions are isolated**.

- **Temporal model**: floating effective dates are modelled as solvable expressions; recurring obligations (such as two-tier EPR filing deadlines) are modelled separately; normative assertions are bitemporal, storing only the computation trail and never caching conclusions.
- **Gap analysis**: gaps are computed across 6 states (satisfied, missing, unknown, conflicting, unobtainable, rights-blocked); data requests cite PPWR Art. 16(1) as their legal basis.
- **Deliverables**: one determination projects into 4 deliverables: technical documentation & DoC, EPR filing quantities, platform questionnaires, customer questionnaires.

**Roadmap**: V0 clause-level Q&A → V1 obligation calendar + quoting → V2 technical documentation & DoC → multi-regime kernel.

</details>

---

## Quick start

```bash
docker compose up -d                      # PostgreSQL 16
pip install -r requirements.txt
python scripts/init_db.py
python -m ingest.load                     # load demo_index/
pytest -q                                 # 35 tests, no model needed
uvicorn app.api:app --reload              # http://127.0.0.1:8000
```

Q&A needs a model. Set `ARK_API_KEY` in `.env` (Volcano Engine Ark, OpenAI-compatible API), or edit `app/providers.py` to use another provider. **Before switching, make sure the model supports equivalent strict structured output.** "JSON mode" is not enough; without strict output the first red line degrades from a schema guarantee to retry-and-discard.

<details>
<summary><b>Diagnostics: why were these clauses recalled?</b></summary>

```bash
python scripts/why.py "包装里重金属最多能有多少？"
```

It prints the candidate terms, where each recalled clause came from, each of the three conflicts' self-scores and **which term** contributed, and the order in which context was kept or dropped. Both attribution bugs so far were found with it.

</details>

<details>
<summary><b>The home-page graph</b></summary>

The animated graph on the home page **draws the real clause relations**: nodes are adopted clause units, edges are registered `derogates` / `explains` relations, and the pulses along the edges show one-hop expansion. By default it shows only authority rank and topology, without clause numbers. **A node gets its name and becomes clickable only after it has appeared as a citation in this session.**

</details>

<details>
<summary><b>Project layout</b></summary>

```
app/          FastAPI app: retrieval, context assembly, answer checks, rate limits
ingest/       index loader
demo_index/   demo slice of 10 clause units + conflicts, links, stopwords, golden cases
scripts/      setup, diagnostics (why.py), model gate tests
tools/        demo slice export
web/          single-page front end
tests/        35 offline tests
```

</details>

---

## What's not in this repo

**The index.** This repo ships a demo slice of 10 clause units (`demo_index/`), enough to exercise every mechanism above. The full index, working translations, internal answering rules, the full test set and the definition of a "correct answer" are not included.

That's deliberate: **the mechanisms and discipline are public; the content judgement, where most of the work went, is not.** The two can be separated, so the mechanisms can be open.

**Out of scope by design**: picking a side in a conflict automatically, "common-sense" answers without citations, legal advice.

## License

Code is MIT. English provisions in `demo_index/` are quoted from the Official Journal of the EU (already public). Chinese headings are working translations with **no legal effect; the English original prevails**.

---

<div align="center">

By [@Luxuruce](https://github.com/Luxuruce) · B2B AI agent products

</div>
