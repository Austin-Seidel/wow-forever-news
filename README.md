# WoW Forever News bot

Posts the most important World of Warcraft: Forever news to The Rested Experience's #news channel on Discord: at most 3 a day (9am / 2pm / 7pm Pacific). Big breaking news posts right away.

It runs every 15 minutes on GitHub Actions, so it works even when your PC is off.

- **Webhook URL:** stored as the repo secret `DISCORD_WEBHOOK_URL` (Settings → Secrets and variables → Actions).
- **Run it by hand:** go to the Actions tab → WoW Forever News → Run workflow. For mode, choose `test` to send a test message or `dry-run` to see today's articles ranked by score in the run log.
- **Tweak it:** the settings at the top of `wow_forever_webhook.py` control the posting times, the daily limit, the keywords and the news sources.
- **Already-posted list:** `wow_forever_seen.json`. The bot updates it automatically.
