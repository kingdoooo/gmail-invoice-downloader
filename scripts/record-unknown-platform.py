#!/usr/bin/env python3
"""Append an unknown_platform item to missing.json.

Invoked by Agents during the exit-5 auto-probe loop when probe-platform.py
cannot discover a PDF URL for a failed short link. Recomputes
convergence_hash so the next --supplemental iteration doesn't wrongly
declare convergence.

Exit 0 on success. Exit 1 on missing / malformed missing.json.
"""
import argparse
import datetime as _dt
import hashlib
import json
import os
import sys


def _compute_convergence_hash(items):
    """Mirror of scripts/postprocess.py::_compute_convergence_hash.

    Must stay in sync with postprocess's algorithm: sha256 over JSON-encoded
    sorted (type, needed_for) tuples, truncated to 16 hex chars.
    """
    keys = sorted(
        (item.get("type", ""), item.get("needed_for", ""))
        for item in items
    )
    blob = json.dumps(keys, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", required=True,
                    help="Batch output directory (must contain missing.json).")
    ap.add_argument("--url", required=True,
                    help="Original short-link URL that probe could not resolve.")
    ap.add_argument("--email-subject", required=True,
                    help="Gmail subject line of the source email.")
    ap.add_argument("--email-from", required=True,
                    help="Gmail From header of the source email.")
    ap.add_argument("--probe-suggestion", required=True,
                    help="Probe CLI stdout summary / next-step suggestion.")
    args = ap.parse_args()

    mpath = os.path.join(args.output, "missing.json")
    if not os.path.exists(mpath):
        print(
            f"REMEDIATION: {mpath!r} does not exist. Run download-invoices.py "
            f"first to generate missing.json, then retry this helper.",
            file=sys.stderr,
        )
        return 1

    try:
        with open(mpath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(
            f"REMEDIATION: {mpath} is not valid JSON ({e}). Inspect the file "
            f"— do not hand-edit missing.json.",
            file=sys.stderr,
        )
        return 1

    if data.get("schema_version") != "1.0":
        print(
            f"REMEDIATION: missing.json schema_version="
            f"{data.get('schema_version')!r}, this helper only supports 1.0.",
            file=sys.stderr,
        )
        return 1

    item = {
        "type": "unknown_platform",
        "needed_for": args.url,
        "original_url": args.url,
        "email_subject": args.email_subject,
        "email_from": args.email_from,
        "probe_suggestion": args.probe_suggestion,
        "hint": (
            "未知平台，probe 无法自动恢复。按 references/platforms.md 的 "
            "5-step playbook 评估是否扩展支持"
        ),
        "search_suggestion": None,
    }
    data.setdefault("items", []).append(item)
    data["status"] = "user_action_required"
    data["recommended_next_action"] = "ask_user"
    data["convergence_hash"] = _compute_convergence_hash(data["items"])
    data["generated_at"] = _dt.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )

    tmp = mpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, mpath)
    print("OK: appended unknown_platform item, status=user_action_required")
    return 0


if __name__ == "__main__":
    sys.exit(main())
