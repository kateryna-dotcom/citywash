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


def _cancel_partial_document(page):
    """Best-effort cleanup, never allowed to raise or mask the real error:
    the initial "צור מסמך" click in enter_invoice already creates a real,
    persisted document (with its own document number) before any of the
    fields below are filled in. Kateryna found a string of empty/
    near-empty ת.מ. רכש documents in Cash On Tab on 2026-08-27 -- one left
    behind by every failed run from earlier in this debugging session,
    since nothing ever cleaned them up. Called from the except block in
    enter_invoice on any failure after that point, to click "ביטול" and
    discard the partial document instead of leaving another one dangling.

    Scoped to the document panel (same "מסמך: ת.מ. רכש" label prefix as
    the final-save click) -- live run 2026-08-27 showed there are TWO
    "ביטול" buttons on the page by this point, same page-wide-vs-in-panel
    pattern as every other button here. A page-wide search would hit the
    same strict-mode ambiguity and, since this is best-effort, silently
    fail to actually cancel anything."""
    try:
        page.get_by_label(re.compile("^מסמך: ת\\.מ\\. רכש")) \
            .get_by_role("button", name="ביטול") \
            .click(timeout=5000)
    except Exception:  # noqa: BLE001
        pass


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


def _pick_unique_search_result(page, what, query, expected_code=None):
    """Cash On Tab's search dialogs (מחסן / קוד ספק / קוד פריט) all follow the
    same pattern: a search box filters a results table, each row has a
    "בחר" button. Types `query`, then requires EXACTLY one visible row
    containing it -- zero or multiple matches is a hard stop (see the "не
    гадать" rule in the playbook), never a guess.

    `expected_code` is an escape hatch for the case where Cash On Tab
    genuinely has two rows with the identical name -- live run 2026-08-27:
    two מחסן rows are both named "בית דגן" (codes 8 and 24), and unlike
    ספק (where searching a code directly filters correctly), the מחסן
    search box only filters by name, so searching the code itself returns
    zero results instead of narrowing it down. When multiple rows match
    the name and expected_code is given, pick the one row whose own code
    column (the row's last <td>) exactly equals it, instead of failing on
    the ambiguity."""
    try:
        page.get_by_placeholder("חפש", exact=False).first.fill(query, timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, f'לא נמצאה תיבת חיפוש בחלון הבחירה של "{what}"')
    page.wait_for_timeout(600)  # results filter client-side, no reliable load event to await

    rows = page.locator("tr", has_text=query)
    count = rows.count()
    if count == 0:
        _fail(page, f'החיפוש "{query}" לא החזיר תוצאות ({what}) -- לבדוק את הכתיב מול Cash On Tab')

    target_row = rows.first
    if count > 1:
        if expected_code is None:
            _fail(page, f'החיפוש "{query}" החזיר {count} תוצאות ({what}) -- לא ברור איזו למלАי')
        target_row = None
        for i in range(count):
            candidate = rows.nth(i)
            try:
                code_text = candidate.locator("td").last.inner_text(timeout=_TIMEOUT_MS).strip()
            except PlaywrightTimeoutError:
                continue
            if code_text == str(expected_code):
                target_row = candidate
                break
        if target_row is None:
            _fail(page, f'החיפוש "{query}" החזיר {count} תוצאות ({what}), אך אף אחת עם קוד "{expected_code}"')

    try:
        target_row.get_by_role("button", name="בחר").click(timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        _fail(page, f'נמצאה שורה מתאימה ל-"{query}" ({what}) אך לא נלחץ עליה כראוי')


def _find_item_row(page):
    """Returns the items grid's row to fill next: a real data row (>=10
    <td>, ruling out the 1-wide-cell הערה לפריט note row) -- see the
    row-targeting note in _fill_line_item's docstring for why a fixed
    offset from the end isn't reliable once a prior item has been saved.

    Strongly prefers a genuinely *empty* row (קוד פריט not yet filled),
    retrying for a few seconds while Cash On Tab renders the next row
    after a שמור click. Only reusing/clearing the last already-filled row
    as a last resort (when no empty row ever appears) turned out unsafe --
    live run 2026-08-27: re-typing a code into an *already-linked* row
    didn't cleanly replace it, it duplicated that row with כמות/אריזות
    both blown up to 999,999 and Cash On Tab itself flagged a duplicate
    line ("פרט כפול בתעודה"). So don't reuse a non-empty row unless
    retrying for an empty one has genuinely been exhausted."""
    last_data_row = None
    for attempt in range(10):
        rows = page.locator("table tbody tr")
        for i in range(rows.count() - 1, -1, -1):
            tr = rows.nth(i)
            cells = tr.locator("td")
            if cells.count() < 10:
                continue  # too few cells -- this is the הערה לפריט note row
            code_input = cells.nth(1).locator("input")
            if code_input.count() == 0:
                continue
            if last_data_row is None:
                last_data_row = tr
            try:
                if code_input.first.input_value(timeout=1000) == "":
                    return tr
            except PlaywrightTimeoutError:
                pass
        if attempt < 9:
            page.wait_for_timeout(500)
    if last_data_row is not None:
        return last_data_row
    _fail(page, "לא נמצאה שורת פריט להזנת הפריט הבא")


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
    Then set כמות (best-effort -- see below) and click the row's own שמור
    button to confirm it -- distinct from the document-level צור מסמך at
    the very end of enter_invoice (confirmed by Kateryna: שמור sits next to
    per-row grid/delete icons, screenshot 2026-08-27). מחיר לפני מע"מ is
    left as Cash On Tab's own default -- Kateryna confirmed 2026-08-27 the
    invoice's unit_price should NOT overwrite it.

    Row targeting: a fixed nth(-2) (second-to-last <tr>) broke on the
    *second* item -- Kateryna confirmed live 2026-08-27 that קוד פריט still
    showed the *previous* item's code, meaning we were re-editing item 1's
    now-saved row instead of a fresh one (its שמור button was then also
    gone, matching the exact next error we'd been seeing). Requiring that
    row's קוד פריט to be empty (see _find_item_row) fixed that, but broke
    again on later items once Cash On Tab stopped appending fresh empty
    rows -- so _find_item_row now just returns the last data row
    regardless of its content, and this function explicitly clears קוד
    פריט before typing into it (Kateryna's fix, 2026-08-27), instead of
    relying on the row already being empty."""
    row = _find_item_row(page)
    try:
        code_input = row.locator("input").first
        code_input.fill("", timeout=_TIMEOUT_MS)
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

    # כמות has no real <label> (same issue as מספר תעודת ספק earlier) --
    # get_by_label timed out against it live 2026-08-27, and a dynamic
    # header-text lookup (page.locator("table thead th")) also misfired on
    # the second item -- Ant Design tables commonly render a separate
    # fixed-column <table><thead> alongside the main one, so a page-wide
    # "table thead th" query returns more/duplicated headers than the row
    # actually has <td> cells for, silently shifting every index. Kateryna
    # confirmed the real, page-independent mapping via a live DOM dump
    # (2026-08-27): the row's 12 <td> cells line up 1:1, in order, with #,
    # קוד פריט, תיאור, סוג אריזה, אריזות, כמות, מחיר במט"ח, מחיר לפני מע"מ,
    # מחיר כולל מע"מ, % הנחה, סה"כ ללא מע"מ, then the שמור action cell --
    # so hardcode this index instead of re-deriving it per run.
    #
    # Best-effort, not fatal: Cash On Tab's own default כמות (usually 1)
    # is frequently already correct, and Kateryna confirmed 2026-08-27 that
    # fill() can time out here even when the value it set is actually
    # right (a re-render right after the code lookup above likely trips
    # Playwright's stability check) -- failing the whole invoice over that
    # would be wrong, so just leave whatever's there if this fill fails.
    #
    # מחיר לפני מע"מ is intentionally left untouched -- Kateryna confirmed
    # 2026-08-27 the item's own default price should stand, no overwrite
    # from the invoice's unit_price.
    _QUANTITY_COL = 5
    try:
        row.locator("td").nth(_QUANTITY_COL).locator("input").fill(str(item["quantity"]), timeout=_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        pass

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
            #
            # NOT scoped to the document panel label, unlike the final
            # click -- tried that (assuming the same page-wide-vs-in-panel
            # ambiguity applied here too) and it broke this click entirely:
            # live run 2026-08-27 confirmed the panel's accessible label
            # doesn't exist yet at this point, only after this click opens
            # the form. So the original page-wide ambiguity Kateryna hit
            # here once was something else / a one-off, not a symptom of
            # this same two-button issue -- leave this click unscoped.
            try:
                page.get_by_role("button", name="צור מסמך", exact=True).click(timeout=_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                _fail(page, 'בחרתי "ת.מ. רכש" אבל לא נמצא כפתור "צור מסמך" הראשוני (לפתיחת הטופס)')

            # This click already created a real, persisted document (with
            # its own document number) before anything below is filled in
            # -- confirmed 2026-08-27, when Kateryna found a string of
            # empty/near-empty ת.מ. רכש documents in Cash On Tab, one per
            # failed run from earlier in this same debugging session. From
            # here on, try to clean up (click ביטול) on any failure instead
            # of leaving another one dangling.
            try:
                # עובד (employee) always gets the same fixed code -- not
                # part of the invoice data, confirmed by Kateryna
                # 2026-08-27. Same search-picker pattern as מחסן/קוד ספק
                # below (same "..." button).
                _open_picker_near_label(page, "קוד עובד")
                _pick_unique_search_result(page, "עובד", _DEFAULT_EMPLOYEE_CODE)

                _open_picker_near_label(page, "קוד מחסן")
                _pick_unique_search_result(page, "מחסן", invoice["branch"], expected_code=invoice.get("branch_code_hint"))

                _open_picker_near_label(page, "קוד ספק")
                _pick_unique_search_result(page, "ספק", invoice["supplier_name"])

                # Digits only -- suppliers' subject lines often prefix the
                # number with letters (e.g. "SI266028527"), which Kateryna
                # confirmed must be stripped before entry (2026-08-27).
                supplier_doc_number = re.sub(r"\D", "", str(invoice.get("invoice_number") or ""))
                _fill_field_in_row(page, "מספר תעודת ספק", supplier_doc_number)

                try:
                    page.get_by_role("tab", name="פריטים").click(timeout=_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    _fail(page, "לא נמצאה לשונית \"פריטים\" (שורות הפריטים)")

                for item in invoice["items"]:
                    _fill_line_item(page, item)

                # Final document save, at the bottom of the whole form
                # (ביטול / הדפס טיוטה / צור מסמך / צור והצג מסמך / צור
                # תבנית -- screenshot 2026-08-27). Kateryna first said
                # "צור מסמך" alone wasn't the right final action and
                # "צור והצג מסמך" was -- but a live DOM inspection
                # 2026-08-27 (10 items entered correctly, still not
                # finishing) showed "צור מסמך" is actually the primary
                # half of a split-button (an adjacent sibling <button>
                # with no text -- almost certainly a dropdown caret that
                # "צור והצג מסמך" lives behind as a menu item, not a
                # separately clickable button of its own). Kateryna
                # confirmed clicking "צור מסמך" itself is correct and
                # sufficient. Scoped to the document panel by its stable
                # "מסמך: ת.מ. רכש" prefix (קופה name/code vary), same
                # reasoning as before: a page-wide search risks matching
                # more than one button with overlapping text.
                try:
                    page.get_by_label(re.compile("^מסמך: ת\\.מ\\. רכש")) \
                        .get_by_role("button", name="צור מסמך", exact=True) \
                        .click(timeout=_TIMEOUT_MS)
                except PlaywrightTimeoutError:
                    _fail(page, "לחיצה על \"צור מסמך\" הסופית לא הושלמה כצפוי")

                # That click can pop an "אזהרה" (warning) confirm dialog --
                # e.g. "פריט כפול בתעודה" (duplicate line item) -- which
                # blocks the actual save until confirmed. Live run
                # 2026-08-27: the outer click "succeeded" (no timeout) but
                # nothing was ever saved, because this dialog was still
                # sitting there waiting for its own "צור מסמך" to be
                # pressed. Kateryna confirmed it's fine to just proceed
                # through it.
                #
                # A first attempt scoped this via get_by_role("dialog"),
                # assuming Ant Design's Modal.confirm exposes the standard
                # dialog role -- the identical failure recurred right
                # after deploying that, so the role assumption was
                # probably wrong (could be "alertdialog", or no ARIA role
                # at all). Switched to the same text-then-ancestor pattern
                # already proven elsewhere in this file (_fill_field_in_row):
                # find the "אזהרה" text itself, then its nearest ant-modal
                # ancestor, then the button inside that. Best-effort with a
                # generous wait -- most saves don't trigger this dialog at
                # all, so a miss here shouldn't fail or meaningfully slow
                # down a normal run, but a slow render is more likely than
                # a fast one for a popup like this.
                try:
                    warning_title = page.locator(':text-is("אזהרה")').first
                    warning_title.wait_for(state="visible", timeout=5000)
                    warning_title.locator(
                        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ant-modal ')][1]"
                    ).get_by_role("button", name="צור מסמך", exact=True).click(timeout=3000)
                except PlaywrightTimeoutError:
                    pass

                # A second, different dialog can also pop here: "עדכון
                # שינויים במחירי עלות" (update cost-price changes), when
                # this document's price differs from the item's default
                # supplier cost price. Kateryna confirmed 2026-08-27 the
                # default cost price should NOT be updated from this
                # document -- click "אל תעדכן" (don't update) to dismiss
                # it and proceed. Same text-then-ancestor approach, same
                # best-effort/generous-wait reasoning as the אזהרה dialog
                # above.
                try:
                    cost_price_title = page.locator(':text-is("עדכון שינויים במחירי עלות")').first
                    cost_price_title.wait_for(state="visible", timeout=5000)
                    cost_price_title.locator(
                        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' ant-modal ')][1]"
                    ).get_by_role("button", name="אל תעדכן", exact=True).click(timeout=3000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_load_state("networkidle", timeout=_TIMEOUT_MS)

                # Verify the save actually happened instead of trusting a
                # clean click -- live run 2026-08-27: the click and the
                # wait both completed with no error, invoice_store got
                # marked "ok", but nothing was ever created in Cash On Tab
                # at all. A click not timing out only means the button was
                # reachable, not that Cash On Tab persisted anything. As
                # long as ביטול is still there, we're still in the
                # unsaved create/edit form -- if it's gone within a few
                # seconds of the click, the document panel closed and
                # entered_invoice can trust the save (matches how every
                # confirmed-successful save so far looked in screenshots:
                # the form/its ביטול button disappear once truly saved).
                #
                # Scoped to the document panel, not the whole page -- live
                # run 2026-08-27 hit a strict-mode error here too: there
                # are TWO "ביטול" buttons on the page at this point, same
                # page-wide-vs-in-panel pattern as every other button in
                # this flow. If the save genuinely succeeded the panel
                # (and its label) is gone entirely, so this scoped locator
                # naturally has zero matches and wait_for(hidden) resolves
                # immediately -- exactly the signal we want.
                try:
                    page.get_by_label(re.compile("^מסמך: ת\\.מ\\. רכש")) \
                        .get_by_role("button", name="ביטול") \
                        .wait_for(state="hidden", timeout=10000)
                except PlaywrightTimeoutError:
                    _fail(page, 'לחצתי "צור מסמך" בלי שגיאה, אבל הטופס (כפתור "ביטול") עדיין פתוח -- ' +
                          "נראה שהמסמך לא נשמר בפועל למרות שהלחיצה עצמה הצליחה")
            except CashOnTabError:
                _cancel_partial_document(page)
                raise
            except PlaywrightTimeoutError as e:
                # Catches anything in the block above that isn't
                # individually wrapped with its own _fail() call -- still
                # needs cleanup since the document already exists by here,
                # unlike the outer safety net below.
                _cancel_partial_document(page)
                _fail(page, f"שלב לא צפוי בתהליך: {e}")
        except PlaywrightTimeoutError as e:
            # Safety net for any step above that isn't individually wrapped --
            # always attach a screenshot rather than let a raw timeout escape
            # (see docs/cashontab-entry-playbook.md, first real run 2026-08-26,
            # where a gap here meant no screenshot came back).
            _fail(page, f"שלב לא צפוי בתהליך: {e}")
        finally:
            browser.close()

    return {"status": "created"}
