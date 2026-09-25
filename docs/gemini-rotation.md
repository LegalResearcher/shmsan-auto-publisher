# Gemini rotation policy

Shamsan uses the same Gemini model/key rotation policy as Janoub Voice. This changes only how Gemini models and keys are selected; Shamsan's prompts, article formatting, publishing destination, and Telegram integration are unchanged.

## GitHub Actions configuration

The existing `GEMINI_API_KEYS` secret remains supported as a comma-separated list and is treated as one key group. To use multiple key groups, optionally define the `GEMINI_API_KEY_GROUPS` repository secret in this format:

```text
key1,key2;key3,key4
```

Semicolons separate groups; commas separate keys in each group. If `GEMINI_API_KEY_GROUPS` is empty or absent, the workflow falls back to `GEMINI_API_KEYS`.

## Rotation behavior

- Yemen local time from 00:00 through 13:59 is night mode. It tries `gemini-3.1-flash-lite`, then `gemini-3.5-flash-lite`; within each group it starts with the last key and rotates backward, then proceeds to the previous group.
- Yemen local time from 14:00 through 23:59 is day mode. It tries `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.7-flash`, `gemini-3.5-flash-lite`, then `gemini-3.1-flash-lite`; within each group it tries the full model sequence for each key, then advances to the next group.
- Daily quota exhaustion, unavailable models, and rejected keys advance the rotation. Transient rate-limit, server, and timeout errors follow the existing retry behavior.
- Rotation state is per process; a new workflow run starts at the initial model/key for the current Yemen-time mode.
