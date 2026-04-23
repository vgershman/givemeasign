"""Typer CLI entry point."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import typer

from givemeasign import __version__
from givemeasign.observability.logging import configure_logging, logger

app = typer.Typer(
    name="givemeasign",
    help="Autonomous startup-idea scoring pipeline.",
    no_args_is_help=True,
    add_completion=False,
)


_PIPELINE_COMMANDS = {
    "extract-pains",
    "build-candidates",
    "score-candidates",
    "run-pipeline",
    "fetch-reddit",
    "fetch-hn",
    "fetch-devto",
    "fetch-product-hunt",
    "send-deck",
    "bot",
    "generate-research",
}


@app.callback()
def _bootstrap(ctx: typer.Context) -> None:
    """Bootstrap logging for every subcommand.

    Pipeline-class commands also wire the Telegram log sink so progress
    flows to your phone when the runtime toggle is on.
    """
    cmd = (ctx.invoked_subcommand or "").strip()
    configure_logging(with_telegram_sink=cmd in _PIPELINE_COMMANDS)


# --------------------------------------------------------------- utility


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"givemeasign {__version__}")


@app.command()
def doctor() -> None:
    """Validate env, database, LLM access, and Telegram token."""
    from givemeasign.doctor import print_checks, run_all

    logger.info("Running doctor checks…")
    checks = asyncio.run(run_all())
    ok = print_checks(checks)
    if ok:
        logger.info("All checks passed.")
        raise typer.Exit(code=0)
    logger.error("One or more checks failed — fix and re-run.")
    raise typer.Exit(code=1)


# --------------------------------------------------------------- sources


@app.command("fetch-reddit")
def fetch_reddit(
    subreddit: str = typer.Argument(..., help="Subreddit name (without r/ prefix)."),
    limit: int = typer.Option(25, "--limit", "-n", help="Max posts to fetch."),
    sort: str = typer.Option("hot", "--sort", "-s", help="hot | new | top | rising"),
    top_comments: int = typer.Option(
        10, "--comments", "-c", help="Top-N comments per post (by score)."
    ),
) -> None:
    """Fetch posts from one subreddit and store them as raw_signals."""
    from givemeasign.sources.reddit import fetch_and_store

    if sort not in {"hot", "new", "top", "rising"}:
        logger.error(f"Unknown sort: {sort!r}. Use one of: hot, new, top, rising.")
        raise typer.Exit(code=2)

    logger.info(f"Fetching r/{subreddit} [sort={sort}, limit={limit}, comments={top_comments}]…")
    inserted, duplicates = asyncio.run(
        fetch_and_store(subreddit, limit=limit, sort=sort, top_comments=top_comments)  # type: ignore[arg-type]
    )
    logger.info(f"Done. Inserted {inserted} new, skipped {duplicates} duplicate(s).")


@app.command("fetch-hn")
def fetch_hn(
    tags: str = typer.Option(
        "ask_hn,story",
        "--tags",
        "-t",
        help="HN Algolia tag filter: ask_hn,story | show_hn,story | story",
    ),
    limit: int = typer.Option(25, "--limit", "-n", help="Max stories to fetch."),
    since_days: int = typer.Option(
        30, "--since-days", "-d", help="Only stories newer than N days."
    ),
) -> None:
    """Fetch Hacker News threads (default: Ask HN) and store as raw_signals."""
    from givemeasign.sources.hackernews import fetch_and_store

    logger.info(f"Fetching HN [tags={tags}, limit={limit}, since={since_days}d]…")
    inserted, duplicates = asyncio.run(
        fetch_and_store(tags=tags, limit=limit, since_days=since_days)
    )
    logger.info(f"Done. Inserted {inserted} new, skipped {duplicates} duplicate(s).")


@app.command("fetch-devto")
def fetch_devto(
    tag: str = typer.Option("discuss", "--tag", "-t", help="Dev.to tag (discuss, beginners, help, …)."),
    limit: int = typer.Option(25, "--limit", "-n", help="Max articles to fetch."),
    top_days: int = typer.Option(7, "--top-days", "-d", help="Top articles over last N days."),
) -> None:
    """Fetch Dev.to articles + comments (no auth) and store as raw_signals."""
    from givemeasign.sources.devto import fetch_and_store

    logger.info(f"Fetching Dev.to [tag={tag}, limit={limit}, top_days={top_days}]…")
    inserted, duplicates = asyncio.run(fetch_and_store(tag=tag, limit=limit, top_days=top_days))
    logger.info(f"Done. Inserted {inserted} new, skipped {duplicates} duplicate(s).")


@app.command("fetch-product-hunt")
def fetch_product_hunt(
    limit: int = typer.Option(25, "--limit", "-n", help="Max posts to fetch."),
) -> None:
    """Fetch Product Hunt launches + comments and store as raw_signals."""
    from givemeasign.sources.product_hunt import fetch_and_store

    logger.info(f"Fetching Product Hunt [limit={limit}]…")
    try:
        inserted, duplicates = asyncio.run(fetch_and_store(limit=limit))
    except RuntimeError as e:
        logger.error(str(e))
        raise typer.Exit(code=2) from e
    logger.info(f"Done. Inserted {inserted} new, skipped {duplicates} duplicate(s).")


# --------------------------------------------------------------- pipeline


@app.command("extract-pains")
def extract_pains(
    limit: int = typer.Option(20, "--limit", "-n", help="Max unprocessed raw_signals to process."),
) -> None:
    """Run Haiku extraction over unprocessed raw_signals → pain_signals."""
    from givemeasign.pipeline.extract import extract_pains_batch

    logger.info(f"Starting extraction batch (limit={limit})…")
    summary = asyncio.run(extract_pains_batch(limit=limit))
    logger.info(
        f"Done. processed={summary.processed} skipped={summary.skipped} "
        f"pains={summary.total_pains} cost=${summary.total_cost_usd:.4f}"
    )


@app.command("build-candidates")
def build_candidates(
    pain_limit: int = typer.Option(
        100, "--pain-limit", "-p", help="Max unclustered pain_signals to feed synthesis."
    ),
) -> None:
    """Cluster unclustered pain_signals into candidate ideas via Sonnet."""
    from givemeasign.pipeline.candidates import synthesize_candidates_batch

    logger.info(f"Starting synthesis (pain_limit={pain_limit})…")
    summary = asyncio.run(synthesize_candidates_batch(pain_limit=pain_limit))
    logger.info(
        f"Done. input_pains={summary.input_pains} "
        f"candidates={summary.candidates} "
        f"linked={summary.linked_pains} orphan={summary.orphan_pains} "
        f"cost=${summary.total_cost_usd:.4f}"
    )


@app.command("run-pipeline")
def run_pipeline_cmd(
    no_fetch: bool = typer.Option(False, "--no-fetch", help="Skip the fetch stage."),
    hn_limit: int = typer.Option(25, "--hn-limit", help="HN stories to fetch."),
    hn_tags: str = typer.Option("ask_hn,story", "--hn-tags", help="HN Algolia tag filter."),
    devto_limit: int = typer.Option(25, "--devto-limit", help="Dev.to articles to fetch."),
    devto_tag: str = typer.Option("discuss", "--devto-tag", help="Dev.to tag filter."),
    ph_limit: int = typer.Option(25, "--ph-limit", help="Product Hunt launches to fetch (if configured)."),
    extract_limit: int = typer.Option(100, "--extract-limit"),
    synth_pain_limit: int = typer.Option(100, "--synth-pain-limit"),
) -> None:
    """End-to-end: fetch (HN + Dev.to + Product Hunt if configured) → extract → synthesize."""
    from givemeasign.pipeline.run import run_pipeline

    logger.info("Running full pipeline…")
    summary = asyncio.run(
        run_pipeline(
            fetch=not no_fetch,
            hn_limit=hn_limit,
            hn_tags=hn_tags,
            devto_limit=devto_limit,
            devto_tag=devto_tag,
            ph_limit=ph_limit,
            extract_limit=extract_limit,
            synth_pain_limit=synth_pain_limit,
        )
    )
    logger.info(f"Pipeline complete. Summary: {summary.as_dict()}")


# --------------------------------------------------------------- debug / inspection


@app.command("list-raw")
def list_raw(
    source: str = typer.Option(None, "--source", help="Filter by source (e.g. 'reddit')."),
    limit: int = typer.Option(10, "--limit", "-n"),
) -> None:
    """List the most recent raw_signals rows (debugging helper)."""
    from sqlalchemy import select

    from givemeasign.db.models import RawSignal
    from givemeasign.db.session import session_scope

    with session_scope() as s:
        stmt = select(RawSignal).order_by(RawSignal.created_at.desc()).limit(limit)
        if source:
            stmt = stmt.where(RawSignal.source == source)
        rows = s.execute(stmt).scalars().all()
        if not rows:
            typer.echo("(no rows)")
            return
        for r in rows:
            payload = r.payload or {}
            header = payload.get("post") or payload.get("story") or {}
            title = (header.get("title") or "").strip().replace("\n", " ")
            comments = payload.get("comments") or payload.get("children") or []
            mark = "processed" if r.processed_at else "pending  "
            typer.echo(
                f"{r.created_at.isoformat(timespec='seconds')}  "
                f"{mark}  "
                f"{r.source}:{r.source_id}  "
                f"[{len(comments)} top-lvl]  "
                f"{title[:80]}"
            )


@app.command("list-pains")
def list_pains(
    limit: int = typer.Option(20, "--limit", "-n"),
    min_strength: float = typer.Option(0.0, "--min-strength"),
) -> None:
    """List recent pain_signals (debug)."""
    from sqlalchemy import select

    from givemeasign.db.models import PainSignal
    from givemeasign.db.session import session_scope

    with session_scope() as s:
        stmt = (
            select(PainSignal)
            .where(PainSignal.strength >= min_strength)
            .order_by(PainSignal.created_at.desc())
            .limit(limit)
        )
        rows = s.execute(stmt).scalars().all()
        if not rows:
            typer.echo("(no rows)")
            return
        for r in rows:
            tags = ",".join(r.topic_tags) if r.topic_tags else "-"
            typer.echo(f"{r.strength:.2f}  [{tags:<30}]  {r.text[:100]}")


@app.command("score-candidates")
def score_candidates_cmd(
    limit: int = typer.Option(50, "--limit", "-n", help="Max unscored candidates to process."),
    dedup_similarity: float = typer.Option(
        0.80, "--dedup-similarity", help="Cosine similarity threshold for cross-candidate dedup."
    ),
    rescore: bool = typer.Option(
        False,
        "--rescore",
        help="Re-score candidates whose latest scores are at an older scorer_version (prompt changed).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Re-score ALL scored/gated/deduped candidates regardless of scorer_version. "
            "Use after the scoring EVIDENCE shape changed (e.g. M4b trends) but the prompt "
            "version stayed the same."
        ),
    ),
) -> None:
    """Score each unscored candidate on 7 dimensions (Haiku), gate, aggregate, dedup."""
    from givemeasign.scoring.runner import score_candidates_batch

    logger.info(
        f"Scoring candidates (limit={limit}, dedup_sim={dedup_similarity}, "
        f"rescore={rescore}, force={force})…"
    )
    summary = asyncio.run(
        score_candidates_batch(
            limit=limit,
            dedup_similarity=dedup_similarity,
            rescore=rescore,
            force=force,
        )
    )
    logger.info(
        f"Done. scored={summary.scored} gated={summary.gated} "
        f"deduplicated={summary.deduplicated} failed={summary.failed} "
        f"cost=${summary.total_cost_usd:.4f}"
    )


@app.command("top-candidates")
def top_candidates(
    limit: int = typer.Option(10, "--limit", "-n", help="Max rows to show."),
    include_gated: bool = typer.Option(
        False, "--include-gated", help="Also show gated_out candidates."
    ),
    include_deduped: bool = typer.Option(
        False, "--include-deduped", help="Also show deduplicated candidates."
    ),
) -> None:
    """Top-N candidates by aggregate_score with their dimension breakdown."""
    from sqlalchemy import select

    from givemeasign.db.models import Candidate, Score
    from givemeasign.db.session import session_scope
    from givemeasign.llm.prompts.score_candidate import SCORER_VERSION

    allowed_statuses = ["scored"]
    if include_gated:
        allowed_statuses.append("gated_out")
    if include_deduped:
        allowed_statuses.append("deduplicated")

    with session_scope() as s:
        stmt = (
            select(Candidate)
            .where(Candidate.status.in_(allowed_statuses))
            .order_by(Candidate.aggregate_score.desc().nulls_last())
            .limit(limit)
        )
        rows = s.execute(stmt).scalars().all()
        if not rows:
            typer.echo("(no scored candidates — run `givemeasign score-candidates` first)")
            return
        for c in rows:
            agg = c.aggregate_score
            agg_str = f"{agg:.3f}" if agg is not None else "  -  "
            status_tag = f"[{c.status}]" if c.status != "scored" else ""
            typer.echo(f"{agg_str}  {status_tag}  {c.concept[:110]}")
            if c.gate_failed:
                typer.echo(f"         gated by: {c.gate_failed}")
            # Per-dimension scores at the CURRENT scorer_version only.
            # Older versions stay in the table as audit history but don't pollute the display.
            dim_rows = s.execute(
                select(Score)
                .where(Score.candidate_id == c.id)
                .where(Score.scorer_version == SCORER_VERSION)
                .order_by(Score.dimension)
            ).scalars().all()
            if dim_rows:
                dim_str = "  ".join(f"{d.dimension[:5]}={d.value:.2f}" for d in dim_rows)
                typer.echo(f"         {dim_str}")
            typer.echo("")


@app.command("list-candidates")
def list_candidates(
    limit: int = typer.Option(10, "--limit", "-n"),
    min_confidence: float = typer.Option(0.0, "--min-confidence"),
) -> None:
    """List recent synthesized candidates."""
    from sqlalchemy import func, select

    from givemeasign.db.models import Candidate, CandidateSignal
    from givemeasign.db.session import session_scope

    with session_scope() as s:
        pain_counts = (
            select(
                CandidateSignal.candidate_id,
                func.count().label("pain_count"),
            )
            .group_by(CandidateSignal.candidate_id)
            .subquery()
        )
        stmt = (
            select(Candidate, pain_counts.c.pain_count)
            .outerjoin(pain_counts, pain_counts.c.candidate_id == Candidate.id)
            .where(Candidate.confidence >= min_confidence)
            .order_by(Candidate.confidence.desc(), Candidate.created_at.desc())
            .limit(limit)
        )
        rows = s.execute(stmt).all()
        if not rows:
            typer.echo("(no candidates)")
            return
        for cand, pain_count in rows:
            typer.echo(
                f"{cand.confidence:.2f}  [{pain_count or 0} pains]  {cand.concept}"
            )
            if cand.target_user:
                typer.echo(f"       user:    {cand.target_user}")
            if cand.value_prop:
                typer.echo(f"       value:   {cand.value_prop}")
            if cand.angles:
                for a in cand.angles:
                    typer.echo(f"       angle:   {a}")
            typer.echo("")


@app.command("generate-research")
def generate_research_cmd(
    candidate_id: str = typer.Argument(..., help="Candidate UUID to research."),
    no_send: bool = typer.Option(
        False,
        "--no-send",
        help="Generate and store pack, but don't deliver to Telegram.",
    ),
) -> None:
    """Force-generate a deep-research pack for a candidate (manual trigger).

    Normally this is triggered automatically by right/super swipes. Use this
    to retry a failed pack, research a candidate without swiping, or test
    the Opus path without touching the bot.
    """
    from uuid import UUID

    from givemeasign.research.generator import generate_pack
    from givemeasign.research.delivery import deliver_pack
    from givemeasign.llm.router import LLMRouter

    try:
        cid = UUID(candidate_id)
    except ValueError as e:
        logger.error(f"not a valid UUID: {candidate_id!r}")
        raise typer.Exit(code=2) from e

    router = LLMRouter()
    logger.info(f"Generating research pack for {cid}…")
    result = asyncio.run(generate_pack(candidate_id=cid, router=router))
    if result is None:
        logger.error("Generation failed — see earlier log lines.")
        raise typer.Exit(code=1)

    logger.info(
        f"Pack complete. recommendation={result.recommendation} "
        f"cost=${result.usd_cost:.4f}"
    )
    if no_send:
        logger.info("Skipping delivery (--no-send).")
        return
    ok = asyncio.run(deliver_pack(result.pack_id))
    if not ok:
        logger.error("Delivery failed — pack is stored, you can retry later.")
        raise typer.Exit(code=1)
    logger.info("Delivered.")


@app.command("list-research")
def list_research_cmd(
    limit: int = typer.Option(15, "--limit", "-n"),
) -> None:
    """List recent research packs with status + cost."""
    from sqlalchemy import select

    from givemeasign.db.models import Candidate, ResearchPack
    from givemeasign.db.session import session_scope

    with session_scope() as s:
        rows = list(
            s.execute(
                select(ResearchPack, Candidate)
                .join(Candidate, Candidate.id == ResearchPack.candidate_id)
                .order_by(ResearchPack.created_at.desc())
                .limit(limit)
            ).all()
        )
        if not rows:
            typer.echo("(no research packs yet)")
            return
        for pack, candidate in rows:
            rec = pack.recommendation or "—"
            sent = pack.sent_at.isoformat(timespec="seconds") if pack.sent_at else "—"
            cost = pack.usd_cost or 0.0
            typer.echo(
                f"{pack.status:<11} rec={rec:<6} cost=${cost:<6.4f} sent={sent:<20} "
                f"{candidate.concept[:90]}"
            )
            if pack.error_message:
                typer.echo(f"           error: {pack.error_message[:150]}")


@app.command("bot")
def bot_cmd() -> None:
    """Run the long-running Telegram bot (polls Telegram, handles callbacks, schedules daily deck)."""
    from givemeasign.telegram.bot import run_bot

    asyncio.run(run_bot())


@app.command("send-deck")
def send_deck_cmd(
    n: int = typer.Option(10, "--limit", "-n", help="Top-N candidates to send."),
    user_id: int = typer.Option(0, "--user-id", help="Override target Telegram user_id (0 = use TELEGRAM_USER_ID)."),
) -> None:
    """Send today's top-N scored candidates to Telegram (one-shot)."""
    from givemeasign.telegram.sender import send_deck

    target_user = user_id or None
    result = asyncio.run(send_deck(user_id=target_user, limit=n))
    if result.no_new:
        logger.info("No new candidates to send.")
    else:
        logger.info(f"Sent {result.sent} card(s).")


@app.command("telegram-log")
def telegram_log_cmd(
    action: str = typer.Argument(..., help="on | off | status"),
    min_level: str = typer.Option(
        "INFO", "--min-level", help="Minimum log level to forward when ON (INFO|WARNING|ERROR)."
    ),
) -> None:
    """Toggle Telegram log routing on/off (lives in bot_settings, no restart needed)."""
    from givemeasign.telegram.settings import (
        get_telegram_log_min_level,
        is_telegram_log_enabled,
        set_telegram_log,
    )

    action = action.lower().strip()
    if action == "on":
        set_telegram_log(enabled=True, min_level=min_level)
        logger.info(f"Telegram log routing: ON (min_level={min_level.upper()})")
    elif action == "off":
        set_telegram_log(enabled=False)
        logger.info("Telegram log routing: OFF")
    elif action == "status":
        state = "ON" if is_telegram_log_enabled() else "OFF"
        logger.info(f"Telegram log routing: {state} (min_level={get_telegram_log_min_level()})")
    else:
        logger.error(f"unknown action {action!r}; use one of: on | off | status")
        raise typer.Exit(code=2)


@app.command("usage")
def usage_stats(
    days: int = typer.Option(7, "--days", "-d", help="Window in days."),
) -> None:
    """Aggregate LLM/API spend from usage_log for the last N days."""
    from sqlalchemy import func, select

    from givemeasign.db.models import UsageLog
    from givemeasign.db.session import session_scope

    since = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as s:
        stmt = (
            select(
                UsageLog.stage,
                UsageLog.model,
                func.count().label("calls"),
                func.sum(UsageLog.input_tokens).label("in_tokens"),
                func.sum(UsageLog.output_tokens).label("out_tokens"),
                func.sum(UsageLog.usd_cost).label("cost"),
            )
            .where(UsageLog.created_at >= since)
            .group_by(UsageLog.stage, UsageLog.model)
            .order_by(func.sum(UsageLog.usd_cost).desc())
        )
        rows = s.execute(stmt).all()
        if not rows:
            typer.echo(f"(no usage in the last {days} days)")
            return
        total = 0.0
        typer.echo(
            f"{'stage':<20} {'model':<35} {'calls':>6} {'in_tok':>10} {'out_tok':>10} {'cost':>10}"
        )
        for r in rows:
            cost = float(r.cost or 0)
            typer.echo(
                f"{r.stage:<20} {r.model:<35} {r.calls:>6} "
                f"{r.in_tokens or 0:>10} {r.out_tokens or 0:>10} ${cost:>9.4f}"
            )
            total += cost
        typer.echo(f"{'TOTAL':<62} ${total:>9.4f}")


if __name__ == "__main__":
    app()
