# Keep quoted context and required learning across handoffs

- Symptom: quoting a scheduled message lost its subject while the prep Yes path worked.
- Cause: scheduled sends have no interactive prepared-reply/run join and were excluded at ingress.
- Fix: freeze the exact accepted scheduled dispatch and project it as model context only.
- Boundary: keep route, payload, source revision, session and 15-minute checks; no new tool authority.
- Learning: essential-write failure plus failed retry admission previously left no recovery job.
- Fix: let existing parent work retry that combined failure; complete essential work before replacement.
- Proof: production journal/application and durable queue restart cases, with real-model reply checks.

`ownerWebReadReplyTarget` relies on `quotedReplyForEvent`. Do not broaden that shared helper to
admit background prose: a quoted injected link could then become permission to read it. The
separate context projection lets the model interpret the owner's question without granting a
new read or external action. Provider acceptance is required; unknown delivery stays held.

Required learning and optional ancillary learning differ. A valid brief may proceed when
essential work failed but its recovery was admitted. It may also proceed when only optional
learning admission failed. If neither essential completion nor durable recovery exists, retain
the artifact and let the original work item retry. Before recomposing into the same generation
folder, complete its essential phase under the existing generation lock. This preserves the old
inputs on failure without a new store or scanning task. It does not guarantee optional learning
for replaced generations or retries beyond the existing work deadline. A deterministic failure
in the old essential phase can withhold every replacement for that work item until its existing
deadline or attempt limit ends retries. The prior brief inputs remain intact; a fresh useful
replacement is not guaranteed that day. The receiver's existing terminal reporting still applies.

The implementation and acceptance report distinguish isolated production-code behavior,
actual-model replies and provider/device delivery. No new live acceptance is implied by local tests.
