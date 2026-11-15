"""
Private Discord music bot.

Plays songs by name, YouTube links and playlists, and Spotify tracks, albums
and playlists. Audio is handled by a Lavalink server (see lavalink/application.yml).
Spotify links are read by the LavaSrc plugin and the matching song is played
from YouTube, because Spotify itself never provides audio to bots.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from collections import OrderedDict
from typing import cast

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

try:  # Lets you run the bot outside Docker with a local .env file.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


# ---------------------------------------------------------------- settings --

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
LAVALINK_URI = os.getenv("LAVALINK_URI", "http://localhost:2333")
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "youshallnotpass")
GUILD_ID = os.getenv("GUILD_ID", "").strip()  # optional: instant slash commands in one server
DEFAULT_VOLUME = int(os.getenv("DEFAULT_VOLUME", "60"))
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT_SECONDS", "300"))  # leave after this long with nothing playing
ALONE_TIMEOUT = int(os.getenv("ALONE_TIMEOUT_SECONDS", "60"))  # leave after this long with nobody listening

COLOR = discord.Color.blurple()
URL_RE = re.compile(r"https?://", re.IGNORECASE)
QUEUE_PAGE_SIZE = 10

log = logging.getLogger("soorgam")


# ------------------------------------------------------------------ player --

class MusicPlayer(wavelink.Player):
    """A wavelink Player that remembers where to post messages and its volume."""

    home: discord.abc.Messageable | None = None
    now_playing_message: discord.Message | None = None
    preferred_volume: int = DEFAULT_VOLUME


# ----------------------------------------------------------------- helpers --

def format_time(ms: int) -> str:
    seconds = max(ms, 0) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def shorten(text: str, limit: int = 70) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def track_line(track: wavelink.Playable) -> str:
    length = "LIVE" if track.is_stream else format_time(track.length)
    title = discord.utils.escape_markdown(shorten(track.title))
    author = discord.utils.escape_markdown(shorten(track.author, 40))
    name = f"[{title}]({track.uri})" if track.uri else f"**{title}**"
    return f"{name} · {author} `{length}`"


def requester_of(track: wavelink.Playable) -> int | None:
    return getattr(track.extras, "requester_id", None)


def progress_bar(position: int, length: int, size: int = 16) -> str:
    if length <= 0:
        return "🔴 LIVE"
    filled = min(size - 1, int(size * position / length))
    return "▬" * filled + "🔘" + "▬" * (size - filled - 1)


def loop_label(player: MusicPlayer) -> str:
    return {
        wavelink.QueueMode.normal: "Off",
        wavelink.QueueMode.loop: "Current song",
        wavelink.QueueMode.loop_all: "Whole queue",
    }[player.queue.mode]


def now_playing_embed(player: MusicPlayer, track: wavelink.Playable, *, with_progress: bool = False) -> discord.Embed:
    embed = discord.Embed(title="🎶 Now playing", description=track_line(track), color=COLOR)
    if track.artwork:
        embed.set_thumbnail(url=track.artwork)
    if with_progress and not track.is_stream:
        embed.add_field(
            name="Progress",
            value=f"{progress_bar(player.position, track.length)}\n"
            f"`{format_time(player.position)} / {format_time(track.length)}`",
            inline=False,
        )
    requester = requester_of(track)
    if requester:
        embed.add_field(name="Requested by", value=f"<@{requester}>")
    embed.add_field(name="Up next", value=f"{player.queue.count} in queue")
    embed.add_field(name="Loop", value=loop_label(player))
    return embed


async def reply_error(interaction: discord.Interaction, text: str) -> None:
    message = f"❌ {text}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


def humans_in(channel: discord.abc.Connectable | None) -> int:
    members = getattr(channel, "members", [])
    return sum(1 for member in members if not member.bot)


async def get_player(interaction: discord.Interaction, *, connect: bool) -> MusicPlayer | None:
    """Return this server's player after checking the user is in the right voice channel.

    With connect=True the bot joins the user's channel if it isn't in one yet.
    Sends an error message and returns None when something is wrong.
    """
    guild = interaction.guild
    member = interaction.user
    assert guild is not None and isinstance(member, discord.Member)

    player = cast(MusicPlayer | None, guild.voice_client)
    user_channel = member.voice.channel if member.voice else None

    if player is None and not connect:
        await reply_error(interaction, "I'm not playing anything right now.")
        return None
    if user_channel is None:
        await reply_error(interaction, "Join a voice channel first.")
        return None

    if player is None:
        permissions = user_channel.permissions_for(guild.me)
        if not (permissions.connect and permissions.speak):
            await reply_error(interaction, f"I need permission to join and speak in {user_channel.mention}.")
            return None
        try:
            player = await user_channel.connect(cls=MusicPlayer, self_deaf=True)
        except Exception:
            log.exception("Could not join voice channel %s", user_channel.id)
            await reply_error(interaction, "I couldn't join your voice channel. Please try again in a moment.")
            return None
        player.autoplay = wavelink.AutoPlayMode.partial  # play the queue in order, no random recommendations
        player.home = cast(discord.abc.Messageable, interaction.channel)
        return player

    if player.channel != user_channel:
        await reply_error(interaction, f"I'm playing in {player.channel.mention}. Join me there!")
        return None
    return player


async def find_tracks(query: str) -> wavelink.Search:
    """Turn a song name or link into tracks. Plain names search YouTube Music, then YouTube."""
    if URL_RE.match(query):
        return await wavelink.Playable.search(query)
    results = await wavelink.Playable.search(query, source=wavelink.TrackSource.YouTubeMusic)
    if not results:
        results = await wavelink.Playable.search(query, source=wavelink.TrackSource.YouTube)
    return results


async def start_if_idle(player: MusicPlayer) -> None:
    if not player.playing and not player.queue.is_empty:
        await player.play(player.queue.get(), volume=player.preferred_volume)


async def leave_if_idle(player: MusicPlayer) -> None:
    """Leave right away when a failed search left the bot connected with nothing to do."""
    if not player.playing and player.queue.is_empty:
        await player.disconnect()


# ----------------------------------------------------------- autocomplete --

AUTOCOMPLETE_TTL = 600           # keep each cached result for 10 minutes
AUTOCOMPLETE_CACHE_MAX = 200     # remember at most this many queries at once
AUTOCOMPLETE_RESULTS = 8         # suggestions to show per query

# Cache: lowercased input -> (cached_at_monotonic, choices). OrderedDict so we can drop oldest.
_autocomplete_cache: "OrderedDict[str, tuple[float, list[app_commands.Choice[str]]]]" = OrderedDict()

# (From "Movie Name") or (From "Movie Name (Tamil)")  ->  keep just the movie name.
_FROM_RE = re.compile(r'\(\s*from\s+"([^"]+)"\s*\)', re.IGNORECASE)
_INNER_TAG_RE = re.compile(r"\s*\([^)]*\)\s*$")  # a trailing language tag like (Tamil)
# Junk tags wrapped in () or [], longest phrases first so they win the alternation.
_JUNK_RE = re.compile(
    r"[\(\[]\s*(?:"
    r"official music video|official video|official audio|full video song|"
    r"lyric video|video song|lyrical|audio|hd|4k"
    r")\s*[\)\]]",
    re.IGNORECASE,
)
_EMPTY_BRACKETS_RE = re.compile(r"[\(\[]\s*[\)\]]")
_TOPIC_RE = re.compile(r"\s*-\s*topic\s*$", re.IGNORECASE)


def clean_title(title: str) -> str:
    def from_repl(match: "re.Match[str]") -> str:
        inner = _INNER_TAG_RE.sub("", match.group(1)).strip()
        return f"({inner})" if inner else ""

    title = _FROM_RE.sub(from_repl, title)
    title = _JUNK_RE.sub("", title)
    title = _EMPTY_BRACKETS_RE.sub("", title)
    return re.sub(r"\s+", " ", title).strip()


def clean_artist(author: str) -> str:
    return _TOPIC_RE.sub("", author or "").strip()


def track_to_choice(track: wavelink.Playable) -> "app_commands.Choice[str] | None":
    """Build a '[m:ss] Title — Artist' choice, or None if the track has no usable uri."""
    uri = track.uri
    if not uri or len(uri) > 100:
        return None
    duration = "LIVE" if track.is_stream else format_time(track.length)
    title = shorten(clean_title(track.title) or track.title, 40)
    artist = shorten(clean_artist(track.author), 25)
    name = f"[{duration}] {title} — {artist}" if artist else f"[{duration}] {title}"
    return app_commands.Choice(name=shorten(name, 100), value=uri)


async def query_autocomplete(
    interaction: discord.Interaction, current: str
) -> "list[app_commands.Choice[str]]":
    """Suggest songs as the user types. Never raises; returns [] on timeout or error."""
    text = current.strip()
    if len(text) < 3 or URL_RE.match(text):
        return []

    key = text.lower()
    now = time.monotonic()
    cached = _autocomplete_cache.get(key)
    if cached is not None and now - cached[0] < AUTOCOMPLETE_TTL:
        _autocomplete_cache.move_to_end(key)
        return cached[1]

    try:
        results = await asyncio.wait_for(
            wavelink.Playable.search(text, source=wavelink.TrackSource.YouTubeMusic),
            timeout=2.5,
        )
    except Exception:  # timeout or anything else: show nothing rather than erroring
        return []

    tracks = results.tracks if isinstance(results, wavelink.Playlist) else results
    choices: "list[app_commands.Choice[str]]" = []
    for track in tracks[:AUTOCOMPLETE_RESULTS]:
        choice = track_to_choice(track)
        if choice is not None:
            choices.append(choice)

    _autocomplete_cache[key] = (now, choices)
    _autocomplete_cache.move_to_end(key)
    while len(_autocomplete_cache) > AUTOCOMPLETE_CACHE_MAX:
        _autocomplete_cache.popitem(last=False)
    return choices


# --------------------------------------------------------------------- bot --

class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()  # includes the voice states intent the bot needs
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        node = wavelink.Node(
            uri=LAVALINK_URI,
            password=LAVALINK_PASSWORD,
            inactive_player_timeout=IDLE_TIMEOUT,
        )
        log.info("Connecting to Lavalink at %s ...", LAVALINK_URI)
        await wavelink.Pool.connect(nodes=[node], client=self, cache_capacity=100)

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d slash commands to server %s", len(synced), GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands globally", len(synced))

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Logged in as %s (ID %s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/play"))

    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        log.info("Lavalink ready: %s (resumed: %s)", payload.node.identifier, payload.resumed)

    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        player = cast(MusicPlayer | None, payload.player)
        if player is None or player.home is None:
            return
        if player.now_playing_message is not None:  # keep the channel tidy: one "now playing" at a time
            try:
                await player.now_playing_message.delete()
            except discord.HTTPException:
                pass
        try:
            player.now_playing_message = await player.home.send(embed=now_playing_embed(player, payload.track))
        except discord.HTTPException:
            player.now_playing_message = None

    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        player = cast(MusicPlayer | None, payload.player)
        reason = (payload.exception or {}).get("message") or "unknown error"
        log.warning("Track failed: %s (%s)", payload.track.title, reason)
        if player and player.home:
            title = discord.utils.escape_markdown(shorten(payload.track.title))
            try:
                await player.home.send(f"⚠️ Couldn't play **{title}**, skipping it. ({shorten(reason, 120)})")
            except discord.HTTPException:
                pass

    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload) -> None:
        log.warning("Track stuck: %s", payload.track.title)
        if payload.player:
            await payload.player.skip(force=True)

    async def on_wavelink_inactive_player(self, player: wavelink.Player) -> None:
        player = cast(MusicPlayer, player)
        if player.home:
            try:
                await player.home.send("💤 Nothing has played for a while, so I left the voice channel.")
            except discord.HTTPException:
                pass
        await player.disconnect()

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """Leave when everyone else has left the voice channel."""
        if member.bot or before.channel is None or before.channel == after.channel:
            return
        player = cast(MusicPlayer | None, member.guild.voice_client)
        if player is None or player.channel != before.channel or humans_in(player.channel) > 0:
            return

        await asyncio.sleep(ALONE_TIMEOUT)

        player = cast(MusicPlayer | None, member.guild.voice_client)
        if player is None or humans_in(player.channel) > 0:
            return
        if player.home:
            try:
                await player.home.send("👋 Everyone left, so I stopped the music.")
            except discord.HTTPException:
                pass
        await player.disconnect()


bot = MusicBot()


# ---------------------------------------------------------------- commands --

@bot.tree.command(name="play", description="Play a song name, or a YouTube/Spotify link or playlist")
@app_commands.describe(query="Song name, or a YouTube / Spotify link (playlists and albums work too)")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str) -> None:
    await queue_tracks(interaction, query, play_next=False)


@bot.tree.command(name="playnext", description="Like /play, but puts the song at the front of the queue")
@app_commands.describe(query="Song name, or a YouTube / Spotify link")
@app_commands.guild_only()
async def playnext(interaction: discord.Interaction, query: str) -> None:
    await queue_tracks(interaction, query, play_next=True)


async def queue_tracks(interaction: discord.Interaction, query: str, *, play_next: bool) -> None:
    query = query.strip().strip("<>")  # Discord sometimes wraps pasted links in < >
    player = await get_player(interaction, connect=True)
    if player is None:
        return
    await interaction.response.defer(thinking=True)
    player.home = cast(discord.abc.Messageable, interaction.channel)

    try:
        results = await find_tracks(query)
    except wavelink.LavalinkLoadException as error:
        log.warning("Load failed for %r: %s", query, error.error)
        await interaction.followup.send(f"❌ I couldn't load that: {shorten(error.error, 200)}")
        await leave_if_idle(player)
        return
    except (wavelink.InvalidNodeException, wavelink.NodeException):
        log.exception("Lavalink is not reachable")
        await interaction.followup.send("❌ The music server isn't ready yet. Try again in a minute.")
        await leave_if_idle(player)
        return

    if not results:
        await interaction.followup.send(f"🔍 No results for **{discord.utils.escape_markdown(shorten(query))}**.")
        await leave_if_idle(player)
        return

    # A link to one video inside a YouTube "Mix" (list=RD...) should add just that video, not 50 random ones.
    if isinstance(results, wavelink.Playlist) and "list=RD" in query and 0 <= results.selected < len(results.tracks):
        results = [results.tracks[results.selected]]

    if isinstance(results, wavelink.Playlist):
        results.track_extras(requester_id=interaction.user.id)
        if play_next:
            for track in reversed(results.tracks):
                player.queue.put_at(0, track)
            added = len(results.tracks)
        else:
            added = await player.queue.put_wait(results)
        name = discord.utils.escape_markdown(shorten(results.name or "playlist"))
        where = "to the front of the queue" if play_next else "to the queue"
        await interaction.followup.send(f"📃 Added **{added}** songs from **{name}** {where}.")
    else:
        track = results[0]
        track.extras = {"requester_id": interaction.user.id}
        title = discord.utils.escape_markdown(shorten(track.title))
        if player.playing:
            if play_next:
                player.queue.put_at(0, track)
                position = 1
            else:
                await player.queue.put_wait(track)
                position = player.queue.count
            await interaction.followup.send(f"➕ Queued **{title}** · position #{position}")
        else:
            await player.queue.put_wait(track)
            await interaction.followup.send(f"▶️ Playing **{title}**")

    await start_if_idle(player)


@play.autocomplete("query")
async def play_query_autocomplete(
    interaction: discord.Interaction, current: str
) -> "list[app_commands.Choice[str]]":
    return await query_autocomplete(interaction, current)


@playnext.autocomplete("query")
async def playnext_query_autocomplete(
    interaction: discord.Interaction, current: str
) -> "list[app_commands.Choice[str]]":
    return await query_autocomplete(interaction, current)


@bot.tree.command(name="skip", description="Skip the current song")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction) -> None:
    player = await get_player(interaction, connect=False)
    if player is None:
        return
    if player.current is None:
        await reply_error(interaction, "Nothing is playing.")
        return
    title = discord.utils.escape_markdown(shorten(player.current.title))
    await player.skip(force=True)
    await interaction.response.send_message(f"⏭️ Skipped **{title}**")


@bot.tree.command(name="pause", description="Pause the music")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction) -> None:
    player = await get_player(interaction, connect=False)
    if player is None:
        return
    if player.paused or player.current is None:
        await reply_error(interaction, "Nothing is playing right now.")
        return
    await player.pause(True)
    await interaction.response.send_message("⏸️ Paused. Use /resume to continue.")


@bot.tree.command(name="resume", description="Resume the music")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction) -> None:
    player = await get_player(interaction, connect=False)
    if player is None:
        return
    if not player.paused:
        await reply_error(interaction, "The music isn't paused.")
        return
    await player.pause(False)
    await interaction.response.send_message("▶️ Resumed.")


@bot.tree.command(name="stop", description="Stop the music, clear the queue and leave the voice channel")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction) -> None:
    player = await get_player(interaction, connect=False)
    if player is None:
        return
    player.queue.clear()
    await player.disconnect()
    await interaction.response.send_message("⏹️ Stopped and left the voice channel.")


@bot.tree.command(name="queue", description="Show the songs waiting to play")
@app_commands.describe(page="Page number (10 songs per page)")
@app_commands.guild_only()
async def show_queue(interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1) -> None:
    player = cast(MusicPlayer | None, interaction.guild.voice_client if interaction.guild else None)
    if player is None or (player.current is None and player.queue.is_empty):
        await reply_error(interaction, "The queue is empty. Add songs with /play.")
        return

    tracks = list(player.queue)
    pages = max(1, math.ceil(len(tracks) / QUEUE_PAGE_SIZE))
    page = min(page, pages)
    start = (page - 1) * QUEUE_PAGE_SIZE
    lines = [f"`{number}.` {track_line(track)}" for number, track in enumerate(tracks[start : start + QUEUE_PAGE_SIZE], start + 1)]

    total_ms = sum(track.length for track in tracks if not track.is_stream)
    embed = discord.Embed(
        title=f"📜 Queue · {len(tracks)} songs · {format_time(total_ms)}",
        description="\n".join(lines) or "Nothing else queued.",
        color=COLOR,
    )
    if player.current:
        embed.add_field(name="Now playing", value=track_line(player.current), inline=False)
    embed.set_footer(text=f"Page {page}/{pages} · Loop: {loop_label(player)}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Show the song that's playing")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction) -> None:
    player = cast(MusicPlayer | None, interaction.guild.voice_client if interaction.guild else None)
    if player is None or player.current is None:
        await reply_error(interaction, "Nothing is playing.")
        return
    await interaction.response.send_message(embed=now_playing_embed(player, player.current, with_progress=True))


@bot.tree.command(name="shuffle", description="Shuffle the queue")
@app_commands.guild_only()
async def shuffle(interaction: discord.Interaction) -> None:
    player = await get_player(interaction, connect=False)
    if player is None:
        return
    if player.queue.count < 2:
        await reply_error(interaction, "Add at least two songs to the queue before shuffling.")
        return
    player.queue.shuffle()
    await interaction.response.send_message(f"🔀 Shuffled {player.queue.count} songs.")


@bot.tree.command(name="loop", description="Repeat the current song or the whole queue")
@app_commands.describe(mode="What to repeat")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Current song", value="song"),
        app_commands.Choice(name="Whole queue", value="queue"),
    ]
)
@app_commands.guild_only()
async def loop(interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
    player = await get_player(interaction, connect=False)
    if player is None:
        return
    player.queue.mode = {
        "off": wavelink.QueueMode.normal,
        "song": wavelink.QueueMode.loop,
        "queue": wavelink.QueueMode.loop_all,
    }[mode.value]
    await interaction.response.send_message(f"🔁 Loop: **{mode.name}**")


@bot.tree.command(name="volume", description="Change the volume (default 60)")
@app_commands.describe(level="0 to 150")
@app_commands.guild_only()
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 150]) -> None:
    player = await get_player(interaction, connect=False)
    if player is None:
        return
    player.preferred_volume = level
    await player.set_volume(level)
    await interaction.response.send_message(f"🔊 Volume set to **{level}**")


@bot.tree.command(name="remove", description="Remove a song from the queue by its number")
@app_commands.describe(position="The song's number in /queue")
@app_commands.guild_only()
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1]) -> None:
    player = await get_player(interaction, connect=False)
    if player is None:
        return
    if position > player.queue.count:
        await reply_error(interaction, f"There are only {player.queue.count} songs in the queue.")
        return
    track = player.queue.peek(position - 1)
    player.queue.delete(position - 1)
    await interaction.response.send_message(f"🗑️ Removed **{discord.utils.escape_markdown(shorten(track.title))}**")


@bot.tree.command(name="clear", description="Remove every song from the queue (keeps the current song)")
@app_commands.guild_only()
async def clear(interaction: discord.Interaction) -> None:
    player = await get_player(interaction, connect=False)
    if player is None:
        return
    count = player.queue.count
    player.queue.clear()
    await interaction.response.send_message(f"🧹 Cleared {count} songs from the queue.")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    log.exception("Command /%s failed", interaction.command.name if interaction.command else "?", exc_info=error)
    await reply_error(interaction, "Something went wrong. Please try again.")


if __name__ == "__main__":
    discord.utils.setup_logging(level=logging.INFO)
    bot.run(DISCORD_TOKEN, log_handler=None)
