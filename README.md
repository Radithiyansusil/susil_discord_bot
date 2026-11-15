# Your own Discord music bot

Plays songs in a voice channel from:

- a song name: `/play tum hi ho`
- a YouTube or YouTube Music link, single video or playlist
- a Spotify track, album or playlist link (any public playlist)

The bot doesn't store any songs. It finds each song on YouTube at the moment you ask for it, so every song on YouTube is available, including ones released today.

## How it fits together

```
You type /play  ->  bot (Python)  ->  Lavalink (audio engine)  ->  YouTube
                                         |
                                         +-> LavaSrc plugin reads Spotify links,
                                             then plays the same song from YouTube
```

Both programs run in Docker on a free Oracle Cloud server, restart automatically, and stay online 24/7.

## What you need

- A Discord account and a server where you're an admin
- An Oracle Cloud account (free, but signing up needs a credit or debit card for verification)
- A **spare** Google account for YouTube sign-in (step 7). Never use your main one.
- About an hour for the first setup

---

## Step 1: Create the Discord bot

1. Go to https://discord.com/developers/applications and click **New Application**. Give it a name.
2. Open the **Installation** tab and set **Install Link** to **None**. (Discord won't let you make the bot private until you do this.)
3. Open the **Bot** tab:
   - Turn **Public Bot** OFF, so only you can add it to servers.
   - Click **Reset Token** and copy the token. It's shown only once. Treat it like a password.
   - You do not need any of the "Privileged Gateway Intents".
4. Open **OAuth2 → URL Generator**:
   - Scopes: tick `bot` and `applications.commands`
   - Bot permissions: tick `View Channels`, `Send Messages`, `Embed Links`, `Connect`, `Speak`
   - Open the generated link at the bottom and add the bot to your server.

## Step 2: Create the free Oracle Cloud server

1. Sign up at https://www.oracle.com/cloud/free/.
   - **Pick your home region carefully.** Free servers can only run in your home region and you can't change it later. Choose the one closest to you (for India: Mumbai or Hyderabad).
2. In the Oracle console, go to **Compute → Instances → Create instance**.
   - **Image:** Canonical Ubuntu 24.04
   - **Shape:** click *Change shape* → *Ampere* → `VM.Standard.A1.Flex`, then set **1 OCPU** and **6 GB** memory. That's plenty for this bot. Never go above **2 OCPU / 12 GB in total** across all your servers, or it stops being free.
   - **Networking:** keep the defaults and make sure "Assign a public IPv4 address" is on.
   - **SSH keys:** choose *Generate a key pair for me* and click **Save private key**. You need this file to log in.
   - Click **Create**. When it's running, copy its **Public IP address**.

> **"Out of capacity" error?** Free ARM servers are popular. Try another *Availability Domain* in the same screen, or try again later (early morning often works).

3. **Recommended: upgrade to Pay As You Go** (Billing → Upgrade and Manage Payment). Oracle may shut down free servers it thinks are idle for a week, and a music bot that nobody uses for a few days can look idle. Pay As You Go accounts don't get shut down this way, and you are still not charged as long as you stay within the free limits above. Set a small **budget alert** (Billing → Budgets) so Oracle emails you if anything ever starts costing money.

## Step 3: Log in to your server

On Windows, open **PowerShell** in the folder where you saved the key (replace the file name and IP):

```powershell
icacls .\ssh-key.key /inheritance:r /grant:r "$($env:USERNAME):(R)"
ssh -i .\ssh-key.key ubuntu@YOUR_SERVER_IP
```

The first command fixes a Windows "permissions are too open" error and only needs to be run once. Type `yes` if asked whether to continue connecting.

On Mac/Linux: `chmod 600 ssh-key.key` then `ssh -i ssh-key.key ubuntu@YOUR_SERVER_IP`.

## Step 4: Install Docker on the server

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
exit
```

Then log in again with the same `ssh` command from step 3, so the change takes effect.

## Step 5: Get the bot onto the server

**On the server**, install git and clone your repository (replace the URL with your own):

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
```

If your repository is **private** (recommended — see "Good to know"), the clone will ask for a
username and password. Enter your GitHub username, and for the password use a **personal access
token**, not your account password (GitHub → Settings → Developer settings → Fine-grained tokens →
give it read-only access to just this one repository). Plain passwords no longer work for git.

Later, to pull changes you've pushed from your PC, run `git pull` in this folder and then rebuild
(see "Everyday commands on the server").

## Step 6: Add your settings and start it

```bash
cp .env.example .env
openssl rand -hex 24      # prints a random password; copy it
nano .env
```

In the editor, paste your bot token after `DISCORD_TOKEN=` and the random password after `LAVALINK_PASSWORD=`. Optionally add your server ID after `GUILD_ID=` so commands appear instantly. Save with **Ctrl+O**, **Enter**, then exit with **Ctrl+X**.

Start everything:

```bash
docker compose up -d --build
docker compose logs -f
```

The first start takes a minute or two while Lavalink downloads its plugins. When you see `Logged in as ...` and `Lavalink ready`, press **Ctrl+C** to stop watching the logs. The bot keeps running.

## Step 7: Sign in to YouTube (do this once)

YouTube often blocks cloud servers as "bots". Signing in with a spare Google account fixes most of that.

```bash
docker compose logs -f lavalink
```

1. Look for a message asking you to visit **google.com/device** and enter a code.
2. Open that page on your phone or PC, **sign in with your spare Google account**, and enter the code.
3. Back in the logs, a **refresh token** gets printed. Copy it, then press **Ctrl+C**.
4. Open the config: `nano lavalink/application.yml`. Find the `oauth:` section, remove the `#` at the start of the `refreshToken` and `skipInitialization` lines, and paste your token between the quotes.
5. Save, exit, and restart Lavalink: `docker compose restart lavalink`

> The refresh token is a secret. It lives only in this file on the server. Don't paste it back into
> the copy on your PC and don't `git push` this change — treat it like your bot token.

Now try `/play` in Discord. 🎉

---

## Commands

| Command | What it does |
|---|---|
| `/play <name or link>` | Plays a song, or adds it (or a whole playlist) to the queue |
| `/playnext <name or link>` | Same, but puts it at the front of the queue |
| `/skip` | Skips the current song |
| `/pause` · `/resume` | Pauses or resumes |
| `/queue [page]` | Shows what's coming up |
| `/nowplaying` | Shows the current song with a progress bar |
| `/shuffle` | Shuffles the queue |
| `/loop <off / current song / whole queue>` | Repeats music |
| `/volume <0-150>` | Changes volume (default 60) |
| `/remove <number>` | Removes one song from the queue |
| `/clear` | Empties the queue but keeps the current song |
| `/stop` | Stops, clears the queue and leaves |

The bot leaves on its own after 5 minutes with nothing playing, or 1 minute after everyone leaves the voice channel. You can change both in `.env`.

## Everyday commands on the server

Run these from inside the folder you cloned:

| Task | Command |
|---|---|
| See what's happening | `docker compose logs -f` (Ctrl+C to stop watching) |
| Restart everything | `docker compose restart` |
| Pull the latest version you pushed | `git pull && docker compose up -d --build` |
| Apply changes to `bot.py` or `.env` | `docker compose up -d --build` |
| Apply changes to `application.yml` | `docker compose restart lavalink` |
| Turn the bot off | `docker compose down` |

The bot starts again by itself if the server reboots.

## When YouTube breaks (it will, sometimes)

YouTube changes things every few weeks to block tools like this, and the Lavalink developers release fixes. If songs suddenly stop playing:

1. Check the logs: `docker compose logs --tail 100 lavalink`
2. Look for a newer YouTube plugin at https://github.com/lavalink-devs/youtube-source/releases
3. Put the new version number in `lavalink/application.yml` (the comments there show exactly where), then `docker compose restart lavalink`

The plugin's GitHub "Issues" page is the best place to see whether others have the same problem and what fixed it.

## Troubleshooting

**Slash commands don't show up.** Put your server ID in `GUILD_ID` in `.env`, run `docker compose up -d --build`, and press Ctrl+R in Discord to reload it. If you used the bot before without `GUILD_ID`, you may briefly see each command twice; that goes away on its own.

**"The music server isn't ready yet."** Lavalink is still starting (wait a minute), or the password doesn't match. Check `docker compose logs lavalink`.

**"This video requires login", "Sign in to confirm you're not a bot", or the bot joins but stays silent.** Finish step 7. If it's already done, update the YouTube plugin (see above).

**"No supported audio streams available" or "All clients failed".** YouTube changed something. Update the YouTube plugin.

**A Spotify link doesn't load.** Spotify may have changed something on its side. Check for a newer LavaSrc release at https://github.com/topi314/LavaSrc/releases and update the version in `application.yml`.

**A Spotify song plays the wrong version (live, cover, remix).** The bot matches Spotify songs to YouTube by name, and sometimes picks a different upload. Use `/play` with the YouTube link of the version you want.

## Good to know

- **Keep the bot private.** It uses YouTube in a way YouTube's terms don't allow. Big public music bots have been shut down for this. Using it with your own friends is very common and low-risk, but don't make it public or earn money from it.
- **Never share your `.env` file or bot token.** If the token leaks, click *Reset Token* in the Developer Portal and update `.env`.
- Lavalink is never exposed to the internet, so you don't need to open any ports on Oracle.
