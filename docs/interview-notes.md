# Interview prep — one-sentence answers

Not part of the submitted PDF. Expected pushbacks and the short answer for each.

**Why Temporal instead of Celery or a plain queue?**
The two hardest requirements — rerun one failed stage without restarting the job, and pause at a human step for days then resume — are Temporal's native primitives (activity retries over durable history, and signals), so with a queue I would be building and testing both myself.

**Isn't the human-queue math a deal-breaker (1,700 person-hours)?**
It is the honest cost of compliance, and the design attacks it from three sides: route ≥ 80% of listing tasks to API/aggregator channels where no human is needed, pre-package every human task so the person spends 2 minutes not 10, and track queue metrics from week 1 so staffing is a forecast, not a surprise.

**What if a cPanel host blocks API uploads (no SSH, UAPI disabled, WAF on :2083)?**
The capability probe detects it before any write, the job stops in `awaiting_approval` with a human task, and the fallback is a signed release bundle the client (or we, with their permission) installs through File Manager — the two-rename swap and rollback still work because they are just directory renames.

**How do you know the LLM didn't invent facts?**
Every factual sentence must cite a span from the crawl artifact or it is dropped before build; the golden set catches regressions when prompts or models change.

**Why not let the LLM pick the design freely? Wouldn't that be more unique?**
Free-form output can't be checked; a structured design object (palette, font pair, layout DNA, logo) can be measured against every previous build with one indexed query, and the combinatorial space is > 10¹³ so constraint costs nothing in variety.

**Why download every file to verify the upload? That's slow.**
On SSH hosts it's one `sha256sum`; on UAPI-only hosts the download loop is the only honest check, it runs before anything touches `public_html`, and a 15-page site is a few hundred KB — seconds, not minutes.

**What happens if the second rename in the swap fails?**
The deployer immediately renames the previous directory back into place; the test suite covers a rename failure during rollback too, which ends in a loud `rollback_failed` state with the Blob backup URI in the alert.

**How is Part 2 compliant with Google / Yelp / Apple terms?**
Each directory is accessed only through a channel its terms permit — official API with owner-granted access, an approved listing partner, or a human doing the human-intended step in a real browser — and the no-circumvention rule (no solvers, no proxies, no fingerprinting) is enforced in code review.

**What's the single biggest risk?**
Partner-programme approval timing: GBP, Yelp and Apple access all depend on third parties, so applications go in during week 1 and the aggregator channel keeps the system useful in the meantime.

**What would you cut if you had half the time?**
Ship Part 1 with mandatory human approval on every deploy and Part 2 with GBP + Bing import + aggregator only; the browser-assisted connector is the most engineering per listing and the last thing to build.
