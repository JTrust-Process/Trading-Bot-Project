"""
finalize_runs.py — finalize any stale `running` rows in bot_runs.

Runs as a post-step in the GitHub Actions workflow with `if: always()`,
so even if the main `python bot.py` step is hard-killed by `timeout-minutes`
(SIGKILL — Python `finally` does not run), the most recent run row is
still closed out for the dashboard.

Strategy:
  - Find the most recent `running` row started in the last 30 minutes
    (this run's row).
  - Mark it as `timeout` if the bot step's outcome was failure/cancelled,
    `success` otherwise.
  - Also sweep older `running` rows (>30 minutes) and mark them `timeout`
    in case a previous run was killed before this fix existed.

Reads BOT_STATUS env var to decide success vs timeout for the most recent row.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, cast

try:
    from supabase import create_client
except ImportError:
    print("[finalize_runs] supabase not installed; skipping")
    sys.exit(0)


def main() -> None:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("[finalize_runs] SUPABASE_URL / SUPABASE_SERVICE_KEY not set; skipping")
        return

    bot_status = os.getenv("BOT_STATUS", "unknown").lower()
    # GitHub `steps.<id>.outcome` values: success, failure, cancelled, skipped
    final_status = "success" if bot_status == "success" else "timeout"

    sb = create_client(url, key)
    now_utc = datetime.now(timezone.utc)
    recent_cutoff = (now_utc - timedelta(minutes=30)).isoformat()
    old_cutoff = recent_cutoff  # anything older than 30 min is "old"

    try:
        # 1. Most-recent running row (this run, probably) → final_status
        recent = (
            sb.table("bot_runs")
            .select("id,start_time")
            .eq("status", "running")
            .gte("start_time", recent_cutoff)
            .order("start_time", desc=True)
            .limit(1)
            .execute()
        )
        recent_rows: List[Dict[str, Any]] = cast(List[Dict[str, Any]], recent.data or [])
        if recent_rows:
            first_row: Dict[str, Any] = recent_rows[0]
            row_id = first_row.get("id")
            if row_id:
                sb.table("bot_runs").update({
                    "status": final_status,
                    "end_time": now_utc.isoformat(),
                    "notes": f"finalized by finalize_runs.py (BOT_STATUS={bot_status})",
                }).eq("id", row_id).execute()
                print(f"[finalize_runs] finalized recent run {row_id} -> {final_status}")
            else:
                print("[finalize_runs] recent row had no id; skipping")
        else:
            print("[finalize_runs] no recent running row found")

        # 2. Any older running rows → timeout (cleanup of historical stuck rows)
        old = (
            sb.table("bot_runs")
            .update({
                "status": "timeout",
                "end_time": now_utc.isoformat(),
                "notes": "swept by finalize_runs.py (older than 30m running)",
            })
            .eq("status", "running")
            .lt("start_time", old_cutoff)
            .execute()
        )
        old_rows: List[Dict[str, Any]] = cast(List[Dict[str, Any]], old.data or [])
        swept = len(old_rows)
        if swept:
            print(f"[finalize_runs] swept {swept} older running rows -> timeout")
    except Exception as e:
        print(f"[finalize_runs] error: {e}")


if __name__ == "__main__":
    main()
