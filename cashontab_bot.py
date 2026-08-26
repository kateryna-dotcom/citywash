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

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

BASE_URL = "https://cashontab.co.il"
_TIMEOUT_MS = 15000


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
    """Raises CashOnTabError with a screenshot of the current page attached."""
    try:
        screenshot_b64 = base64.b64encode(page.screenshot(full_page=True)).decode("ascii")
    except Exception:  # noqa: BLE001
        screenshot_b64 = None
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


def _open_picker_near_label(page, label_text):
    """מחסן / קוד ספק both open their search dialog via a small '...' button
    that sits in the same form row as the field's label. Scopes to the row
    containing `label_text` rather than picking the first '...' on the page."""
    row = page.locator(f':text("{label_text}")').locator(
        "xpath=ancestor-or-self::*[self::tr or self::div][1]"
    )
    try:
        row.get_by_role("button", name="...").click(timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, f'לא נמצא כפתור החיפוש ("...") ליד השדה "{label_text}"')


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
    """LEAST VERIFIED PART OF THIS FILE. The items grid (tab פרטים) columns
    seen in the screenshot, left to right as displayed: קוד פריט, תיאור,
    סוג אריזה, אריזות, כמות, מחיר במט"ח, מחיר לפני מע"מ, מחיר כולל מע"מ,
    % הנחה, סה"כ ללא מע"מ. `code` searches/selects the item (auto-fills
    תיאור and a default price); `unit_price` from our invoice data then
    overwrites the default in מחיר לפני מע"מ specifically (price *before*
    VAT, matching how unit_price is parsed from supplier invoices) --
    confirm this is the right column if a run comes out with an unexpected
    total. DOM structure of the grid rows (whether cells are addressable by
    column index reliably) is unconfirmed."""
    try:
        page.get_by_role("button", name="+").click(timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, "לא נמצא כפתור '+' להוספת שורת פריט")

    row = page.locator("table tr").last
    try:
        row.get_by_placeholder("קוד פריט").fill(item["code"], timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, f'לא נמצא שדה "קוד פריט" בשורה החדשה (פריט {item.get("code")})')
    page.wait_for_timeout(600)
    _pick_unique_search_result(page, "פריט", item["code"])

    try:
        row.get_by_label("כמות").fill(str(item["quantity"]), timeout=_TIMEOUT_MS)
        row.get_by_label("מחיר לפני מע\"מ").fill(str(item["unit_price"]), timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, f'לא הצלחתי למלא כמות/מחיר לפריט {item.get("code")} -- ייתכן שהעמודות שונות ממה שתוכנת')


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
        page = browser.new_page()
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

            _open_picker_near_label(page, "מחסן")
            _pick_unique_search_result(page, "מחסן", invoice["branch"])

            _open_picker_near_label(page, "קוד ספק")
            _pick_unique_search_result(page, "ספק", invoice["supplier_name"])

            try:
                page.get_by_label("מספר תעודת ספק").fill(str(invoice["invoice_number"]), timeout=_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                _fail(page, "לא נמצא שדה \"מספר תעודת ספק\"")

            try:
                page.get_by_role("tab", name="פרטים").click(timeout=_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                _fail(page, "לא נמצאה לשונית \"פרטים\" (שורות הפריטים)")

            for item in invoice["items"]:
                _fill_line_item(page, item)

            try:
                page.get_by_role("button", name="צור מסמך").click(timeout=_TIMEOUT_MS)
                page.wait_for_load_state("networkidle", timeout=_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                _fail(page, "לחיצה על \"צור מסמך\" לא הושלמה כצפוי")
        except PlaywrightTimeoutError as e:
            # Safety net for any step above that isn't individually wrapped --
            # always attach a screenshot rather than let a raw timeout escape
            # (see docs/cashontab-entry-playbook.md, first real run 2026-08-26,
            # where a gap here meant no screenshot came back).
            _fail(page, f"שלב לא צפוי בתהליך: {e}")
        finally:
            browser.close()

    return {"status": "created"}
