# AI helper audit — November 7, 2026

Implemented and verified fixes:

1. Follow-ups save the complete original draft and reference text with their task. They do not depend on the draft staying among recent channel messages.
2. An AI failure does not create a follow-up. Task limits produce an explicit scheduling failure. An undelivered original draft leaves its follow-up paused.
3. Tasks complete only after successful Discord delivery. AI/network failures retain the task, retry with backoff, and pause after five failures. Task listings show the error; resume resets the retry count.
4. Long output saves unsent-chunk progress across restarts. Concurrent scheduler/manual executions are guarded. Cancellation during generation prevents delivery.
5. The running scheduler dispatches tasks independently; slow AI requests do not block reminder delivery. Shutdown cancels and awaits dispatched work.
6. Scheduling requires a direct request. Negative requests, quotations, ordinary questions, and tested ambiguous frequency phrases do not create tasks. Unsupported times and invalid cancellation requests receive explicit explanations.
7. Natural requests and `/ask` share real scheduling logic. Recurring reminders use direct reminder text rather than an AI-generated acknowledgment.
8. Recurring tasks, KB changes, and auto-watch configuration require Manage Server. A matching username grants no extra permissions. Members can manage their own tasks.
9. Scheduled posts can ping only the creator. Ordinary responses and scheduling confirmations disable mentions. Unsafe fallback sends that removed mention restrictions were removed.
10. Missing cached task channels are fetched. Long KB/task listings fit Discord message limits. Custom recurrence intervals and fractional durations display accurately.
11. Gemini receives actual image bytes for supported Discord attachments. Consecutive same-role turns merge. Empty/blocked/malformed responses fall back or fail cleanly. API keys travel in a header, not a URL.
12. Empty web search results explicitly tell the model that current facts were not verified.

Validation:

- `py -m unittest discover -q`: **85 passed** (55 existing tests plus 30 new regression tests).
- Python compilation: passed for all changed Python modules.
- Live AI text request: passed before and after provider changes.
- `py smoke_ai_helper.py`: passed real Civil War draft, persisted five-second follow-up recovered from disk, actual AI review, Gemini vision on a generated red image, and four usable live web search results.
- Discord read-only bot authentication: HTTP 200, bot account confirmed.
- Test Discord sends were captured locally. No live server test messages were posted. Actual per-channel sending permissions and gateway event delivery were not tested in a server.

Deployment status:

The existing controller and bot processes were running during the audit. Automatic restart could not be performed: desktop automation repeatedly reported `Computer Use native pipe is unavailable` with Windows error 2, including after resetting its session. Click **Restart** in **Switzer Bot Control** to load the saved changes. A live authenticated REST request does not establish that this existing process has loaded the updates.

Limits:

Tests cannot guarantee every natural-language interpretation, factual AI answer, or third-party service response. Clock-time scheduling remains unsupported. Timers are checked every five seconds; generation and network delays add latency. A process crash after Discord accepts a chunk but before its receipt is saved can duplicate that chunk. After repeated service failures, a task pauses for manual review rather than retrying forever. The bot still requires its host PC and network connection to stay running.

## Discord card UI completion — November 7, 2026

- Added consistent native Discord cards for AI answers, reminders, scheduled results, helper commands, summaries, and fact-checks. Follow-up notices are separate from the original answer.
- Long answers use numbered pages, with headings adapted for Discord and code fences closed/reopened between pages. Code contents are preserved during markdown cleanup.
- Embed-only `/ask` replies work. Reply and channel context read both embed cards and legacy text messages.
- Saved deliveries freeze their rendered pages. Legacy text tasks retain the old 1,990-character chunk boundaries when resuming after an upgrade, so unsent text is not skipped.
- Final offline suite: **99 tests passed**. Python compilation passed.
- Live AI draft and five-second review recovered from disk passed, with card output captured locally. Google returned HTTP 429; configured OpenRouter fallback handled these requests.
- Live vision identified a generated red image using a valid inline image payload through the fallback. Live search returned four usable sources. The diagnostic script's synthetic image was corrected so its OpenRouter fallback receives actual image bytes rather than a nonexistent attachment URL.
- No live Discord server messages were posted and the cards were not visually inspected in a live server. Restart Switzer Bot Control to load the changes. Cards require the bot's **Embed Links** channel permission.
