# Uncertain delivery, interrupt and recovery

Timeout, lost reply and connection failure do not prove input was rejected.
Inspect the original request's status, journal association and native session
before retrying. Reuse its identity. Do not replay start, submit, recover or an
unknown webhook delivery. For an uncertain monitor, inspect the original monitor
identity before another launch. `reconcile --request-id REQUEST_ID` resolves
interrupted verification state; it does not replay checks or business input.

To stop a running round while retaining its session, when authorized:

```sh
herdr-bridge --config /private/project.json interrupt ROUND_ID --request-id REQUEST_ID --actor ACTOR --note REASON
herdr-bridge --config /private/project.json status --request-id REQUEST_ID --observe --wait 10
```

Confirm the original round is interrupted and its native identity is retained
before a parent-linked continuation. Interrupt is not acceptance or close.

On process loss, inspect `recovery-status --request-id REQUEST_ID`. Review its
code/session/terminal fingerprint and the recovery shell against the original
run. With authority to resume that same task:

```sh
herdr-bridge --config /private/project.json recover --request-id REQUEST_ID --fingerprint FINGERPRINT --actor ACTOR --review REVIEW
```

Re-observe the original request and confirm startup in the same Pi session or
Codex thread.
Unknown recovery must be reconciled there; do not resend the resume command.
Changed code, session identity or permission/trust prompts require human review.
Recovery continues execution under the original request. Source PROJECT.md gives
the current recovery contract. Use CLI help for exact parameters.
