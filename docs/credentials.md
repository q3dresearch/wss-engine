# Credentials

Most sources need no authentication. When one does, the rule is simple:
**the registry names an environment variable; it never holds the value.**

```yaml
# registry/openrouter.classifications.task.yml
auth:
  bearer_env: WSS_OPENROUTER_KEY    # a NAME, never a key
endpoints:
  - url: "https://openrouter.ai/api/v1/classifications/task?window=7d"
```

That sends `Authorization: Bearer <value of $WSS_OPENROUTER_KEY>`.

## Treat every API key as a payment credential

Many "read-only" data APIs are gated by the same key that authorises paid
usage — OpenRouter's Data API takes the key you use for inference. A leaked
key is a bill, not just a privacy problem. Assume the worst unless the
publisher explicitly issues read-only keys, and where you can:

- issue a **separate key** for capture rather than reusing a personal one,
- set a **spend limit** on it if the publisher supports per-key limits,
- rotate it if it is ever printed, pasted, or committed.

## Where the value lives

**Locally:** `<repo root>/.env.local`, which the scaffolded `.gitignore`
excludes. Copy the committed `.env.example` and fill it in:

```bash
cp .env.example .env.local
$EDITOR .env.local        # never `git add` this file
```

The CLI loads it automatically from the data root. **Real environment
variables always win**, so a stale file in a checkout can never shadow a CI
secret.

**In CI:** a repository secret (Settings → Secrets and variables → Actions),
plus one line in the capture workflow's `env:` block:

```yaml
env:
  WSS_CONTACT: ${{ secrets.WSS_CONTACT }}
  WSS_OPENROUTER_KEY: ${{ secrets.WSS_OPENROUTER_KEY }}
```

This is the **one** workflow edit a new *credential* requires. Adding a
source still never needs one — the cost is per publisher-with-auth, not per
source.

## What the engine guarantees

- The credential is sent as a **request header**. Capture archives response
  bodies, never requests, so it cannot reach `raw/`.
- The manifest records the URL, status and hashes — never headers.
- `doctor` prints the variable *name* and the words "value never printed",
  never the value.
- A test asserts that after an authenticated capture, **no file written
  anywhere under the data root contains the secret**.

## What the engine refuses

`wss validate` rejects a URL carrying a credential in a query string
(`?api_key=…`, `?token=…`). The manifest records every URL verbatim and
permanently, so a key in a query string is a permanent, committed leak. Use
`auth:` instead.

It also rejects an `auth.bearer_env` value that is not a plain environment
variable name, which catches the obvious mistake of pasting the key itself
into the registry file.

## When a key is missing

The source fails with `outcome: error`, `reason: missing_credential`, and the
run exits non-zero — loud, but **localised**: other sources in the same shard
still capture. Health counts it as a failure, so a permanently missing
credential eventually auto-disables that source and opens an issue.

## If a key leaks anyway

Rotate it at the publisher first — that is the only step that actually
matters. Rewriting git history reduces exposure but never guarantees removal:
on GitHub, dangling commits stay reachable by SHA until garbage collection.
Assume anything pushed publicly, even briefly, is compromised.
