# Security and privacy

## Where keys live

Resolution order (first hit wins):

1. Environment variable `OPENALEX_API_KEY` / `SEMANTIC_SCHOLAR_API_KEY`
2. User config `~/.config/citation-context/config.json`
3. Bundled `config.json` inside the skill (shipped empty)

Never put a real key in the bundled `config.json`. `doctor` warns if it finds one.

## What the tool does to protect keys

- Request URLs are logged with `api_key` replaced by `***`; `graph.json → meta.errors` is safe.
- Cache keys strip `api_key`; on startup, legacy rows containing `api_key=` are deleted.
- `export-dist` scans every exported file for the current keys, e-mail addresses and home paths;
  any hit returns exit code 4 and the package is not safe to publish.
- `doctor` prints keys masked (`usvc…Xv`).

## Publishing checklist

- [ ] `python scripts/citegraph.py doctor` — key not stored in the skill directory
- [ ] `python scripts/citegraph.py selftest` — passes offline
- [ ] `python scripts/citegraph.py export-dist --out dist/citation-context` — "密钥泄漏检查: 未发现"
- [ ] `.gitignore` covers `config.json`, `.cache/`, `runs/`, `__pycache__/`, `*.pyc`
- [ ] no terminal screenshots containing a home directory or e-mail
- [ ] repository starts from a clean history (no commit ever contained a key)
- [ ] rotate the OpenAlex key after publishing if it was ever stored in plain text elsewhere

## Rotating a key

<https://openalex.org/settings/api> → rotate; the old key dies immediately (or after a 24h/7d grace
window). Then update `~/.config/citation-context/config.json` or the environment variable.

## Keyless operation

`free_mode: true` (or `CITEGRAPH_FREE_MODE=1`, or `graph --free-mode`) uses single-entity lookups,
which OpenAlex bills at zero. It is slower (roughly 2–4 minutes for 50 references) but needs no key
and survives an exhausted shared IP budget.
