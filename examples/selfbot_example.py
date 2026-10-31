"""
Hilleye // Discord Selfbot Client Integration Example (discord.py-self + aiohttp)

Demonstrates handling the `,who <ckey>` command by fetching the rendered
PNG player dossier card from the local Hilleye API and attaching it as a
discord.File (since Discord user accounts cannot send Rich Embeds).

Usage:
    pip install discord.py-self aiohttp
    export DISCORD_USER_TOKEN="your_token_here"
    python examples/selfbot_example.py
"""

import io
import os
import re

import aiohttp
import discord
from discord.ext import commands

HILLEYE_API_BASE = os.getenv("HILLEYE_API_BASE", "http://127.0.0.1:8000")
DISCORD_TOKEN = os.getenv("DISCORD_USER_TOKEN", "YOUR_DISCORD_USER_TOKEN")

bot = commands.Bot(command_prefix=",", self_bot=True)


def normalize_ckey(raw: str) -> str:
    """Normalizes a BYOND ckey to lowercase alphanumeric characters."""
    return re.sub(r"[^a-z0-9]", "", (raw or "").lower())


@bot.command(name="who")
async def who_command(ctx: commands.Context, *, ckey: str = "") -> None:
    """
    Fetches a player's PNG dossier card from Hilleye and uploads it to the channel.
    Command syntax: ,who <ckey>
    """
    clean_ckey = normalize_ckey(ckey)
    if not clean_ckey:
        await ctx.send("Usage: `,who <ckey>`")
        return

    card_url = f"{HILLEYE_API_BASE}/api/v1/player/{clean_ckey}/card.png"

    try:
        timeout = aiohttp.ClientTimeout(total=5.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(card_url) as resp:
                if resp.status == 404:
                    await ctx.send(f"Target `{clean_ckey}` not found in Hilleye telemetry.")
                    return
                if resp.status != 200:
                    await ctx.send(f"Hilleye API returned HTTP {resp.status}.")
                    return

                png_data = await resp.read()
    except Exception as exc:
        await ctx.send(f"Failed to query Hilleye API: `{exc}`")
        return

    file_attachment = discord.File(io.BytesIO(png_data), filename=f"{clean_ckey}.png")
    await ctx.send(file=file_attachment)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
