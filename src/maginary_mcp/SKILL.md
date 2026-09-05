---
name: maginary-image-gen
description: Use when generating or editing images/video with Maginary — building `--flag` prompts, choosing a model (e.g. --flagship vs --nb2 vs --sora), or running/polling a generation through the Maginary MCP or REST API. Covers the prompt DSL, model selection, and the async generate→poll flow.
---

# Maginary image & video generation

Maginary is a Midjourney-style image + video generator with a `--flag` prompt DSL and an async HTTP API. This skill teaches the mental model; the **authoritative, always-current flag list lives in the catalog** — if the `maginary` MCP is connected, call `search_parameters` / `get_parameter` before relying on any flag below, since the catalog can change.

## The prompt is: subject text + `--flags`

```
a fox in autumn foliage --ar 16:9 --flagship --4k
```

Flags go **after** the subject. Values follow the flag (`--ar 16:9`, `--seed 42`). Boolean flags stand alone (`--flagship`, `--mp4`).

## Flags that matter most (verify against the catalog)

- **Composition:** `--ar <w:h>` aspect ratio; `--<n>` output count (`--2` = 2 images).
- **Quality:** `--2k`, `--4k` (pair with `--ar`). Default is standard res.
- **Determinism:** `--seed <int>` for repeatable results.
- **Format:** `--png`, `--jpg`, `--webp`, `--svg` (svg for logos/marks), `--transparent`.
- **References:** `--sref <url>` style reference, `--sw <0-1000>` its weight.
- **Negative:** `--no <things>` — but this is `mostly-dead`; prefer describing what you want.
- **Video:** `--mp4` turns it into video; then `--5s`/`--10sec` duration, `--1080p` resolution, `--24fps`.

## Choosing a model (the `--model` triggers)

Default (no model flag) is a solid general image model. Force one when the job calls for it:

- `--flagship` — highest-quality general image work (brand/ad/hero shots).
- `--nanobananapro` — product mockups / clean commercial renders.
- `--nb2`, `--gpt2` / `--gpt2high` — alternate image models; try when the default's aesthetic isn't landing.
- **Video:** `--sora` / `--sora2pro` (cinematic), `--soralite` (faster/cheaper), `--seedance2` / `--seedance2pro`. Video models require `--mp4`.

Cost scales with model + quality + video duration. If the user is cost-sensitive, prefer the default image model and skip `--4k`; reserve `--flagship`/`--sora2pro` for when quality is the priority.

## The generation flow (async)

1. **`generate(prompt)`** → returns a record with `uuid`, `action_type`, `expected_output_count`, `processing_state`. It does **not** block.
2. **`wait_for_generation(uuid)`** (or a webhook `callback_url`) → polls to `done`/`failed`. On `done`, `image_urls[]` holds the outputs.
3. From an upscaled image you can follow up: upscale, vary (`--vary` / `--varysubtle` / `--varystrong`), zoom out (`--zoomout`), or pan (`--panleft/right/up/down`).

## No MCP? Use the REST API directly

Same flow over plain HTTP — base `https://app.maginary.ai/api`, auth
`Authorization: Bearer <key>` (create keys at https://app.maginary.ai/dashboard#api-keys):

```bash
# 1. kick off (async — returns immediately with a uuid)
curl -X POST https://app.maginary.ai/api/gens/ \
  -H "Authorization: Bearer $MAGINARY_API_KEY" -H "Content-Type: application/json" \
  -d '{"prompt": "a fox in autumn foliage --ar 16:9"}'
# 2. poll until processing_state is 'done' (outputs in image_urls[]) or 'failed'
curl https://app.maginary.ai/api/gens/<uuid>/ -H "Authorization: Bearer $MAGINARY_API_KEY"
```

`POST /gens/` also accepts `callback_url` for webhook delivery instead of polling.
HTTP 402 = out of credits (body carries a `billing_url` and, for agents, an x402
top-up challenge). The flag catalog is public JSON at
https://maginary.ai/docs/parameters.json — the REST equivalent of `search_parameters`.

## Working rules

- **Look up flags, don't invent them.** Use `search_parameters`/`get_parameter`. Flags marked `dead`, `mostly-dead`, or `unimplemented` won't behave as expected — avoid them.
- **One concept per generation.** For variations, generate then `vary`, rather than cramming everything into one prompt.
- **Video needs `--mp4`** plus a video model; don't add video flags to an image prompt.
- **A `timeout` result is not a failure.** `wait_for_generation` returns after ~45s to stay under MCP client limits; the generation is still running — just call it again (video can take a few rounds).
- **Surface errors verbatim.** If `generate` returns an auth/credit error, show it — don't retry blindly or fabricate a result.
- **Credits:** generations cost credits. If the account is out and you're an agent using an API key, the API may respond with an x402 top-up challenge (HTTP 402) — that's the paid-top-up path, not an error.
