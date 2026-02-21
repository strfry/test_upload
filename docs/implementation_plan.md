# Implementation Plan

## Objective
Document the refactor that turns Scambaiter into a transport-agnostic, CLI-driven core with HF/OpenAI fallback, alongside the supporting architecture updates (card cache, log stores, Bot API cleanup, feedback hooks) that external clients and monitoring layers rely on.

## Components & Steps

1. **Core CLI commands**
   - Implement a CLI entrypoint (e.g., `scam_baiter.py` or new module) exposing `analyze`, `prompt-builder`, `suggest`, and `executor`. Each command consumes structured JSON payloads (sender, channel, conversation ID, history, metadata) and emits JSON results (analysis, prompts, suggestions, provider metadata).
   - `analyze` produces risk/tone/CTA tags plus sanitized summaries.
   - `prompt-builder` merges whatever template exists under `prompts/<channel>/<conv>.json` with history and analysis tags, outputs prompt text and recorded prompt version for auditing.
   - `suggest` posts the constructed prompt to HuggingFace router (`https://router.huggingface.co/hf-inference/models/{model}`) using `HF_TOKEN`; on router failure it falls back to `https://api-inference.huggingface.co/models/{model}` or a configurable OpenAI-compatible URI when `--provider openai` is specified. It returns `text`, provider name, tokens, and fallback hints (for monitoring).
   - `executor` decides whether to auto-send or queue a draft, recording decision metadata in a feedback store (e.g., `feedback` table or log).

2. **Persistence & caching**
   - Extend `AnalysisStore` with `image_entries` (chat_id, cache_key, caption, optional file_id, updated_at) and `profile_photos` tables plus helper APIs (`image_entry_get`, `image_entry_upsert`, `list_image_entries`, `has_image_entries`, `profile_photo_get/set`).
   - Maintain `logs/conversations/<conv>.log` with inbound/outbound events, provider info, tokens, and success/error metadata for monitoring and auditing.

3. **Bot API & card registry**
   - Add `card_registry` state in `bot_api.py` so every posted card (control menu, infobox, picture group, prompts) registers its `message_id`s, enabling `_cleanup_card_messages` to delete them reliably and log any failures.
   - Update the “🖼️ Bilder” button flow to read from `image_entries`, only upload missing cards, normalize captions (`[Picture Card]` removed), register every message, and allow “Löschen” to call `_cleanup_card_messages`.
   - Cache profile photos from scans and card posts, rendering user cards purely from stored Telegram `file_id`s to avoid repeated uploads.

4. **Architecture doc & plan sync**
   - Update `docs/architecture.md` (already reflects much of this) to explicitly describe the CLI commands, HF/OpenAI fallback providers, prompt templates (`prompts/<channel>/<conv>.json`), `card_registry`, `image_entries`, `profile_photos`, Bot API monitoring role, logging directories, and policy/backlog references (Anti-Loop, Konkretheit, prompt_cases).
   - Cross-reference `docs/backlog.md`, `docs/prompt_cases`, `docs/snippets`, and `docs/event_schema_draft.md` so readers understand policy/test input sources.
   - Mention that the UI is an external Bot API client providing advanced monitoring and interacts with Scambaiter via the new CLI commands/responses.

5. **Monitoring & feedback**
   - Ensure every CLI invocation logs provider/tokens and stores audit info either via `logs/conversations` or a feedback table to resurface in dashboards.  
   - Track when suggestions are actually sent (executor) so the external monitoring layer can adjust prompt weights or training dumps.

## Testing & Validation

- CLI unit and integration coverage for each command, including provider fallback and prompt template merging.  
- Image-cache flow tests ensuring `image_entries` and `card_registry` stay in sync and cleanup works.  
- Manual Bot API run verifying “🖼️ Bilder” button uses cached images and profile photos do not trigger new uploads.  
- End-to-end run with external UI simulation writing payload JSON and invoking the CLI chain, verifying logs, suggestions, and feedback entries.

## Assumptions

- Default `suggest` host is `https://router.huggingface.co` (HF router) and `HF_TOKEN` is set (e.g., `deepseek-ai/DeepSeek-R1` for development).  
- External UI/monitoring client communicates via the Bot API, not a new transport library, and uses structured JSON outputs for dashboards.  
- Prompt templates under `prompts/<channel>/<conv>.json` are maintained manually; prompt-builder records the concatenated prompt text/version for audits.
