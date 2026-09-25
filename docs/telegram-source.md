# Telegram source for Shamsan Auto Publisher

This integration reads posts from the same private Janoub Voice channel used by the Janoub publisher and sends them through Shamsan's existing extraction, filtering, rewrite, image, and Supabase publishing pipeline.

## Telegram setup

- Use a dedicated source bot token that is **not** the source-polling token used by the Janoub repository. Two `getUpdates` pollers cannot safely share one bot token.
- Add the Shamsan source bot as an administrator of the private channel.
- Keep Shamsan's existing `TELEGRAM_BOT_TOKEN` intact; it is used to publish to Shamsan's output channel. The new source token is a separate setting.

## GitHub Actions settings

In `LegalResearcher/shmsan-auto-publisher` configure:

- Repository **secret** `TELEGRAM_SOURCE_BOT_TOKEN`: the dedicated source bot token.
- Repository **variable** `TELEGRAM_SOURCE_CHAT_ID`: `-1004430613399`.

Do not put bot tokens in the repository, issues, or commit history.

## Supabase setup

Apply `migration/20260925_telegram_source_cursor.sql` to the Supabase project used by Shamsan. The migration creates `public.bot_source_cursors` with RLS enabled and no public policies; the publisher accesses it using the existing server-side service-role secret. The cursor is independent of the Janoub project's cursor.

## Processing behavior

- Only `channel_post` updates from chat `-1004430613399` are accepted.
- The post text or photo caption is passed as raw input; a post with neither is ignored.
- Accepted posts are assigned to `أخبار وتقارير` and enter Shamsan's existing `rewrite_article()` flow and current Shamsan prompt. Telegram posts are not sent through webpage full-extraction; RSS items retain that behavior.
- The largest Telegram photo is downloaded (subject to Telegram's hosted Bot API download limit) and passed through Shamsan's existing image validation, processing, compression, and Supabase Storage upload pipeline.
- The Telegram update cursor is committed only after the batch has been safely handled. A processing failure leaves it pending for a retry. Already-published channel links are deduplicated by `source_url`.
- The current GitHub Actions workflow determines the polling interval; this integration does not change scheduling or turn the source into real-time delivery.
