# Human takeover

For a development task, `takeover --request-id REQUEST_ID --actor ACTOR` transfers
input ownership to the named human. Stop all automatic input immediately; use
the same App/hc terminal and native session for inspection and control. Do not
start another Agent or approve a permission/trust dialog on the human's behalf.

Before restoring automation, read the latest task/result and
`reconcile --request-id REQUEST_ID`, review code changes and current session,
and retain the returned fingerprint. Only after the authorized review:

```sh
herdr-bridge --config /private/project.json resume --request-id REQUEST_ID --fingerprint FINGERPRINT --review REVIEW
```

Confirm control has returned to automation and input is ready. Continue with a
new round and the existing parent. Rerun independent verification after code or
native evidence changes. Keep explicit accept/reject/terminate decisions tied to
the named actor; a takeover or a passing check alone is not user acceptance.
See source PROJECT.md and DEVELOPMENT.md for App binding and evidence semantics.
