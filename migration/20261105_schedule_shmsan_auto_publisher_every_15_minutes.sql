-- Run the Shamsan News publisher from Supabase pg_cron every 15 minutes.
-- The workflow exposes workflow_dispatch for the Supabase GitHub token.
DO $migration$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM cron.job
    WHERE jobname = 'shmsan-auto-publisher-every-15-min'
  ) THEN
    PERFORM cron.unschedule('shmsan-auto-publisher-every-15-min');
  END IF;

  PERFORM cron.schedule(
    'shmsan-auto-publisher-every-15-min',
    '*/15 * * * *',
    $job$
      SELECT net.http_post(
        url := 'https://api.github.com/repos/LegalResearcher/shmsan-auto-publisher/actions/workflows/auto_publish_shmsan.yml/dispatches',
        headers := jsonb_build_object(
          'Accept', 'application/vnd.github+json',
          'Authorization', 'Bearer ' || (
            SELECT decrypted_secret
            FROM vault.decrypted_secrets
            WHERE name = 'github_actions_token'
            LIMIT 1
          ),
          'Content-Type', 'application/json',
          'X-GitHub-Api-Version', '2022-11-28'
        ),
        body := jsonb_build_object('ref', 'main'),
        timeout_milliseconds := 10000
      );
    $job$
  );
END;
$migration$;
