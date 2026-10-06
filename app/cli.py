"""Command line surface. Every stage runs standalone so you can debug one at a time."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(add_completion=False, help="Automated Content Engine")
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    _setup_logging(verbose)


@app.command()
def ingest(once: bool = typer.Option(True, "--once/--loop")) -> None:
    """Stage 1 — poll every configured feed."""
    from app.ingest.fetch import ingest_once

    report = ingest_once()
    table = Table(title="Source health")
    table.add_column("source"); table.add_column("status"); table.add_column("items", justify="right")
    for src in report.sources:
        table.add_row(src.source_id, "[green]ok" if src.ok else f"[red]{src.error[:40]}",
                      str(src.count))
    console.print(table)
    console.print(f"[bold]inserted {report.inserted}[/bold] new items "
                  f"({report.skipped_duplicates} already seen)")


@app.command()
def detect(
    window: int = typer.Option(None, "--window", help="Window in minutes"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Stage 2 — cluster the window and score it against each entity's baseline."""
    from app.detect.velocity import run_detection

    reports = run_detection(dry_run=dry_run, window_minutes=window)
    if not reports:
        console.print("[yellow]no items in window[/yellow]")
        raise typer.Exit()

    table = Table(title=f"Clusters ({'dry run' if dry_run else 'live'})")
    for col, kw in [("", {}), ("entity", {}), ("domains", {"justify": "right"}),
                    ("base", {"justify": "right"}), ("thresh", {"justify": "right"}),
                    ("title", {}), ("reason", {})]:
        table.add_column(col, **kw)
    for r in sorted(reports, key=lambda r: (-r["fired"], -r["distinct_domains"])):
        table.add_row(
            "[green]FIRE" if r["fired"] else "[dim]-",
            r["entity"], str(r["distinct_domains"]), f"{r['baseline']:.1f}",
            f"{r['threshold']:.1f}", r["title"][:55], r["reason"][:60],
        )
    console.print(table)
    fired = [r for r in reports if r["fired"]]
    console.print(f"[bold]{len(fired)} fired[/bold] of {len(reports)} clusters")
    for r in fired:
        console.print(f"  topic {r['topic_id']}: {r['title']}  {r['domains']}")


@app.command()
def backfill(days: int = typer.Option(3, "--days")) -> None:
    """Replay collected items window-by-window to tune thresholds before committing."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from app.db import session_scope
    from app.detect.velocity import run_detection
    from app.models import RawItem
    from app.settings import detect_config

    step = detect_config()["poll_interval_minutes"]
    with session_scope() as s:
        oldest = s.scalar(select(func.min(RawItem.fetched_at)))
    if oldest is None:
        console.print("[yellow]no items collected yet — run `ingest` first[/yellow]")
        raise typer.Exit(1)

    start = max(oldest, datetime.now(timezone.utc) - timedelta(days=days))
    console.print(f"replaying from {start:%Y-%m-%d %H:%M} in {step}-minute steps "
                  f"[dim](dry run — nothing is written)[/dim]")

    total_windows = fired_total = 0
    cursor = start
    now = datetime.now(timezone.utc)
    while cursor < now:
        elapsed = int((now - cursor).total_seconds() / 60)
        reports = run_detection(dry_run=True, window_minutes=elapsed)
        fired = [r for r in reports if r["fired"]]
        if fired:
            console.print(f"[green]{cursor:%m-%d %H:%M}[/green] "
                          + "; ".join(f"{r['entity']} ({r['distinct_domains']}d)" for r in fired))
        fired_total += len(fired)
        total_windows += 1
        cursor += timedelta(minutes=step)

    console.print(f"\n[bold]{fired_total} fires across {total_windows} windows[/bold] "
                  f"— roughly {fired_total / max(days, 1):.1f}/day at current thresholds")


@app.command()
def scrape(topic: int = typer.Option(..., "--topic")) -> None:
    """Stage 2b — pull full text for a topic's sources."""
    from app import pipeline

    console.print_json(data=pipeline.run_scrape(topic))


@app.command()
def synth(topic: int = typer.Option(..., "--topic")) -> None:
    """Stage 3 — fact sheet, then article + script. Needs ANTHROPIC_API_KEY."""
    from app import pipeline

    console.print_json(data=pipeline.run_synthesis(topic))


@app.command()
def assets(topic: int = typer.Option(..., "--topic")) -> None:
    """Stage 4 — hero image, voiceover, subtitles, rendered MP4."""
    from app import pipeline

    console.print_json(data=pipeline.run_assets(topic))


@app.command()
def produce(topic: int = typer.Option(..., "--topic")) -> None:
    """Stages 2b through 4 for one topic."""
    from app import pipeline

    console.print_json(data=pipeline.produce(topic))


@app.command("export-web")
def export_web(
    topic: int = typer.Option(..., "--topic"),
    web_dir: str = typer.Option("", "--web-dir", help="Path to news_automation_web (default: WEB_PROJECT_DIR)"),
    allow_unapproved: bool = typer.Option(False, "--allow-unapproved",
                                          help="Sample/demo export of a topic not yet approved"),
) -> None:
    """Stage 6 (web) — write an approved topic into the news site's content folder."""
    from pathlib import Path

    from app.publishers.web import ExportError, export_topic
    from app.settings import get_settings

    settings = get_settings()
    target = web_dir or settings.web_project_dir
    if not target:
        console.print("[red]Set WEB_PROJECT_DIR in .env or pass --web-dir[/red]")
        raise typer.Exit(2)
    try:
        result = export_topic(topic, Path(target).expanduser(), settings.media_path,
                              allow_unapproved=allow_unapproved)
    except ExportError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print_json(data=result)


@app.command()
def topics(limit: int = 20) -> None:
    """List recent topics and their pipeline status."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Topic

    with session_scope() as s:
        rows = list(s.scalars(select(Topic).order_by(Topic.created_at.desc()).limit(limit)))
    table = Table(title="Topics")
    for col in ("id", "status", "entity", "title", "error"):
        table.add_column(col)
    for t in rows:
        table.add_row(str(t.id), t.status.value, t.primary_entity,
                      t.title[:50], (t.error or "")[:40])
    console.print(table)


@app.command()
def prune(days: int = typer.Option(None, "--days")) -> None:
    """Delete asset directories older than the retention window."""
    from app.assets.pipeline import prune_old_media

    console.print(f"removed {prune_old_media(days)} bundle(s)")


@app.command("run-all")
def run_all(
    fixture: bool = typer.Option(False, "--fixture", help="Use canned data; no API keys, no DB"),
    topic_id: int = typer.Option(9001, "--id"),
) -> None:
    """End-to-end smoke test.

    With --fixture this needs no database, no Anthropic key and no network: it produces
    a real hero image, real voiceover, real subtitles and a real MP4 from canned copy.
    That is the acceptance test for the asset pipeline.
    """
    if not fixture:
        from app import pipeline

        result = pipeline.run_poll()
        console.print_json(data={k: v for k, v in result.items() if k != "detections"})
        for tid in result["topic_ids"]:
            console.print_json(data=pipeline.produce(tid))
        return

    from app.assets.pipeline import produce_assets
    from app.settings import ROOT

    draft = json.loads((ROOT / "fixtures" / "draft.json").read_text())
    console.print(f"[bold]fixture run[/bold] — topic {topic_id}: {draft['article']['title']}")
    result = produce_assets(
        topic_id, draft["article"], draft["script"], draft["metadata"],
        entity=draft.get("entity", ""),
    )
    console.print_json(data={
        "directory": str(result.directory),
        "video": str(result.video),
        "duration_seconds": round(result.duration, 2),
        "providers_used": result.providers_used,
    })
    console.print(f"\n[green]open it:[/green] open {result.video}")


@app.command()
def notify(topic: int = typer.Option(..., "--topic")) -> None:
    """Stage 5 — manually (re)send the Telegram approval card for one topic."""
    from app.approval.service import push_for_approval

    result = push_for_approval(topic)
    console.print_json(data=result)
    if not result.get("sent"):
        raise typer.Exit(1)


@app.command("telegram-test")
def telegram_test() -> None:
    """Verify TELEGRAM_BOT_TOKEN is valid by asking Telegram who the bot is."""
    from app.approval.telegram_client import TelegramClient, TelegramError
    from app.settings import get_settings

    token = get_settings().telegram_bot_token
    if not token:
        console.print("[red]TELEGRAM_BOT_TOKEN is not set in .env[/red]")
        raise typer.Exit(1)
    try:
        me = TelegramClient(token).get_me()
    except TelegramError as exc:
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(1)
    console.print(f"[green]OK[/green] connected as @{me.get('username')} "
                  f"(id={me.get('id')})")


@app.command("telegram-chat-id")
def telegram_chat_id() -> None:
    """Find the chat id to put in TELEGRAM_CHAT_ID.

    Send any message in your bot's group/channel first, THEN run this — Telegram only
    keeps a short backlog of updates, so it has to see that message to report the chat.
    """
    from app.approval.telegram_client import TelegramClient, TelegramError
    from app.settings import get_settings

    token = get_settings().telegram_bot_token
    if not token:
        console.print("[red]TELEGRAM_BOT_TOKEN is not set in .env[/red]")
        raise typer.Exit(1)
    try:
        updates = TelegramClient(token).get_updates(timeout=0)
    except TelegramError as exc:
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(1)

    seen: dict[str, dict] = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            seen[str(chat["id"])] = chat
    if not seen:
        console.print("[yellow]No recent messages seen.[/yellow] Send a message in "
                      "your group/channel (with the bot added) and try again.")
        raise typer.Exit(1)
    table = Table(title="Chats the bot has seen recently")
    for col in ("chat_id", "type", "title/name"):
        table.add_column(col)
    for chat_id, chat in seen.items():
        table.add_row(chat_id, chat.get("type", ""),
                      chat.get("title") or chat.get("username") or chat.get("first_name", ""))
    console.print(table)
    console.print("\nCopy the chat_id you want into TELEGRAM_CHAT_ID in .env")


@app.command("approvals-once")
def approvals_once() -> None:
    """Poll Telegram a single time for button presses (useful for testing/cron)."""
    from app.approval.service import poll_once

    n = poll_once()
    console.print(f"processed {n} update(s)")


@app.command("approvals-poll")
def approvals_poll() -> None:
    """Run the approval listener in the foreground. Ctrl+C to stop.

    Long-polls Telegram (each call waits up to 25s for a button press), so this is not
    a busy loop. In docker-compose this runs as the `approvals` service instead.
    """
    from app.approval.service import poll_forever

    console.print("[bold]listening for Telegram button presses...[/bold] (Ctrl+C to stop)")
    try:
        poll_forever()
    except KeyboardInterrupt:
        console.print("\nstopped.")


@app.command("check-render")
def check_render() -> None:
    """Verify this ffmpeg can actually burn subtitles."""
    from app.assets.render import RenderError, check_ffmpeg

    try:
        check_ffmpeg()
    except RenderError as exc:
        console.print(f"[red]FAIL[/red] {exc}")
        raise typer.Exit(1)
    console.print("[green]OK[/green] ffmpeg has ass, zoompan and loudnorm")


if __name__ == "__main__":
    app()
