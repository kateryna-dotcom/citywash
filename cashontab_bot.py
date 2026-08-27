"""
Server-side automation that logs into Cash On Tab and creates a ת.מ. רכש
(goods receipt) document for one invoice, using our own
/api/inventory/ready-for-entry data as the source of truth for what to key
in. Runs headless Chromium via Playwright.

Cash On Tab has no API (checked -- see catalog_store.py), so this drives the
real web UI the same way Kateryna does by hand: log in, search for the
branch/supplier/item by name in Cash On Tab's own pickers, fill quantities
and the real invoice price, save. See docs/cashontab-entry-playbook.md for
the step-by-step this mirrors, and for the browser-supervised alternative
(Claude in Chrome) that doesn't need a stored password.

Never guesses: every search (branch, supplier, item) must return exactly one
matching row or this raises instead of picking blindly -- see
_pick_unique_search_result.

Env vars required (set in Render, never in git):
    CASHONTAB_COMPANY_CODE
    CASHONTAB_USERNAME
    CASHONTAB_PASSWORD

IMPORTANT -- this was written from screenshots taken together with Kateryna
on 2026-08-23 (login screen added 2026-08-25), not from Cash On Tab's actual
HTML (no API or browser access from the session that wrote it). Selectors
favor visible Hebrew text/roles over guessed CSS classes/attribute names for
that reason, but this is still a first draft, never run end-to-end against
the live site: expect it to need a debugging pass, especially the
per-line-item fields (_fill_line_item), which is the part seen the least
clearly. On any unexpected screen this raises CashOnTabError with a
screenshot of the page attached, so a failure can be diagnosed from a chat
without anyone needing to be watching the browser when it happens.
"""
import base64
import os
import re

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://cashontab.co.il"
_TIMEOUT_MS = 15000

# Fixed עובד (employee) code every automated document gets filed under --
# not derived from invoice data (confirmed by Kateryna 2026-08-27).
_DEFAULT_EMPLOYEE_CODE = "999"


class CashOnTabError(Exception):
    """Raised whenever the bot can't confidently proceed: missing
    credentials, a search with zero or multiple matches, a screen that
    doesn't look like what was expected. `screenshot_b64` (PNG, may be None
    if even the screenshot failed) lets the caller show exactly what the bot
    was looking at."""

    def __init__(self, message, screenshot_b64=None):
        super().__init__(message)
        self.screenshot_b64 = screenshot_b64


def _get_credentials():
    company = os.environ.get("CASHONTAB_COMPANY_CODE")
    username = os.environ.get("CASHONTAB_USERNAME")
    password = os.environ.get("CASHONTAB_PASSWORD")
    if not (company and username and password):
        raise CashOnTabError(
            "CASHONTAB_COMPANY_CODE / CASHONTAB_USERNAME / CASHONTAB_PASSWORD "
            "environment variables are not all set"
        )
    return company, username, password


def _fail(page, message):
    """Raises CashOnTabError with a screenshot of the current page attached.
    Every real run so far has come back with NO screenshot at all (silently
    swallowed) -- so this now (a) falls back to a viewport-only screenshot if
    the full-page one fails (full_page can struggle inside an open ant-modal
    with its own scroll/overlay), and (b) if even that fails, appends the
    actual exception text to the error message instead of just losing the
    screenshot silently, so the failure itself becomes visible in the UI."""
    screenshot_b64 = None
    screenshot_error = None
    try:
        screenshot_b64 = base64.b64encode(page.screenshot(full_page=True, timeout=10000)).decode("ascii")
    except Exception as e:  # noqa: BLE001
        screenshot_error = f"full_page: {e}"
        try:
            screenshot_b64 = base64.b64encode(page.screenshot(timeout=10000)).decode("ascii")
            screenshot_error = None
        except Exception as e2:  # noqa: BLE001
            screenshot_error = f"{screenshot_error} | viewport: {e2}"
    if screenshot_error:
        message = f"{message} [גם צילום המסך נכשל: {screenshot_error}]"
    raise CashOnTabError(message, screenshot_b64=screenshot_b64)


def _login(page, company, username, password):
    """Based on a real screenshot of the login screen ("התחברות למערכת"),
    gathered 2026-08-25 -- not yet run end-to-end against the live site.
    The company-code field has no placeholder of its own (the screenshot
    showed it pre-filled with a remembered value from Kateryna's own
    browser, which a fresh headless session won't have), so it's addressed
    by position -- the first input on the page -- rather than by label."""
    page.goto(BASE_URL, timeout=_TIMEOUT_MS)
    try:
        page.locator("input").first.fill(str(company), timeout=_TIMEOUT_MS)
        page.get_by_placeholder("שם משתמש").fill(username, timeout=_TIMEOUT_MS)
        page.get_by_placeholder("סיסמה").fill(password, timeout=_TIMEOUT_MS)
        page.get_by_role("button", name="התחבר").click(timeout=_TIMEOUT_MS)
        page.wait_for_load_state("networkidle", timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, "לא הצלחתי למלא את מסך ההתחברות (קוד חברה / שם משתמש / סיסמה / התחבר)")


def _open_picker_near_label(page, input_placeholder):
    """מחסן / קוד ספק both open their search dialog via a small button (an
    Ant Design Input.Search with a custom ellipsis enterButton) that lives
    in the SAME `span.ant-input-group` as the field's own input. Confirmed
    precisely on the מחסן field on 2026-08-26 via Claude in Chrome reading
    the live DOM with JS (document.querySelector), not a DevTools
    screenshot -- the actual field is `input#search_storage_picker[
    placeholder="קוד מחסן"]`, and its search-button addon (sibling of the
    input's own wrapper inside that shared ant-input-group span) contains
    `i.anticon-ellipsis`. Anchoring on the input's placeholder (id differs
    per field, unlike placeholder) and clicking the button that contains
    that specific icon, rather than just "the last button in the group" --
    tighter now that the exact icon is confirmed rather than assumed."""
    input_el = page.get_by_placeholder(input_placeholder)
    group = input_el.locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ant-input-group ')][1]"
    )
    try:
        group.locator("button:has(.anticon-ellipsis)").click(timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, f'לא נמצא כפתור החיפוש ליד השדה עם placeholder "{input_placeholder}"')


def _fill_field_in_row(page, label_text, value):
    """Plain text fields with no search picker (e.g. מספר תעודת ספק) --
    label and input share a `div.ant-row` the same way מחסן/קוד ספק do, but
    the input itself has no id/name/placeholder to anchor on (confirmed via
    Claude in Chrome reading the live DOM, 2026-08-27). Scopes to the
    label's ant-row and fills that row's one input.ant-input."""
    row = page.locator(f':text("{label_text}")').locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ant-row ')][1]"
    )
    try:
        row.locator("input.ant-input").fill(value, timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, f'לא נמצא שדה טקסט ליד "{label_text}"')


def _pick_unique_search_result(page, what, query):
    """Cash On Tab's search dialogs (מחסן / קוד ספק / קוד פריט) all follow the
    same pattern: a search box filters a results table, each row has a
    "בחר" button. Types `query`, then requires EXACTLY one visible row
    containing it -- zero or multiple matches is a hard stop (see the "не
    гадать" rule in the playbook), never a guess."""
    try:
        page.get_by_placeholder("חפש", exact=False).first.fill(query, timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, f'לא נמצאה תיבת חיפוש בחלון הבחירה של "{what}"')
    page.wait_for_timeout(600)  # results filter client-side, no reliable load event to await

    rows = page.locator("tr", has_text=query)
    count = rows.count()
    if count == 0:
        _fail(page, f'החיפוש "{query}" לא החזיר תוצאות ({what}) -- לבדוק את הכתיב מול Cash On Tab')
    if count > 1:
        _fail(page, f'החיפוש "{query}" החזיר {count} תוצאות ({what}) -- לא ברור איזו למלАי')
    try:
        rows.first.get_by_role("button", name="בחר").click(timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, f'נמצאה שורה מתאימה ל-"{query}" ({what}) אך לא נלחץ עליה כראוי')


def _fill_line_item(page, item):
    """LEAST VERIFIED PART OF THIS FILE. Items grid (tab פריטים): every item
    is actually a *pair* of <tr> rows -- the data row (קוד פריט, תיאור,
    כמות, prices, ...) immediately followed by a second, full-width הערה
    לפריט (item note) row. Two live runs confirmed this the hard way: using
    the last <tr> as the data row silently filled the item code into הערה
    לפריט instead of קוד פריט (fill() doesn't error just because it hit the
    "wrong" input). So the data row is the *second-to-last* <tr>, not the
    last. A single empty pair (#0) already exists as soon as the tab opens
    -- there is no "+"/add-row button (confirmed by Kateryna 2026-08-27).
    Type the item code directly into the data row's קוד פריט input and
    press Enter -- not a search-picker dialog like מחסן/ספק (confirmed by
    Kateryna) -- which auto-fills תיאור and a default price, and is assumed
    (unconfirmed) to open a new empty row-pair below for the next item.
    Then set כמות and overwrite the default מחיר לפני מע"מ (price before
    VAT) with the invoice's real unit_price, and click the row's own שמור
    button to confirm it -- distinct from the document-level צור מסמך at
    the very end of enter_invoice (confirmed by Kateryna: שמור sits next to
    per-row grid/delete icons, screenshot 2026-08-27)."""
    row = page.locator("table tbody tr").nth(-2)
    try:
        code_input = row.locator("input").first
        code_input.fill(item["code"], timeout=_TIMEOUT_MS)
        code_input.press("Enter")
    except PlaywrightTimeoutError:
        _fail(page, f'לא נמצא שדה "קוד פריט" בשורה החדשה (פריט {item.get("code")})')
    # Enter alone was seen to not always trigger the lookup (תיאור/price
    # stayed empty) -- Kateryna confirmed the same search icon inside that
    # cell does the same thing, so click it too as a belt-and-suspenders
    # second trigger. Best-effort: if the icon isn't there, Enter above may
    # still have worked, so don't fail the whole run over this alone.
    try:
        row.locator(".anticon-search").first.click(timeout=2000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1000)

    # כמות/מחיר לפני מע"מ have no real <label> (same issue as מספר תעודת
    # ספק earlier) -- get_by_label timed out against them live 2026-08-27.
    # Target by column position instead, matched against the header row.
    headers = [h.strip() for h in page.locator("table thead th").all_inner_texts()]

    def _cell_input(column_label):
        try:
            col_index = headers.index(column_label)
        except ValueError:
            _fail(page, f'לא נמצאה עמודה "{column_label}" בטבלת הפריטים (עמודות שנמצאו: {headers})')
        return row.locator("td").nth(col_index).locator("input")

    try:
        _cell_input("כמות").fill(str(item["quantity"]), timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, f'לא הצלחתי למלא כמות לפריט {item.get("code")} -- ייתכן שהעמודה שונה ממה שתוכנת')

    try:
        _cell_input('מחיר לפני מע"מ').fill(str(item["unit_price"]), timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, f'לא הצלחתי לעדכן "מחיר לפני מע"מ" לפריט {item.get("code")} -- ייתכן שהעמודה שונה ממה שתוכנת')

    try:
        row.get_by_role("button", name="שמור").click(timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, f'לא נמצא כפתור "שמור" לשורת הפריט {item.get("code")}')


def enter_invoice(invoice: dict) -> dict:
    """Logs into Cash On Tab and creates one ת.מ. רכש document for `invoice`
    (the shape returned by GET /api/inventory/ready-for-entry: {id, branch,
    supplier_name, invoice_number, items: [{code, name, quantity, unit_price}]}).

    Raises CashOnTabError (screenshot attached) on any ambiguity or
    unexpected screen -- never guesses. Returns {"status": "created"} on
    success; the caller (web_app.py) is responsible for then marking the
    invoice "ok" in our own database via invoice_store.update_status."""
    company, username, password = _get_credentials()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        # Default viewport (1280x720) is narrower than Kateryna's own browser
        # window -- Cash On Tab's Ant Design forms are responsive and may
        # collapse/hide fields at narrower widths, which would explain a
        # selector that's confirmed correct via DevTools/live DOM checks on
        # her screen still not matching anything in the bot's own run.
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        try:
            _login(page, company, username, password)

            # Cash On Tab is an Ant Design SPA with client-side routing --
            # hitting /documents as a direct URL 404s (confirmed 2026-08-26),
            # so navigation has to go through the actual menu click, same as
            # a human would. The right-rail nav is collapsed to icons only,
            # but DevTools showed the underlying <li role="menuitem"> still
            # carries its real label ("מסמכים") in a visually-collapsed
            # <span>, which Playwright's accessible-name lookup still sees.
            try:
                page.get_by_role("menuitem", name="מסמכים").click(timeout=_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                _fail(page, 'לא נמצא פריט התפריט "מסמכים" (הניווט לעמוד יצירת המסמך)')

            try:
                page.get_by_role("button", name="ת.מ. רכש", exact=True).click(timeout=_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                _fail(page, 'נכנסתי ל"מסמכים" אבל לא נמצא כפתור "ת.מ. רכש"')

            # Selecting the document type alone doesn't open the editable
            # form -- "צור מסמך" has to be pressed once here too, before the
            # מחסן/קוד ספק fields exist at all (confirmed by Kateryna
            # 2026-08-27: every earlier failure to find "קוד מחסן" was
            # because this step was missing, not a wrong selector). The same
            # button text is pressed again at the very end to actually save
            # the filled-in document -- two different clicks, same label.
            try:
                page.get_by_role("button", name="צור מסמך", exact=True).click(timeout=_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                _fail(page, 'בחרתי "ת.מ. רכש" אבל לא נמצא כפתור "צור מסמך" הראשוני (לפתיחת הטופס)')

            # עובד (employee) always gets the same fixed code -- not part of
            # the invoice data, confirmed by Kateryna 2026-08-27. Same
            # search-picker pattern as מחסן/קוד ספק below (same "..." button).
            _open_picker_near_label(page, "קוד עובד")
            _pick_unique_search_result(page, "עובד", _DEFAULT_EMPLOYEE_CODE)

            _open_picker_near_label(page, "קוד מחסן")
            _pick_unique_search_result(page, "מחסן", invoice["branch"])

            _open_picker_near_label(page, "קוד ספק")
            _pick_unique_search_result(page, "ספק", invoice["supplier_name"])

            # Digits only -- suppliers' subject lines often prefix the number
            # with letters (e.g. "SI266028527"), which Kateryna confirmed
            # must be stripped before entry (2026-08-27).
            supplier_doc_number = re.sub(r"\D", "", str(invoice.get("invoice_number") or ""))
            _fill_field_in_row(page, "מספר תעודת ספק", supplier_doc_number)

            try:
                page.get_by_role("tab", name="פריטים").click(timeout=_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                _fail(page, "לא נמצאה לשונית \"פריטים\" (שורות הפריטים)")

            for item in invoice["items"]:
                _fill_line_item(page, item)

            # Final document save, at the bottom of the whole form (ביטול /
            # הדפס טיוטה / צור מסמך / צור והצג מסמך / צור תבנית -- screenshot
            # 2026-08-27) -- same button text as the initial one that opened
            # the empty form, not שמור (that's the *per-row* item confirm,
            # see _fill_line_item). exact=True so it doesn't also match
            # "צור והצג מסמך".
            try:
                page.get_by_role("button", name="צור מסמך", exact=True).click(timeout=_TIMEOUT_MS)
                page.wait_for_load_state("networkidle", timeout=_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                _fail(page, "לחיצה על \"צור מסמך\" הסופית לא הושלמה כצפוי")
        except PlaywrightTimeoutError as e:
            # Safety net for any step above that isn't individually wrapped --
            # always attach a screenshot rather than let a raw timeout escape
            # (see docs/cashontab-entry-playbook.md, first real run 2026-08-26,
            # where a gap here meant no screenshot came back).
            _fail(page, f"שלב לא צפוי בתהליך: {e}")
        finally:
            browser.close()

    return {"status": "created"}
