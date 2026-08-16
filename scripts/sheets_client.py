"""
Google Sheets client — comprehensive helpers for reading, writing, and managing tabs.

Auth: service account at ~/.claude/google-service-account.json. Single auth path,
works headless on any machine. Sheets you want to operate on must be shared with
the service account email (found in the JSON under `client_email`).

Creating new spreadsheets: service accounts cannot create files at the Drive
root (Google returns 403). One-time setup: create a folder in your Drive, share
it (Editor) with the service account email, and set env var
SHEETS_PARENT_FOLDER_ID to that folder's ID. New sheets land in that folder
(visible in your Drive automatically). You can also auto-share each new sheet
to SHEETS_OWNER_EMAIL if you want it explicitly listed under "Shared with me".

Quick start:
    from sheets_client import SheetsClient
    sc = SheetsClient(SPREADSHEET_ID)

    # Read
    rows = sc.read("Sheet1")                  # list[list[str]]
    dicts = sc.read_dicts("Sheet1")           # list[dict] using row 1 as header

    # Write
    sc.write("Sheet1", [["a","b"],["c","d"]])  # clears + writes from A1
    sc.append("Sheet1", [["e","f"]])           # append to bottom
    sc.write_dicts("Sheet1", [{"a": 1, "b": 2}])  # auto-derives headers

    # Tabs
    sc.list_tabs()                             # ['Sheet1', 'Tab2']
    sc.create_tab("My New Tab", color="blue")  # idempotent
    sc.delete_tab("Old Tab")
    sc.rename_tab("Sheet1", "Renamed")
    sc.tab_id("Sheet1")                        # numeric sheetId

    # Format
    sc.format_header("Sheet1", bold=True, freeze=True)
    sc.color_code_column("Sheet1", "web_verdict",
                         {"yes": "green", "no": "red", "unsure": "yellow"})
    sc.autofit("Sheet1")

    # Create a new spreadsheet (lands in SHEETS_PARENT_FOLDER_ID)
    sid, url = SheetsClient.create_spreadsheet("My Sheet")
    sid, url = SheetsClient.create_spreadsheet("My Sheet", parent_folder_id="1ab...")
    sid, url = SheetsClient.create_spreadsheet("My Sheet", owner_email="me@example.com")

Sheet ranges use single-quoted names so spaces/special chars work:
    "'My Tab Name'!A1:Z10"

Ref docs: https://developers.google.com/sheets/api/reference/rest
"""
from __future__ import annotations

import csv
import json
import os
import re
from pathlib import Path
from typing import Iterable, Sequence


# ---------- Auth ----------

_SERVICE_CRED_PATH = Path.home() / ".claude" / "google-service-account.json"
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _credentials():
    """Service account credentials. Sheet must be shared with the SA email."""
    from google.oauth2 import service_account
    if not _SERVICE_CRED_PATH.exists():
        raise FileNotFoundError(
            f"Service account JSON not found at {_SERVICE_CRED_PATH}. "
            "Drop your service account key there to use this client."
        )
    return service_account.Credentials.from_service_account_file(
        str(_SERVICE_CRED_PATH),
        scopes=_SCOPES,
    )


def _build_sheets_service():
    from googleapiclient.discovery import build
    return build("sheets", "v4", credentials=_credentials(), cache_discovery=False)


def _build_drive_service():
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=_credentials(), cache_discovery=False)


# ---------- Helpers ----------

_COLOR_PRESETS = {
    "green":  {"red": 0.85, "green": 0.95, "blue": 0.85},
    "red":    {"red": 1.00, "green": 0.85, "blue": 0.85},
    "yellow": {"red": 1.00, "green": 0.97, "blue": 0.78},
    "blue":   {"red": 0.85, "green": 0.92, "blue": 1.00},
    "grey":   {"red": 0.93, "green": 0.93, "blue": 0.93},
    "header": {"red": 0.85, "green": 0.85, "blue": 0.90},
    "tab_blue":   {"red": 0.20, "green": 0.60, "blue": 0.90},
    "tab_green":  {"red": 0.20, "green": 0.70, "blue": 0.30},
    "tab_red":    {"red": 0.85, "green": 0.30, "blue": 0.30},
    "tab_orange": {"red": 0.95, "green": 0.55, "blue": 0.20},
}


def _color(spec) -> dict:
    """Accepts a preset name like 'green', a dict {r,g,b}, or None."""
    if spec is None:
        return None
    if isinstance(spec, str):
        if spec not in _COLOR_PRESETS:
            raise ValueError(f"Unknown color preset '{spec}'. Available: {list(_COLOR_PRESETS)}")
        return _COLOR_PRESETS[spec]
    return spec


def _quote_tab(tab: str) -> str:
    """Wrap tab name in single quotes for use in A1 ranges, escaping any quotes."""
    return "'" + tab.replace("'", "''") + "'"


def _stringify_row(row: Sequence) -> list:
    """Coerce values to JSON-safe strings/numbers for the API."""
    out = []
    for v in row:
        if v is None:
            out.append("")
        elif isinstance(v, (str, int, float, bool)):
            out.append(v)
        else:
            out.append(str(v))
    return out


# ---------- SheetsClient ----------

class SheetsClient:
    """Wrapper around the Sheets v4 API. One instance per spreadsheet."""

    def __init__(self, spreadsheet_id: str):
        self.spreadsheet_id = spreadsheet_id
        self._svc = _build_sheets_service()
        self._spreadsheets = self._svc.spreadsheets()
        self._meta_cache = None

    # ---- Metadata ----

    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"

    def metadata(self, refresh: bool = False) -> dict:
        if self._meta_cache is None or refresh:
            self._meta_cache = self._spreadsheets.get(spreadsheetId=self.spreadsheet_id).execute()
        return self._meta_cache

    def list_tabs(self) -> list[str]:
        return [s["properties"]["title"] for s in self.metadata(refresh=True)["sheets"]]

    def tab_id(self, tab: str) -> int:
        for s in self.metadata(refresh=True)["sheets"]:
            if s["properties"]["title"] == tab:
                return s["properties"]["sheetId"]
        raise KeyError(f"Tab '{tab}' not found in spreadsheet {self.spreadsheet_id}")

    def has_tab(self, tab: str) -> bool:
        try:
            self.tab_id(tab)
            return True
        except KeyError:
            return False

    # ---- Tab management ----

    def create_tab(self, tab: str, *, rows: int = 1000, cols: int = 26,
                   color: str | dict | None = None, exist_ok: bool = True) -> int:
        """Create a tab. Returns its sheetId. If exist_ok and tab exists, returns its id."""
        if self.has_tab(tab):
            if exist_ok:
                return self.tab_id(tab)
            raise ValueError(f"Tab '{tab}' already exists")
        props = {
            "title": tab,
            "gridProperties": {"rowCount": max(rows, 1), "columnCount": max(cols, 1)},
        }
        c = _color(color)
        if c:
            props["tabColor"] = c
        self._spreadsheets.batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": props}}]},
        ).execute()
        self._meta_cache = None
        return self.tab_id(tab)

    def delete_tab(self, tab: str, *, missing_ok: bool = True) -> None:
        if not self.has_tab(tab):
            if missing_ok:
                return
            raise KeyError(f"Tab '{tab}' not found")
        sid = self.tab_id(tab)
        self._spreadsheets.batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{"deleteSheet": {"sheetId": sid}}]},
        ).execute()
        self._meta_cache = None

    def rename_tab(self, tab: str, new_name: str) -> None:
        sid = self.tab_id(tab)
        self._spreadsheets.batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{
                "updateSheetProperties": {
                    "properties": {"sheetId": sid, "title": new_name},
                    "fields": "title",
                }
            }]},
        ).execute()
        self._meta_cache = None

    def clear(self, tab: str, *, range_a1: str = "A1:ZZ100000") -> None:
        self._spreadsheets.values().clear(
            spreadsheetId=self.spreadsheet_id,
            range=f"{_quote_tab(tab)}!{range_a1}",
        ).execute()

    # ---- Reading ----

    def read(self, tab: str, *, range_a1: str = "A1:ZZ100000",
             value_render: str = "UNFORMATTED_VALUE") -> list[list]:
        """Read raw 2D values from the tab. Returns empty list if tab is empty."""
        resp = self._spreadsheets.values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{_quote_tab(tab)}!{range_a1}",
            valueRenderOption=value_render,
        ).execute()
        return resp.get("values", [])

    def read_dicts(self, tab: str, *, range_a1: str = "A1:ZZ100000") -> list[dict]:
        """Read tab and return as list of dicts using row 1 as header."""
        rows = self.read(tab, range_a1=range_a1)
        if not rows:
            return []
        header = [str(h or "") for h in rows[0]]
        out = []
        for r in rows[1:]:
            d = {}
            for i, h in enumerate(header):
                d[h] = r[i] if i < len(r) else ""
            out.append(d)
        return out

    # ---- Writing ----

    def write(self, tab: str, rows: Iterable[Sequence], *,
              range_a1: str = "A1", value_input: str = "RAW",
              clear_first: bool = True, ensure_tab: bool = True) -> int:
        """Write a 2D array. Default: clear tab then write from A1.
        Returns rows written. Set value_input='USER_ENTERED' to let Sheets parse formulas/dates.
        """
        if ensure_tab and not self.has_tab(tab):
            self.create_tab(tab)
        rows = [list(_stringify_row(r)) for r in rows]
        if clear_first:
            self.clear(tab)
        if rows:
            self._spreadsheets.values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"{_quote_tab(tab)}!{range_a1}",
                valueInputOption=value_input,
                body={"values": rows},
            ).execute()
        return len(rows)

    def append(self, tab: str, rows: Iterable[Sequence], *,
               value_input: str = "RAW", ensure_tab: bool = True) -> int:
        """Append rows to the bottom of the tab."""
        if ensure_tab and not self.has_tab(tab):
            self.create_tab(tab)
        rows = [list(_stringify_row(r)) for r in rows]
        if not rows:
            return 0
        self._spreadsheets.values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"{_quote_tab(tab)}!A1",
            valueInputOption=value_input,
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()
        return len(rows)

    def write_dicts(self, tab: str, dicts: list[dict], *,
                    columns: list[str] | None = None, value_input: str = "RAW",
                    clear_first: bool = True, ensure_tab: bool = True) -> int:
        """Write a list of dicts. Header derived from union of keys (or `columns` if given).
        Returns rows written (incl. header).
        """
        if not dicts and not columns:
            return 0
        if columns is None:
            seen = []
            seen_set = set()
            for d in dicts:
                for k in d.keys():
                    if k not in seen_set:
                        seen.append(k); seen_set.add(k)
            columns = seen
        rows = [columns] + [[d.get(c, "") for c in columns] for d in dicts]
        return self.write(tab, rows, value_input=value_input, clear_first=clear_first, ensure_tab=ensure_tab)

    def upload_csv(self, tab: str, csv_path: str | Path, *,
                   value_input: str = "RAW", clear_first: bool = True) -> int:
        """Read a CSV file and write its contents to a tab."""
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        return self.write(tab, rows, value_input=value_input, clear_first=clear_first)

    # ---- Sharing ----

    def share(self, email: str, *, role: str = "writer", notify: bool = False) -> None:
        """Share this spreadsheet with `email`. Role: 'reader', 'writer', or 'owner'."""
        drive = _build_drive_service()
        drive.permissions().create(
            fileId=self.spreadsheet_id,
            body={"type": "user", "role": role, "emailAddress": email},
            sendNotificationEmail=notify,
            fields="id",
        ).execute()

    # ---- Formatting ----

    def format_header(self, tab: str, *, bold: bool = True, freeze: bool = True,
                      bg_color: str | dict | None = "header") -> None:
        """Bold + grey-bg header row, optionally freeze it."""
        sid = self.tab_id(tab)
        requests = []
        if bold or bg_color:
            cell_format = {}
            if bold:
                cell_format["textFormat"] = {"bold": True}
            if bg_color:
                cell_format["backgroundColor"] = _color(bg_color)
            requests.append({
                "repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": cell_format},
                    "fields": "userEnteredFormat(textFormat,backgroundColor)",
                }
            })
        if freeze:
            requests.append({
                "updateSheetProperties": {
                    "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            })
        if requests:
            self._spreadsheets.batchUpdate(spreadsheetId=self.spreadsheet_id, body={"requests": requests}).execute()

    def color_code_column(self, tab: str, column_name_or_index: str | int,
                          mapping: dict, *, start_row: int = 1) -> None:
        """Apply conditional formatting to a column.
        mapping is {cell_value: color_preset_or_dict}.
        column_name_or_index: header name (looks up index) or 0-based index.
        """
        sid = self.tab_id(tab)
        if isinstance(column_name_or_index, int):
            col_idx = column_name_or_index
        else:
            header = self.read(tab, range_a1="A1:ZZ1")
            if not header or not header[0]:
                raise ValueError(f"Tab '{tab}' has no header row")
            try:
                col_idx = [str(h) for h in header[0]].index(column_name_or_index)
            except ValueError:
                raise ValueError(f"Column '{column_name_or_index}' not found in header: {header[0]}")
        requests = []
        for i, (value, color) in enumerate(mapping.items()):
            requests.append({
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": sid,
                            "startRowIndex": start_row,
                            "startColumnIndex": col_idx,
                            "endColumnIndex": col_idx + 1,
                        }],
                        "booleanRule": {
                            "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": str(value)}]},
                            "format": {"backgroundColor": _color(color)},
                        },
                    },
                    "index": i,
                }
            })
        if requests:
            self._spreadsheets.batchUpdate(spreadsheetId=self.spreadsheet_id, body={"requests": requests}).execute()

    def autofit(self, tab: str, *, dimension: str = "COLUMNS") -> None:
        """Auto-resize columns (or rows). dimension in {'COLUMNS','ROWS'}."""
        sid = self.tab_id(tab)
        self._spreadsheets.batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sid,
                        "dimension": dimension,
                    }
                }
            }]},
        ).execute()

    def set_tab_color(self, tab: str, color: str | dict) -> None:
        sid = self.tab_id(tab)
        self._spreadsheets.batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={"requests": [{
                "updateSheetProperties": {
                    "properties": {"sheetId": sid, "tabColor": _color(color)},
                    "fields": "tabColor",
                }
            }]},
        ).execute()

    # ---- Spreadsheet creation ----

    @staticmethod
    def create_spreadsheet(title: str, *, parent_folder_id: str | None = None,
                            owner_email: str | None = None) -> tuple[str, str]:
        """Create a brand-new spreadsheet. Returns (spreadsheet_id, url).

        Service accounts cannot create files at the Drive root (403). The new
        sheet is created inside a folder that is shared with the service account:
            parent_folder_id arg, or env var SHEETS_PARENT_FOLDER_ID (required).

        One-time setup: create a folder in your Drive, share it (Editor) with
        the service account email in ~/.claude/google-service-account.json
        (`client_email`), then set SHEETS_PARENT_FOLDER_ID to its folder ID.

        Optionally also auto-share the new sheet with `owner_email` (or env var
        SHEETS_OWNER_EMAIL). Usually unnecessary if you already have access via
        the parent folder.
        """
        # --- PREFERRED PATH: create as devon@smartbound.ai via gwcli user OAuth ---
        # The service account has ZERO Drive storage quota, so creating a file as the
        # service account fails with 403 storageQuotaExceeded even inside a shared
        # folder. gwcli holds real user tokens for devon@smartbound.ai and the Sheets
        # API `spreadsheets.create` needs no Drive scope. This is the route that works.
        # (Verified 2026-07-28. Do NOT use the Google Drive MCP -- it silently creates
        # under devonkellar@gmail.com, the wrong account.)
        try:
            import sys as _sys
            _p = os.path.join(os.path.expanduser("~"), ".claude", "scripts")
            if _p not in _sys.path:
                _sys.path.insert(0, _p)
            import gwcli_sheets as _gw

            _svc = _gw.service()                      # -> devon@smartbound.ai
            sid = _gw.create(_svc, title)
            url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"
            try:
                _gw.share_with_service_account(sid)   # so this client can write to it
            except Exception as _e:
                print(f"[sheets_client] NOTE: sheet created at {url}\n"
                      f"  but auto-share to the service account failed.\n  {_e}")
            return sid, url
        except ImportError:
            pass  # gwcli_sheets not available -- fall through to the legacy path

        # --- LEGACY FALLBACK: service account into a pre-shared parent folder ---
        parent = parent_folder_id or os.environ.get("SHEETS_PARENT_FOLDER_ID", "")
        if not parent:
            raise RuntimeError(
                "create_spreadsheet needs a parent folder. Set "
                "SHEETS_PARENT_FOLDER_ID (or pass parent_folder_id=) to a folder "
                "ID that has been shared with the service account email."
            )
        drive = _build_drive_service()
        created = drive.files().create(
            body={
                "name": title,
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "parents": [parent],
            },
            fields="id",
            supportsAllDrives=True,
        ).execute()
        sid = created["id"]
        url = f"https://docs.google.com/spreadsheets/d/{sid}/edit"

        share_to = owner_email if owner_email is not None else os.environ.get("SHEETS_OWNER_EMAIL", "")
        if share_to:
            drive.permissions().create(
                fileId=sid,
                body={"type": "user", "role": "writer", "emailAddress": share_to},
                sendNotificationEmail=False,
                fields="id",
            ).execute()
        return sid, url


# ---------- CLI smoke test ----------
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    sid = sys.argv[1]
    sc = SheetsClient(sid)
    print(f"URL: {sc.url()}")
    print(f"Tabs: {sc.list_tabs()}")
