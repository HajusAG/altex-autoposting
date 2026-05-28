# altex-autoposting

Autoposting script for Altex.ro marketplace. Loops over Odoo `x_auto_posting` tasks
for `website_id=194` (HAJUS AG) in stage "New task", fetches product data from
Odoo, picks attribute_set/category/attributes via OpenRouter LLM, signs and
POSTs to Altex.ro via HMAC-SHA512, verifies via GET, then moves the task to
stage "Pending" in Odoo.

## Env vars (Coolify)

| name | required | default |
|---|---|---|
| `ALTEX_PUB` | yes | — |
| `ALTEX_PRIV` | yes | — |
| `ODOO_DB` | yes | — |
| `ODOO_API_KEY` | yes | — |
| `OPENROUTER_API_KEY` | yes | — |
| `ALTEX_BASE` | no | `https://marketplace.altex.ro` |
| `ODOO_URL` | no | `https://odoo.boni.tools` |
| `ODOO_UID` | no | `2` |
| `OPENROUTER_MODEL` | no | `deepseek/deepseek-chat` |
| `N8N_AWS_START_URL` | no | `https://n8np2.boni.tools/webhook/get-aws-images-start` |
| `N8N_AWS_RESULT_URL` | no | `https://n8np2.boni.tools/webhook/get-aws-images-result` |
| `ALTEX_PENDING_WEBSITE_ID` | no | `194` |
| `ALTEX_TASK_STAGE_NEW` | no | `1` |
| `ALTEX_TASK_STAGE_PENDING` | no | `6` |
| `ALTEX_PRICELIST_RON` | no | `21` |
| `ALTEX_PENDING_LIMIT` | no | `0` (all) |

## Run

```
docker build -t altex-autoposting .
docker run --rm -e ALTEX_PUB=... -e ALTEX_PRIV=... ...etc altex-autoposting
```

## Static data

The repo bundles three JSON files (regenerated periodically from Altex API):

- `_allowed_categories.json` — list of attributeSet/category pairs that our
  seller account is allowed to post into (939 entries).
- `_allowed_map.json` — index `attributeSet_id -> [{codes, path}]`.
- `_altex_sets.json` — `id -> {id, name}` for all attribute sets.
