# OpenClaw Production Telegram Configuration

This runbook covers the repository-side production posture for the OpenClaw
Telegram control plane. It is limited to controller-side configuration,
Telegram room policy, and the expected OpenClaw bootstrap state.

## Required Posture

- Keep Telegram controller-side only. Do not add host-side bots, parsers,
  senders, or parallel Telegram control paths.
- Use one private Telegram supergroup for operator traffic. Do not use a
  broadcast channel.
- Require Telegram webhook delivery to terminate at the Railway controller
  endpoint: `POST /adapters/telegram/webhook`.
- Use a Telegram secret token so the controller can verify the
  `X-Telegram-Bot-Api-Secret-Token` header on every webhook request.
- Do not commit Telegram bot tokens, webhook secret tokens, chat IDs, Telegram
  user IDs, or NATS credentials to the repo.

The private supergroup requirement is not cosmetic. OpenClaw command promotion
links a Telegram sender's `platform_user_id` to a verified
`openclaw_external_identities` record with `command:write`. A broadcast channel
does not provide the per-sender identity needed for that policy.

## Controller Variables

Set these on the `openclaw-controller` Railway service only:

- `OPENCLAW_CONTROLLER_SIGNING_SECRET`
  Required. The controller uses this to sign command refs, and any Telegram
  bootstrap automation must reuse the same secret.
- `OPENCLAW_MESSAGING_DB_PATH`
  Required. Telegram room mappings, identity links, and adapter policy live in
  the controller messaging SQLite store at this path.
- `OPENCLAW_TELEGRAM_SECRET_TOKEN`
  Required for webhook production. Telegram must be configured with this exact
  value, and the controller rejects missing or mismatched secret headers.
- `OPENCLAW_TELEGRAM_BOT_TOKEN`
  Keep this controller-side only. It is needed for bot administration and the
  optional `POST /adapters/telegram/poll` path, even though the webhook handler
  itself validates only the secret header.

Related controller variables such as `OPENCLAW_CONTROLLER_DB_PATH`,
`OPENCLAW_CONTEXT_STORE_PATH`, and `OPENCLAW_NATS_URL` still follow the normal
Railway service contract in
[railway-services.md](/home/agent/workspace/docs/openclaw/railway-services.md:1).

## Production Runbook

1. Create a private Telegram supergroup for operators and add the production
   bot to that supergroup.
2. Keep operator messages attributable to individual Telegram accounts. Do not
   rely on anonymous or channel-style posting for commands that need
   `command:write`.
3. Configure the Railway controller with the controller signing secret,
   messaging DB path, bot token, and Telegram secret token. Do not place the
   bot token or secret token on OpenClaw hosts.
4. Register the bot webhook out-of-band against the Railway controller URL:

```text
https://<controller-public-domain>/adapters/telegram/webhook
```

The secret token registered with Telegram must exactly match
`OPENCLAW_TELEGRAM_SECRET_TOKEN`. Telegram then echoes that value in the
`X-Telegram-Bot-Api-Secret-Token` header, which the controller verifies before
identity lookup or command promotion.

5. Bootstrap the OpenClaw messaging policy in the controller messaging DB.
   The expected production state is:

- The built-in `telegram` adapter exists and has `command_promotion_enabled = true`.
- The target OpenClaw room is a controller-managed `fleet` or `task` room.
- The Telegram supergroup chat ID is mapped into
  `openclaw_platform_room_mappings` for adapter `telegram`.
- The route policy allows only the command types and targets intended for the
  operator room. The current production-safe baseline is:

```json
{
  "command_promotion": {
    "enabled": true,
    "allowed_command_types": ["assign_task"],
    "allowed_target_kinds": ["task"]
  }
}
```

- Each operator who may issue Telegram commands has a verified
  `openclaw_external_identities` row for adapter `telegram` with at least
  `message:write` and `command:write`.
- The mapped room metadata already contains the assignment context required by
  the Fleet Controller for task creation and host routing.

6. Record chat IDs and Telegram user IDs only in the operator bootstrap inputs
   or the controller messaging DB. Do not place them in docs, examples, tests,
   or committed config.

## Automation Status

`scripts/bootstrap_openclaw_telegram.py` bootstraps the controller-side room
mapping, adapter policy, and trusted Telegram operator identity links. Run it
only against the controller messaging DB path and reuse the existing controller
signing secret.

Do not add ad-hoc host-side bootstrap logic or create a second signing secret
for Telegram bootstrap.

That keeps Telegram room policy, identity links, and signed command behavior on
the same controller-owned trust boundary as the live webhook path.
