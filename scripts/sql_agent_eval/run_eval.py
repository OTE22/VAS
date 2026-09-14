#!/usr/bin/env python3
"""Run the evaluation corpus against the live SQL agent and report the failure
DISTRIBUTION by category.

    # inside the api container, truth only (fast, no model calls):
    docker exec -w /app -e PYTHONPATH=/app face_recognition_api \
        python scripts/sql_agent_eval/run_eval.py --truth-only

    # on the host, full replay through the streaming endpoint:
    python scripts/sql_agent_eval/run_eval.py --base http://localhost --user <name> --password <pw>

Truth is recomputed from the database on every run, in the REPLAY ACCOUNT's
camera scope, through a direct psycopg2 connection (the guarded manager refuses
`user_pipeline_access`). The point of the run is not a score: it is which STAGE
fails, so effort goes where the failures are.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.sql_agent_eval.corpus import CORPUS  # noqa: E402


# --------------------------------------------------------------------- truth
def scope_for(cur, user_id: int):
    cur.execute("SELECT pipeline_id FROM user_pipeline_access WHERE user_id = %s", (user_id,))
    return [r[0] for r in cur.fetchall()]


def _with_scope(sql: str, listed: str) -> str:
    """Wrap the query so only in-scope detections are visible."""
    return sql.replace(
        "FROM detections d",
        f"FROM (SELECT * FROM detections WHERE pipeline_id IN ({listed})) d"
    ).replace(
        "JOIN detections d ON",
        f"JOIN (SELECT * FROM detections WHERE pipeline_id IN ({listed})) d ON"
    ).replace(
        "JOIN detections d2 ON",
        f"JOIN (SELECT * FROM detections WHERE pipeline_id IN ({listed})) d2 ON"
    ).replace(
        "FROM detections d2",
        f"FROM (SELECT * FROM detections WHERE pipeline_id IN ({listed})) d2"
    )


def compute_truths(user_id: int):
    import psycopg2
    import psycopg2.extras
    from config import settings

    dsn = re.sub(r"^postgresql\+\w+://", "postgresql://", settings.DATABASE_URL)
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    scope = scope_for(cur, user_id)
    dict_cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    truths = []
    for entry in CORPUS:
        row = {"question": entry["question"], "category": entry["category"],
               "check": entry["check"], "note": entry["note"], "rows": None, "error": None}
        if entry["sql"]:
            try:
                dict_cur.execute(_with_scope(entry["sql"], ", ".join(
                    "'" + p.replace("'", "''") + "'" for p in scope)))
                row["rows"] = [dict(r) for r in dict_cur.fetchall()]
            except Exception as e:                       # a corpus bug, not an agent bug
                conn.rollback()
                row["error"] = f"{type(e).__name__}: {str(e).splitlines()[0][:140]}"
        truths.append(row)
    return truths, scope


# ---------------------------------------------------------------- the replay
def login(base, user, password):
    req = urllib.request.Request(base.rstrip("/") + "/api/auth/login",
                                 data=json.dumps({"username": user, "password": password}).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    return json.load(urllib.request.urlopen(req, timeout=60))["access_token"]


def ask(base, token, question, timeout=900):
    urllib.request.urlopen(urllib.request.Request(
        base.rstrip("/") + "/api/sql-agent/session/new", data=b"{}", method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}),
        timeout=60).read()
    req = urllib.request.Request(base.rstrip("/") + "/api/sql-agent/query/stream",
                                 data=json.dumps({"query": question}).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    final, acc = None, []
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            if ev.get("type") in ("token", "word"):
                acc.append(str(ev.get("content") or ev.get("word") or ""))
            elif ev.get("type") == "complete":
                final = ev.get("response") or "".join(acc)
    return " ".join(str(final or "").split()), time.time() - started


# --------------------------------------------------------------- the scoring
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers(text):
    return {n.rstrip(".0") or "0" for n in _NUM.findall(str(text))}


def _contains_number(answer, expected):
    """Accept an exact number or a more precise rendering of a decimal truth."""
    expected_text = str(expected)
    candidates = _NUM.findall(str(answer))
    if "." not in expected_text:
        return expected_text in _numbers(answer)
    places = len(expected_text.rstrip("0").split(".", 1)[1])
    target = float(expected)
    return any(math.isclose(round(float(candidate), places), target,
                            rel_tol=0.0, abs_tol=10 ** -(places + 1))
               for candidate in candidates)


def _normal_text(value):
    return " ".join(str(value).split()).casefold()


def judge(entry, truth_rows, answer):
    """Did the reply carry the figures the truth says it must?

    Deliberately blunt: an answer that omits the number is wrong however well
    it reads, and an answer carrying the right number is not marked wrong for
    phrasing. Anything ambiguous is reported for a human to label.
    """
    if not entry["check"]:
        return "REVIEW", "no automatic check: read it and label the category"
    if truth_rows is None:
        return "CORPUS_ERROR", "the truth query itself failed"
    if not truth_rows:
        return "REVIEW", "truth returned no rows"
    wanted, missing = [], []
    for column in entry["check"]:
        value = truth_rows[0].get(column)
        if value is None:
            continue
        wanted.append(f"{column}={value}")
        text = str(value)
        if isinstance(value, (int, float)) or _NUM.fullmatch(text):
            if not _contains_number(answer, value):
                missing.append(f"{column}={value}")
        elif _normal_text(text) not in _normal_text(answer):
            missing.append(f"{column}={value}")
    if missing:
        return "FAIL", "missing " + ", ".join(missing) + " | expected " + ", ".join(wanted)
    return "PASS", ", ".join(wanted)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--truth-only", action="store_true",
                    help="compute and print the truths; ask the agent nothing")
    ap.add_argument("--base", default="http://localhost")
    ap.add_argument("--user", default=os.environ.get("EVAL_USER", ""))
    ap.add_argument("--password", default=os.environ.get("EVAL_PASSWORD", ""))
    ap.add_argument("--user-id", type=int, default=int(os.environ.get("EVAL_USER_ID", "3976")))
    ap.add_argument("--out", default="")
    ap.add_argument("--truths-to", default="",
                    help="write the computed truths here and stop (run inside the container)")
    ap.add_argument("--truths-from", default="",
                    help="read truths from this file instead of the database (run on the host)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start", type=int, default=1,
                    help="one-based corpus item to start at (useful after an interrupted run)")
    args = ap.parse_args(argv)

    if args.truths_from:
        with open(args.truths_from, encoding="utf-8") as fh:
            payload = json.load(fh)
        truths, scope = payload["truths"], payload["scope"]
    else:
        truths, scope = compute_truths(args.user_id)
    print(f"corpus: {len(CORPUS)} questions | replay scope: {len(scope)} cameras")
    if args.truths_to:
        with open(args.truths_to, "w", encoding="utf-8") as fh:
            json.dump({"truths": truths, "scope": scope}, fh, indent=1, default=str)
        print(f"  wrote {args.truths_to}")
        return 0
    broken = [t for t in truths if t["error"]]
    if broken:
        print(f"\n{len(broken)} corpus queries are themselves broken — fix these first:")
        for t in broken:
            print(f"  [{t['category']}] {t['question'][:64]}\n      {t['error']}")
    if args.truth_only:
        for t in truths:
            if t["error"] or not t["rows"]:
                continue
            print(f"  [{t['category']:13s}] {t['question'][:62]:64s} {json.dumps(t['rows'][0], default=str)[:90]}")
        return 0 if not broken else 1

    token = login(args.base, args.user, args.password)
    results, by_category = [], defaultdict(Counter)
    entries = list(zip(CORPUS, truths))
    stop = (args.start - 1 + args.limit) if args.limit else None
    entries = entries[max(0, args.start - 1):stop]
    for i, (entry, truth) in enumerate(entries, max(1, args.start)):
        try:
            answer, took = ask(args.base, token, entry["question"])
        except urllib.error.HTTPError as e:
            answer, took = f"<HTTP {e.code}>", 0.0
        except Exception as e:
            answer, took = f"<{type(e).__name__}: {str(e)[:160]}>", 0.0
        if answer.startswith("<") and answer.endswith(">"):
            verdict, detail = "TRANSPORT_ERROR", answer[1:-1]
        else:
            verdict, detail = judge(entry, truth["rows"], answer)
        by_category[entry["category"]][verdict] += 1
        results.append({**{k: entry[k] for k in ("question", "category", "note")},
                        "verdict": verdict, "detail": detail,
                        "answer": answer[:600], "seconds": round(took, 1)})
        print(f"[{i:02d}/{len(entries)}] {verdict:12s} {entry['category']:13s} "
              f"{took:5.1f}s  {entry['question'][:56]}")
        if verdict in ("FAIL", "REVIEW"):
            print(f"        {detail}")
            print(f"        answer: {answer[:220]}")
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(results, fh, indent=1, ensure_ascii=False)

    print("\n================ DISTRIBUTION BY CATEGORY ================")
    print(f"  {'category':14s} {'pass':>5s} {'fail':>5s} {'review':>7s}")
    for category in sorted(by_category):
        c = by_category[category]
        print(f"  {category:14s} {c['PASS']:5d} {c['FAIL']:5d} {c['REVIEW']:7d}")
    total = Counter(r["verdict"] for r in results)
    print(f"\n  TOTAL pass={total['PASS']} fail={total['FAIL']} review={total['REVIEW']} "
          f"corpus_error={total['CORPUS_ERROR']}")
    if args.out:
        print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
