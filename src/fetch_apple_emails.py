"""
fetch_apple_emails.py
─────────────────────
Fetches emails from Apple Mail via direct osascript (JXA) calls — no MCP
server required.  Processes emails in configurable batches, shows a live
tqdm progress bar, and writes results to a CSV file.

Usage:
    python src/fetch_apple_emails.py
"""

import csv
import json
import subprocess
import sys
from datetime import datetime, timedelta
from email.utils import parseaddr
from pathlib import Path

from tqdm import tqdm

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
OUTPUT_CSV = "data/recent_emails.csv"
LOOKBACK_HOURS = 240         # how many hours back to keep emails
BATCH_SIZE = 20               # how many email *bodies* to fetch in parallel per tqdm tick
MAILBOX = "INBOX"             # Apple Mail mailbox to query
ACCOUNT = None                # None → use first / default account
CSV_COLUMNS = ["date", "sender_name", "sender_email", "subject", "body"]

# ──────────────────────────────────────────────
# Embedded MailCore JS  (extracted from apple_mail_mcp)
# ──────────────────────────────────────────────
_MAIL_CORE_JS = """
const Mail = Application("Mail");

const MailCore = {
    getAccount(name) {
        if (name) { return Mail.accounts.byName(name); }
        const accounts = Mail.accounts();
        if (accounts.length === 0) { throw new Error("No mail accounts configured"); }
        return accounts[0];
    },
    getMailbox(account, name) { return account.mailboxes.byName(name); },
    batchFetch(msgs, props) {
        const result = {};
        for (const prop of props) { result[prop] = msgs[prop](); }
        return result;
    },
    formatDate(date) {
        if (!date || !(date instanceof Date)) return null;
        return date.toISOString();
    },
};
"""


# ──────────────────────────────────────────────
# Low-level JXA execution
# ──────────────────────────────────────────────

class JXAError(Exception):
    """Raised when an osascript/JXA call fails."""


def run_jxa(script: str, timeout: int = 120) -> str:
    """
    Execute a JXA script via `osascript -l JavaScript` and return stdout.

    Args:
        script:  JavaScript source code.
        timeout: Maximum seconds to wait.

    Returns:
        Stripped stdout string from osascript.

    Raises:
        JXAError: if osascript exits with a non-zero return code.
    """
    result = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise JXAError(f"JXA script failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def run_jxa_with_core(script_body: str, timeout: int = 120) -> object:
    """
    Execute a JXA script body with MailCore injected, returning parsed JSON.

    The script_body should end with a JSON.stringify(...) call so that the
    result can be parsed back into Python objects.

    Args:
        script_body: JavaScript code that uses MailCore.
        timeout:     Maximum seconds to wait.

    Returns:
        Python object parsed from the JSON output.
    """
    full_script = f"{_MAIL_CORE_JS}\n\n{script_body}"
    raw = run_jxa(full_script, timeout=timeout)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        preview = raw[:500] + "..." if len(raw) > 500 else raw
        raise JXAError(f"Cannot parse JXA output as JSON: {exc}\n{preview}") from exc


# ──────────────────────────────────────────────
# EmailFetcher class
# ──────────────────────────────────────────────

class EmailFetcher:
    """
    Fetches emails from Apple Mail using direct JXA / osascript calls.

    Features
    ────────
    * No MCP server dependency — talks directly to Mail.app via JXA.
    * Batch body fetching: fetches metadata in one shot, then retrieves
      bodies in configurable batches to avoid overloading the event bridge.
    * tqdm progress bar over the body-fetch phase.
    * Writes results to a CSV in the correct column format.

    Args:
        mailbox:       Apple Mail mailbox name (default: ``"INBOX"``).
        account:       Account name, or ``None`` for the default account.
        lookback_hours: Only keep emails received within this window.
        batch_size:    Number of email bodies to fetch per progress tick.
        output_csv:    Path of the output CSV file.
    """

    def __init__(
        self,
        mailbox: str = MAILBOX,
        account: str | None = ACCOUNT,
        lookback_hours: int = LOOKBACK_HOURS,
        batch_size: int = BATCH_SIZE,
        output_csv: str = OUTPUT_CSV,
    ):
        self.mailbox = mailbox
        self.account = account
        self.lookback_hours = lookback_hours
        self.batch_size = batch_size
        self.output_csv = Path(output_csv)

    # ── private helpers ──────────────────────────────────────────────

    def _fetch_email_summaries(self) -> list[dict]:
        """
        Fetch metadata (id, subject, sender, date_received) for ALL messages
        in the mailbox in a single batched JXA call.

        Returns:
            List of dicts with keys: id, subject, sender, date_received.
        """
        account_json = json.dumps(self.account)
        mailbox_json = json.dumps(self.mailbox)

        script = f"""
const account = MailCore.getAccount({account_json});
const mailbox = MailCore.getMailbox(account, {mailbox_json});
const msgs = mailbox.messages;

const data = MailCore.batchFetch(msgs, ["id", "subject", "sender", "dateReceived"]);

const results = [];
const len = data.id.length;
for (let i = 0; i < len; i++) {{
    results.push({{
        id: data.id[i],
        subject: data.subject[i],
        sender: data.sender[i],
        date_received: MailCore.formatDate(data.dateReceived[i]),
    }});
}}

// Newest first
results.sort((a, b) => (a.date_received < b.date_received ? 1 : -1));

JSON.stringify(results);
"""
        return run_jxa_with_core(script, timeout=300)

    def _fetch_bodies_batch(self, emails: list[dict]) -> dict[int, str]:
        """
        Fetch email bodies for an entire batch in **one single JXA call**.

        The JXA script resolves the ID→index mapping once (one IPC round-trip
        for the full ``mailbox.messages.id()`` array), then reads each body by
        index — avoiding the O(n²) re-scan that caused the timeout.

        Args:
            emails: List of email summary dicts (must contain ``"id"`` key).

        Returns:
            Dict mapping email_id → body string.
        """
        if not emails:
            return {}

        account_json = json.dumps(self.account)
        mailbox_json = json.dumps(self.mailbox)
        ids_json = json.dumps([int(e["id"]) for e in emails])

        script = f"""
const account = MailCore.getAccount({account_json});
const mailbox = MailCore.getMailbox(account, {mailbox_json});

// Fetch all message IDs once — single IPC round-trip
const allIds = mailbox.messages.id();

const targetIds = {ids_json};
const results = {{}};

for (let t = 0; t < targetIds.length; t++) {{
    const tid = targetIds[t];
    const idx = allIds.indexOf(tid);
    if (idx === -1) {{
        results[String(tid)] = "";
    }} else {{
        try {{
            results[String(tid)] = mailbox.messages[idx].content() || "";
        }} catch (e) {{
            results[String(tid)] = "";
        }}
    }}
}}

JSON.stringify(results);
"""
        try:
            # Timeout scales with batch size — allow up to 30 s per email in the batch
            timeout = max(120, len(emails) * 30)
            raw = run_jxa_with_core(script, timeout=timeout)
            # Keys come back as strings; convert to int
            return {int(k): v for k, v in raw.items()}
        except (JXAError, Exception) as exc:
            print(f"\n  [warn] Batch body fetch failed: {exc}", file=sys.stderr)
            return {int(e["id"]): "" for e in emails}

    def _filter_by_lookback(self, summaries: list[dict]) -> list[dict]:
        """
        Keep only emails received within the configured lookback window.

        Args:
            summaries: List of email summary dicts from ``_fetch_email_summaries``.

        Returns:
            Filtered list.
        """
        cutoff = datetime.now().astimezone() - timedelta(hours=self.lookback_hours)
        kept = []
        for email in summaries:
            date_str = email.get("date_received")
            if not date_str:
                continue
            try:
                if date_str.endswith("Z"):
                    date_str = date_str[:-1] + "+00:00"
                email_dt = datetime.fromisoformat(date_str)
                if email_dt > cutoff:
                    kept.append(email)
            except ValueError:
                continue
        return kept

    # ── public API ───────────────────────────────────────────────────

    def fetch(self) -> list[dict]:
        """
        Full fetch pipeline: summaries → filter → batch body fetch.

        Shows two tqdm progress bars:
        1. A single-step bar for the summary fetch.
        2. A per-email bar for body fetching.

        Returns:
            List of email record dicts ready for CSV writing.
        """
        # ── Step 1: metadata ──────────────────────────────────────
        print(f"📬  Fetching email list from Apple Mail ({self.mailbox})…")
        with tqdm(total=1, desc="Fetching summaries", unit="batch", ncols=80) as pbar:
            summaries = self._fetch_email_summaries()
            pbar.update(1)

        print(f"   Retrieved {len(summaries):,} total emails.")

        # ── Step 2: filter by date ────────────────────────────────
        recent = self._filter_by_lookback(summaries)
        print(f"   {len(recent):,} emails in the last {self.lookback_hours} h — fetching bodies…")

        if not recent:
            return []

        # ── Step 3: batch body fetch ──────────────────────────────
        records: list[dict] = []

        with tqdm(
            total=len(recent),
            desc="Fetching bodies",
            unit="email",
            ncols=80,
            bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
        ) as pbar:
            for i in range(0, len(recent), self.batch_size):
                batch = recent[i : i + self.batch_size]

                # One JXA call for the whole batch — avoids per-email timeout
                bodies = self._fetch_bodies_batch(batch)

                for email in batch:
                    body = bodies.get(int(email["id"]), "")

                    # Parse sender into name / address components
                    sender_name, sender_email = parseaddr(email.get("sender", ""))

                    records.append(
                        {
                            "date": email.get("date_received", ""),
                            "sender_name": sender_name,
                            "sender_email": sender_email,
                            "subject": email.get("subject", "No Subject"),
                            "body": body[:10_000].replace("\n", " "),
                        }
                    )

                pbar.update(len(batch))

        return records

    def write_csv(self, records: list[dict]) -> None:
        """
        Write email records to the configured CSV file.

        Creates parent directories if they do not exist.  Writes with UTF-8
        encoding and includes a header row.

        Args:
            records: List of email record dicts (as returned by ``fetch``).
        """
        self.output_csv.parent.mkdir(parents=True, exist_ok=True)

        with self.output_csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(records)

        print(f"✅  Saved {len(records):,} emails → {self.output_csv}")

    def run(self) -> None:
        """Convenience method: fetch emails and write to CSV."""
        records = self.fetch()
        if records:
            self.write_csv(records)
        else:
            print("ℹ️   No recent emails to save.")


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    fetcher = EmailFetcher(
        mailbox=MAILBOX,
        account=ACCOUNT,
        lookback_hours=LOOKBACK_HOURS,
        batch_size=BATCH_SIZE,
        output_csv=OUTPUT_CSV,
    )
    fetcher.run()
