# World News Bot on GitHub (free, runs with your PC off)

Two scheduled jobs on GitHub's computers:
- **Shorts bot**: 3 times a day makes one Short from a story reported by 2+ news outlets, and sends it to your Telegram.
- **Daily news video**: once a day makes a 3 to 5 minute video of the top ~6 stories, and sends a thumbnail, the title, description (with chapters and source links) and a video copy to Telegram.

You upload to YouTube yourself from your phone. Check the facts against the source links first.

## Files
- `news_bot.py`, `shorts.py`, `prompts.json`, `config.json`, `requirements.txt`
- `.github/workflows/shorts.yml` and `daily-video.yml` (the schedules)

## Set up (about 30 minutes)
1. Make a free GitHub account (github.com).
2. Create a new **public** repository (green New button). Name it anything, tick "Add a README file".
3. Upload all files from this folder (unzip first), including the `.github` folder, using Add file, then Upload files. If the `.github` folder doesn't upload, create `.github/workflows/shorts.yml` and `.github/workflows/daily-video.yml` with Add file, Create new file, and paste each file's text.
4. Settings, Secrets and variables, Actions, New repository secret. Add:
   - `GEMINI_API_KEY` (aistudio.google.com)
   - `TELEGRAM_BOT_TOKEN` (from @BotFather)
   - `TELEGRAM_CHAT_ID` (from @userinfobot)
   - `PIXABAY_API_KEY` (pixabay.com/api/docs)
5. Settings, Actions, General, Workflow permissions: choose **Read and write permissions**, Save.
6. Actions tab, choose **Shorts bot**, Run workflow. Wait 10 to 20 minutes. A Short should arrive in Telegram.
7. Do the same for **Daily news video** (it can take 30+ minutes).

## Times
GitHub schedules use UTC. Shorts: 06:15, 12:15, 18:15. Daily video: 16:30. Edit the `cron:` lines in the `.yml` files to change them. GitHub can start scheduled runs a few minutes late.

## Good to know
- Public repo = unlimited free minutes. Logs are public, but your keys are hidden by GitHub Secrets and the bot never prints them.
- GitHub pauses scheduled runs on public repos after 60 days with no repo activity. The bot saves its memory file after each run, which should count. If runs stop, open the Actions tab and re-enable the workflow.
- If a run fails, the bot sends you a Telegram message with the reason.
- Full-quality daily video: Actions tab, open the run, Artifacts, `daily-news-video` (kept 3 days).
- Gemini: the bot tries `gemini-3.6-flash`, then `gemini-3.5-flash`, then `gemini-flash-lite-latest`. Edit `gemini_models` in `config.json` when Google retires models.
- Accuracy is your job. Always open at least one source link before uploading.
