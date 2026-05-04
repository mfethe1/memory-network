# Context Hot-Load Manifest

A context manifest is a signed, expiring pointer list for starting or
restarting an agent session without loading broad memory files by default.
Slice 7A builds manifests locally and passively.

## Builder Pipeline

`ContextManifestBuilder` runs the M1 five-step pipeline:

1. `code_index doctor --json`
   - Abort with an error manifest if the index is stale or unhealthy.
2. `code_index impact <symbol> --json`
   - Create structural candidate pointers for target symbols.
3. `code_index tests <symbol> --json`
   - Create verification pointers for affected tests.
4. `code_index repo-map --format text --limit 50`
   - Create a compact orientation pointer.
5. Pointer store query
   - Add avoid and decision pointers, prune blocked/dead context, rank,
     budget, apply `ContextRetrievalPolicy`, and sign.

Tests inject a fake probe. The default probe shells out only when explicitly
used.

## Manifest Shape

Milestone 1 stores the signature as explicit fields on the manifest row, not as
a nested envelope. `signed_payload` is canonical JSON. `signature_key_id` names
the local key, and `signature` is the hex HMAC-SHA256 of `signed_payload`.
Verification parses the payload, checks the key ID, rejects expired manifests,
compares the HMAC with `compare_digest`, and confirms stored row fields match
the signed payload.

If `ManifestRequest.expires_at` is omitted, the builder assigns a conservative
30-minute expiry from build time before signing. This keeps default manifests
ergonomic while still making every signed manifest verifiable and expiring.

The canonical `signed_payload` contains:

```text
schema_version
status
host_id
repo_id
task_id
run_id
provider
route_scope
pointer_ids
required_pointer_ids
load_order
omitted_context
token_budget
source_hashes
peer_agent_states
expires_at
request_hash
```

The signature is a local HMAC in Milestone 1. It is sufficient for replay and
tamper tests, not a remote trust boundary.

## Budget Rules

Required pointers are selected first and are never silently dropped. If
required pointers exceed the configured budget, the builder returns an error
manifest instead of signing a partial manifest. Optional pointers are added in
rank order until the budget is reached; skipped pointers are recorded in
`omitted_context`.

All required IDs are still subject to sensitivity policy. A foreign
`host_private` or `provider_private` pointer is treated as unavailable for that
manifest request and yields a `missing_required_pointer` error instead of being
signed.

## Long Context Rules

These source kinds are not auto-loaded:

```text
soul
global_memory
raw_transcript
project_context
```

They can be included only when the manifest explicitly selects a section,
offset, or pointer, or when a required pointer ID names them. This keeps long
"soul" files, global memory, raw transcripts, and stale project context out of
default hot-load prompts.

## Idempotency

The builder hashes the manifest request and stores the generated manifest in
the local context store. Replaying the same request returns the same manifest.
Signed successful manifests replay idempotently. Error manifests, such as a
stale doctor result, are replaceable so the same request can recover after the
index is repaired. Handoff packets use the same pattern with a deterministic
packet hash.

## Fleet Context Graph

Before signing, the builder reads the active Fleet Context Graph snapshot
through the injected probe's `agent_states()` abstraction. In Milestone 1 this
only surfaces peer findings inside the manifest artifact; it does not let
agents query each other directly.
