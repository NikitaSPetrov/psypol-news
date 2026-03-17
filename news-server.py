#!/usr/bin/env python3
"""
news-server.py — PsyPol news dashboard: scan, filter, triage.

Replaces news-triage.py with integrated RSS scanning and Claude API filtering.

Endpoints:
  GET  /               → triage UI
  GET  /api/candidates → candidates.json (empty structure if none exists)
  GET  /api/archive    → published + rejected stories from bulletin.md
  POST /api/scan       → run news-scan.py, return summary
  POST /api/filter     → filter scan.json via Claude API, write candidates.json
  POST /api/scan-and-filter → scan + filter in one call
  POST /api/review     → process editor feedback via Claude API
  POST /api/save       → save triage decisions to triage.json

Requirements:
  pip install anthropic
  export ANTHROPIC_API_KEY=sk-...

Usage:
  python news-server.py              # serve on port 8080
  python news-server.py --port 9000  # custom port
"""

import http.server
import json
import os
import socketserver
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CANDIDATES = SCRIPT_DIR / "candidates.json"
SCAN_JSON = SCRIPT_DIR / "scan.json"
TRIAGE_OUT = SCRIPT_DIR / "triage.json"
TRIAGE_HTML = SCRIPT_DIR / "triage.html"
SCAN_SCRIPT = SCRIPT_DIR / "news-scan.py"
EDITORIAL_LESSONS = Path.home() / ".claude" / "skills" / "news" / "editorial-lessons.md"
BULLETIN_MD = Path.home() / ".claude" / "skills" / "news" / "bulletin.md"

PORT = 8080
MODEL = os.environ.get("PSYPOL_MODEL", "claude-opus-4-6")


def _read_existing_stories() -> str:
    """Extract story IDs and headlines from bulletin.md for dedup."""
    if not BULLETIN_MD.exists():
        return ""
    lines = []
    for line in BULLETIN_MD.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- `") and "` " in line:
            # Format: - `slug` — headline — Source
            parts = line.split("`", 2)
            if len(parts) >= 3:
                slug = parts[1]
                rest = parts[2].lstrip(" —\u2014")
                headline = rest.split(" — ")[0] if " — " in rest else rest
                lines.append(f"- {slug}: {headline}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Filter prompt (extracted from SKILL.md)
# ---------------------------------------------------------------------------

FILTER_SYSTEM = """\
You are a news curator for Psychopolitica. You filter RSS scan results through \
the Psychopolitica lens: stories where the machinery of reality construction \
becomes visible, or where reality itself feels strange, surreal, and psychedelic.

## The Filter

Select 5–20 stories. The filter has two legs:

1. **Machinery of reality construction** — the apparatus becomes visible \
(propaganda mid-operation, algorithmic control exposed, the real/fabricated \
boundary moving).
2. **The absurdity, humor, and surreality of existence itself** — reality \
feels strange, magical, or psychedelic. Not commentary about strangeness — \
the strangeness is in the facts themselves.

The desired effect: "reading the news while on LSD." Hard facts curated to \
highlight how weird, surreal, trippy day-to-day reality is.

## The Literary Test

A PsyPol story reads like it could appear in a novel by:
- **Pelevin** — simulation flickering, ideology as hallucination, power as absurdist theater
- **Bulgakov** — the devil visiting Moscow, bureaucracy as supernatural horror
- **Gogol** — petty officials, human comedy, the state as farce
- **Philip K. Dick** — what is real, android dreams, manufactured memory
- **Stephen King** — ordinary reality cracking open, horror underneath

If the facts themselves read like fiction, it's in.

## What We're Looking For

- **Infowars / machinery of power** — structural mechanics of propaganda or \
reality-fabrication briefly exposed through a specific event.
- **Reality glitches** — real/fake boundary collapsing, deepfakes with \
consequences, AI content passing as real.
- **Agency and control** — surveillance, algorithmic control, behavioral \
manipulation. Also people finding autonomy outside the system.
- **The absurdist angle** — dark comedy, satire, Gogol/Pelevin quality.
- **Science fiction becoming real** — biotech, neurotech, consciousness \
research, anything shifting what "human" means. Core PsyPol — don't underweight.
- **Nature and deep history** — how recently we appeared, how little we know, \
how weird the world is through hard facts. Core PsyPol, not filler.
- **The world** — geopolitics where something structurally strange is happening, \
or events too big to ignore.
- **Regional portraits** — Russia, China, India, Africa accumulating character \
through curation.
- **Fringe going mainstream** — psychedelics in clinics, esoteric ideas \
becoming respectable.

## What We're NOT Looking For

- Standard breaking news with no deeper angle (except major events like a new war)
- Stories fitting cleanly into left/right framing
- Tech hype or doom without structural element ("CEO predicts AI replaces all jobs")
- Celebrity, sports, markets (unless structurally strange)
- Outrage bait
- Obituaries and expected political gestures
- Rational, expected institutional behavior ("FBI surveils activists" = policy brief, not PsyPol)

## The Shrug Test

If a reader's reaction is "yeah, that figures," it fails. PsyPol stories make \
you stop — not because they're shocking, but because they're *revealing*.

Failure modes:
1. **Vague structural claim**: "X has too much power" — needs a specific event.
2. **Unclear sourcing**: Can't tell who's doing what to whom.
3. **Expected behavior**: The machinery running as expected isn't a story. \
A governor investigating a citizen's "internal resentment" after being refused \
a ride is Gogol. FBI surveilling activists is a policy brief.

## Headlines

- State what happened, don't editorialize. If you have to sell it, it doesn't belong.
- Descriptive enough that a reader gets the story without clicking.
- Rewrite boring RSS headlines to be precise and vivid.
- If the source headline is already vivid and accurate, keep it.
- Lead with the frame, then the action.
- No decorative emdashes as conjunction substitutes.
- Write for a non-US audience.

## Output

For each selected story, provide:
- **id**: short slug (lowercase, hyphens, e.g. "kim-daughter", "robot-vacuum")
- **headline**: rewritten if the original is boring or unclear; kept if already good
- **notes**: 1-2 sentences on why this fits and which criteria it matches
- **featured**: true only for absolute must-reads (2-4 per batch)

Order by strength (strongest first). Select 5-20 stories — quality over quantity.\
"""


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class DashboardHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._serve_file(TRIAGE_HTML, "text/html")
        elif self.path == "/api/candidates":
            if CANDIDATES.exists():
                self._serve_file(CANDIDATES, "application/json")
            else:
                self._json_response({
                    "date": "", "selected": [], "candidates": []
                })
        elif self.path == "/api/archive":
            self._handle_archive()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/scan":
            self._handle_scan()
        elif self.path == "/api/filter":
            self._handle_filter()
        elif self.path == "/api/scan-and-filter":
            self._handle_scan_and_filter()
        elif self.path == "/api/review":
            self._handle_review()
        elif self.path == "/api/save":
            self._handle_save()
        elif self.path == "/api/build":
            self._handle_build()
        else:
            self.send_error(404)

    # ── Scan ──────────────────────────────────────────────────────────

    def _handle_scan(self):
        """Run news-scan.py as subprocess, return summary."""
        if not SCAN_SCRIPT.exists():
            self._json_response({
                "ok": False, "error": f"news-scan.py not found at {SCAN_SCRIPT}"
            }, 500)
            return

        try:
            result = subprocess.run(
                [sys.executable, str(SCAN_SCRIPT)],
                capture_output=True, text=True, timeout=120,
                cwd=str(SCRIPT_DIR),
            )
            if result.returncode != 0:
                self._json_response({
                    "ok": False,
                    "error": result.stderr.strip() or "Scan failed",
                }, 500)
                return

            scan_data = json.loads(SCAN_JSON.read_text(encoding="utf-8"))
            self._json_response({
                "ok": True,
                "feeds_fetched": scan_data.get("feeds_fetched", 0),
                "feeds_total": scan_data.get("feeds_total", 0),
                "total_items": scan_data.get("total_items", 0),
                "new_count": scan_data.get("new_count", 0),
                "seen_count": scan_data.get("seen_count", 0),
                "duplicates_removed": scan_data.get("duplicates_removed", 0),
            })
        except subprocess.TimeoutExpired:
            self._json_response({"ok": False, "error": "Scan timed out (120s)"}, 500)
        except Exception as exc:
            traceback.print_exc()
            self._json_response({"ok": False, "error": str(exc)}, 500)

    # ── Filter ────────────────────────────────────────────────────────

    def _handle_filter(self):
        """Filter scan results via Claude API."""
        try:
            import anthropic
        except ImportError:
            self._json_response({
                "ok": False,
                "error": "anthropic not installed. Run: pip install anthropic",
            }, 500)
            return

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            self._json_response({
                "ok": False,
                "error": "ANTHROPIC_API_KEY environment variable not set",
            }, 500)
            return

        if not SCAN_JSON.exists():
            self._json_response({
                "ok": False, "error": "No scan.json — run scan first"
            }, 400)
            return

        try:
            scan_data = json.loads(SCAN_JSON.read_text(encoding="utf-8"))
            new_items = [i for i in scan_data["items"] if i["status"] == "new"]

            if not new_items:
                self._json_response({
                    "ok": False, "error": "No new items in scan"
                }, 400)
                return

            # Build system prompt
            system_prompt = FILTER_SYSTEM

            # Dedup: inject existing stories so the model skips them
            existing = _read_existing_stories()
            if existing:
                system_prompt += (
                    "\n\n## Already on the site (DO NOT recommend these)\n\n"
                    "These stories are already published. Skip any scan item "
                    "that covers the same event, even if the headline differs:\n\n"
                    + existing
                )

            if EDITORIAL_LESSONS.exists():
                try:
                    lessons = EDITORIAL_LESSONS.read_text(encoding="utf-8")
                    system_prompt += (
                        "\n\n## Editorial Lessons (from past triage)\n\n"
                        + lessons
                    )
                except Exception:
                    pass  # non-critical

            # Build user message — numbered list of items
            lines = []
            for i, item in enumerate(new_items):
                line = f"{i}. [{item['source_name']}] {item['headline']}"
                if item.get("headline_ru"):
                    line += f" ({item['headline_ru']})"
                lines.append(line)
            items_text = "\n".join(lines)

            user_message = (
                f"Here are {len(new_items)} new items from today's RSS scan. "
                f"Select 5–20 that fit the Psychopolitica filter.\n\n"
                f"Reference items by their index number (0-based).\n\n"
                f"{items_text}"
            )

            # Tool definition for structured output
            tool = {
                "name": "submit_filtered_stories",
                "description": "Submit the filtered stories",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "selected": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {
                                        "type": "integer",
                                        "description": "0-based index in the scan items",
                                    },
                                    "id": {
                                        "type": "string",
                                        "description": "Short slug (lowercase, hyphens)",
                                    },
                                    "headline": {
                                        "type": "string",
                                        "description": "Headline (rewritten if needed)",
                                    },
                                    "notes": {
                                        "type": "string",
                                        "description": "Why this fits the filter",
                                    },
                                    "featured": {
                                        "type": "boolean",
                                        "description": "Recommend featuring?",
                                    },
                                },
                                "required": [
                                    "index", "id", "headline", "notes", "featured"
                                ],
                            },
                        },
                    },
                    "required": ["selected"],
                },
            }

            print(f"  Calling Claude API ({MODEL}) with {len(new_items)} items...")
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
                tools=[tool],
                tool_choice={"type": "tool", "name": "submit_filtered_stories"},
            )

            # Parse tool response
            tool_input = None
            for block in response.content:
                if block.type == "tool_use":
                    tool_input = block.input
                    break

            if not tool_input or "selected" not in tool_input:
                self._json_response({
                    "ok": False,
                    "error": "Claude didn't return structured output",
                }, 500)
                return

            raw_selected = tool_input["selected"]
            selected_indices = set()

            # Build selected stories
            selected = []
            for i, s in enumerate(raw_selected):
                idx = s.get("index", -1)
                if idx < 0 or idx >= len(new_items):
                    print(f"  WARNING: index {idx} out of range, skipping")
                    continue
                selected_indices.add(idx)
                item = new_items[idx]
                selected.append({
                    "id": s["id"],
                    "headline": s["headline"],
                    "headline_ru": item.get("headline_ru", ""),
                    "original_headline": (
                        item["headline"]
                        if s["headline"] != item["headline"]
                        else ""
                    ),
                    "source_name": item["source_name"],
                    "source_url": item["source_url"],
                    "pub_date": item.get("pub_date", ""),
                    "featured": s.get("featured", False),
                    "status": "pending",
                    "order": i,
                    "notes": s.get("notes", ""),
                    "editor_notes": "",
                })

            # Remaining items become candidates
            candidates = []
            for i, item in enumerate(new_items):
                if i in selected_indices:
                    continue
                candidates.append({
                    "id": "",
                    "headline": item["headline"],
                    "headline_ru": item.get("headline_ru", ""),
                    "source_name": item["source_name"],
                    "source_url": item["source_url"],
                    "pub_date": item.get("pub_date", ""),
                    "status": "pending",
                    "editor_notes": "",
                    "notes": "",
                })

            output = {
                "date": scan_data.get("date", datetime.now().strftime("%Y-%m-%d")),
                "feeds_fetched": scan_data.get("feeds_fetched", 0),
                "total_items": scan_data.get("total_items", 0),
                "new_count": scan_data.get("new_count", 0),
                "selected": selected,
                "candidates": candidates,
            }

            CANDIDATES.write_text(
                json.dumps(output, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            print(
                f"  Filter complete: {len(selected)} selected, "
                f"{len(candidates)} candidates"
            )
            self._json_response({
                "ok": True,
                "selected_count": len(selected),
                "candidates_count": len(candidates),
                "model": MODEL,
            })

        except Exception as exc:
            traceback.print_exc()
            self._json_response({"ok": False, "error": str(exc)}, 500)

    # ── Scan + Filter (combined) ─────────────────────────────────────

    def _handle_scan_and_filter(self):
        """Run scan then filter in one call."""
        # Step 1: scan
        if not SCAN_SCRIPT.exists():
            self._json_response({
                "ok": False, "error": f"news-scan.py not found"
            }, 500)
            return

        try:
            result = subprocess.run(
                [sys.executable, str(SCAN_SCRIPT)],
                capture_output=True, text=True, timeout=120,
                cwd=str(SCRIPT_DIR),
            )
            if result.returncode != 0:
                self._json_response({
                    "ok": False,
                    "error": "Scan failed: " + (result.stderr.strip() or "unknown"),
                }, 500)
                return

            print("  Scan complete, starting filter...")
        except subprocess.TimeoutExpired:
            self._json_response({"ok": False, "error": "Scan timed out"}, 500)
            return
        except Exception as exc:
            self._json_response({"ok": False, "error": str(exc)}, 500)
            return

        # Step 2: filter (reuse existing method logic)
        self._handle_filter()

    # ── Review (process editor feedback) ──────────────────────────────

    def _handle_review(self):
        """Process editor notes via Claude API."""
        try:
            import anthropic
        except ImportError:
            self._json_response({
                "ok": False, "error": "anthropic not installed",
            }, 500)
            return

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            self._json_response({
                "ok": False, "error": "ANTHROPIC_API_KEY not set",
            }, 500)
            return

        # Read current candidates and save the incoming triage state first
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if body:
            try:
                incoming = json.loads(body)
                # Update candidates.json with current triage state
                CANDIDATES.write_text(
                    json.dumps(incoming, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass

        if not CANDIDATES.exists():
            self._json_response({
                "ok": False, "error": "No candidates.json"
            }, 400)
            return

        try:
            cdata = json.loads(CANDIDATES.read_text(encoding="utf-8"))

            # Find stories with editor notes that need processing
            to_review = []
            for s in cdata.get("selected", []):
                notes = (s.get("editor_notes") or "").strip()
                if notes:
                    to_review.append(s)

            if not to_review:
                self._json_response({
                    "ok": True,
                    "message": "No editor notes to process",
                    "reviewed": 0,
                })
                return

            # Build prompt for Claude
            review_items = []
            for s in to_review:
                entry = (
                    f"ID: {s.get('id', '?')}\n"
                    f"Headline: {s.get('headline', '')}\n"
                    f"Source: {s.get('source_name', '')} — {s.get('source_url', '')}\n"
                    f"Status: {s.get('status', 'pending')}\n"
                    f"Editor notes: {s.get('editor_notes', '')}"
                )
                review_items.append(entry)

            review_text = "\n\n---\n\n".join(review_items)

            system = (
                "You are an editorial assistant for Psychopolitica news. "
                "The editor has left notes on stories. Process each one:\n"
                "- If the editor asks a question, answer it concisely.\n"
                "- If the editor requests an edit (headline rewrite, etc), "
                "provide the corrected version.\n"
                "- If the editor says 'shrug' or rejects with a brief note, "
                "just acknowledge.\n"
                "- If the editor asks you to check something (URL, source, "
                "duplicate), do your best to verify.\n\n"
                "Respond using the tool with one entry per story."
            )

            tool = {
                "name": "submit_reviews",
                "description": "Submit review responses for each story",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reviews": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "description": "Story ID",
                                    },
                                    "response": {
                                        "type": "string",
                                        "description": "Response to editor notes",
                                    },
                                    "updated_headline": {
                                        "type": "string",
                                        "description": "New headline if edited, empty otherwise",
                                    },
                                    "recommended_status": {
                                        "type": "string",
                                        "description": "Suggested status change (or empty to keep current)",
                                        "enum": ["", "accepted", "rejected", "maybe", "pending"],
                                    },
                                },
                                "required": ["id", "response"],
                            },
                        },
                    },
                    "required": ["reviews"],
                },
            }

            print(f"  Review: processing {len(to_review)} stories with editor notes...")
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=system,
                messages=[{
                    "role": "user",
                    "content": f"Process these {len(to_review)} stories:\n\n{review_text}",
                }],
                tools=[tool],
                tool_choice={"type": "tool", "name": "submit_reviews"},
            )

            # Parse response
            tool_input = None
            for block in response.content:
                if block.type == "tool_use":
                    tool_input = block.input
                    break

            reviews_by_id = {}
            if tool_input and "reviews" in tool_input:
                for r in tool_input["reviews"]:
                    reviews_by_id[r["id"]] = r

            # Apply reviews to candidates
            for s in cdata.get("selected", []):
                sid = s.get("id", "")
                if sid in reviews_by_id:
                    r = reviews_by_id[sid]
                    # Store AI response
                    s["ai_response"] = r.get("response", "")
                    # Apply headline update if provided
                    if r.get("updated_headline"):
                        if not s.get("original_headline"):
                            s["original_headline"] = s["headline"]
                        s["headline"] = r["updated_headline"]
                    # Archive editor notes into history
                    if s.get("editor_notes"):
                        if "notes_history" not in s:
                            s["notes_history"] = []
                        s["notes_history"].append({
                            "editor": s["editor_notes"],
                            "ai": r.get("response", ""),
                        })
                        s["editor_notes"] = ""

            # Write back
            CANDIDATES.write_text(
                json.dumps(cdata, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            print(f"  Review complete: {len(reviews_by_id)} stories processed")
            self._json_response({
                "ok": True,
                "reviewed": len(reviews_by_id),
                "model": MODEL,
            })

        except Exception as exc:
            traceback.print_exc()
            self._json_response({"ok": False, "error": str(exc)}, 500)

    # ── Archive ───────────────────────────────────────────────────────

    def _handle_archive(self):
        """Return published + rejected stories."""
        published = []
        rejected = []

        # Published from bulletin.md
        if BULLETIN_MD.exists():
            current_section = ""
            for line in BULLETIN_MD.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("**") and stripped.endswith("**"):
                    current_section = stripped.strip("*").strip(": ")
                elif stripped.startswith("- `") and "` " in stripped:
                    parts = stripped.split("`", 2)
                    if len(parts) >= 3:
                        slug = parts[1]
                        rest = parts[2].lstrip(" —\u2014")
                        # Parse "headline — Source" or "◆ headline — Source"
                        featured = "◆" in rest
                        rest = rest.replace("◆ ", "").replace("◆", "")
                        if " — " in rest:
                            headline, source = rest.rsplit(" — ", 1)
                        else:
                            headline, source = rest, ""
                        published.append({
                            "id": slug,
                            "headline": headline.strip(),
                            "source": source.strip(),
                            "section": current_section,
                            "featured": featured,
                        })

        # Rejected from candidates.json
        if CANDIDATES.exists():
            try:
                cdata = json.loads(CANDIDATES.read_text(encoding="utf-8"))
                for s in cdata.get("selected", []):
                    if s.get("status") == "rejected":
                        rejected.append({
                            "id": s.get("id", ""),
                            "headline": s.get("headline", ""),
                            "source": s.get("source_name", ""),
                            "notes": s.get("editor_notes", ""),
                            "ai_notes": s.get("notes", ""),
                        })
            except Exception:
                pass

        self._json_response({
            "published": published,
            "rejected": rejected,
        })

    # ── Save ──────────────────────────────────────────────────────────

    def _handle_save(self):
        """Save triage decisions to triage.json."""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
            TRIAGE_OUT.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            self._json_response({"ok": True})
            print(f"  Saved triage decisions → {TRIAGE_OUT}")
        except Exception as exc:
            self._json_response({"ok": False, "error": str(exc)}, 500)

    # ── Build (git commit + push) ───────────────────────────────────

    def _handle_build(self):
        """Commit and push the PsyPol news site."""
        repo_dir = SCRIPT_DIR
        try:
            # Stage site files
            subprocess.run(
                ["git", "add", "index.html", "reality.html", "style.css",
                 "editorial.html", "editorial.css", "img/"],
                cwd=str(repo_dir), check=True,
                capture_output=True, text=True,
            )
            # Check if there's anything to commit
            status = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=str(repo_dir), capture_output=True,
            )
            if status.returncode == 0:
                self._json_response({
                    "ok": True, "message": "Nothing to commit — site is up to date",
                })
                return

            # Commit
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            subprocess.run(
                ["git", "commit", "-m", f"PsyPol news update {date_str}"],
                cwd=str(repo_dir), check=True,
                capture_output=True, text=True,
            )
            # Push
            result = subprocess.run(
                ["git", "push"],
                cwd=str(repo_dir), check=True,
                capture_output=True, text=True, timeout=30,
            )
            print(f"  Build: committed and pushed")
            self._json_response({
                "ok": True, "message": "Committed and pushed to production",
            })
        except subprocess.CalledProcessError as exc:
            err = exc.stderr.strip() if exc.stderr else str(exc)
            self._json_response({"ok": False, "error": err}, 500)
        except Exception as exc:
            self._json_response({"ok": False, "error": str(exc)}, 500)

    # ── Helpers ───────────────────────────────────────────────────────

    def _serve_file(self, path: Path, content_type: str):
        if not path.exists():
            self.send_error(404, f"Not found: {path.name}")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _json_response(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else ""
        if "POST" in str(args[0]) or str(status) != "200":
            super().log_message(fmt, *args)


# ---------------------------------------------------------------------------
# Threaded server (handles concurrent requests during long operations)
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    port = PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        port = int(sys.argv[idx + 1])

    api_key = os.environ.get("ANTHROPIC_API_KEY")

    print("PsyPol News Dashboard")
    print(f"  http://localhost:{port}")
    print(f"  Model: {MODEL}")
    if api_key:
        print(f"  API key: ...{api_key[-8:]}")
    else:
        print("  \u26a0 ANTHROPIC_API_KEY not set — filter disabled")
    print()
    print("Press Ctrl+C to stop.\n")

    server = ThreadedHTTPServer(("127.0.0.1", port), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
