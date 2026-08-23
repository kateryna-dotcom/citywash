"""
Canonical list of ABT / City Wash branches, matching the branch dropdown in
Cash On Tab exactly (so values picked here line up 1:1 with the real system).
Used to: (1) best-effort auto-detect the branch from invoice text, and
(2) populate the branch picker in the מלАי review UI so Kateryna can assign
or correct it in one click.
"""

BRANCHES = [
    "דור אלון נתב\"ג",
    "מנטה נתב\"ג",
    "כרמיאל",
    "רעננה",
    "ירושלים",
    "באר שבע",
    "מודיעין לינד",
    "מודיעין ישפרו",
    "חדרה",
    "פתח תקווה",
    "עכו",
    "מזכרת בתיה",
    "אשדוד",
    "אשקלון",
    "רמת גן",
    "באר יעקב",
    "בית דגן",
    "קריית מלאכי",
    "שדי חמד",
    "יבנה",
    "חבצלת השרון",
    "כפר סבא",
    "נחלים",
    "חולון המלאכה",
    "גבעת שמואל",
    "נס ציונה",
    "ראשון לציון",
    "קריית גת",
    "פתח תקווה מפגש השרון",
    "בית שמש",
]

# Not real Cash On Tab branches -- expense categories Kateryna wants to file
# a חשבונית under so its cost shows up in the מלАי totals when she filters
# by them, without the invoice ever being entered into Cash On Tab (it has
# no matching סניף there to receive goods into). Kept separate from BRANCHES
# so detect_branch() never guesses one of these from invoice text, and so
# ready-for-entry can exclude them explicitly.
NON_INVENTORY_CATEGORIES = [
    "וואש פוינט",
    "השכורות",
    "שונה",
]


def detect_branch(text: str) -> str | None:
    """Best-effort guess at which branch an invoice belongs to, by checking
    whether every word of a branch name shows up somewhere in the extracted
    text. Word-set matching (not substring-of-whole-name) because pypdf's
    Hebrew RTL extraction can jumble word order within a line.

    Always just a starting guess -- shown as an editable dropdown in the UI
    so Kateryna can fix it in one click if it's wrong or missing.
    """
    if not text:
        return None
    candidates = []
    for branch in BRANCHES:
        words = [w for w in branch.replace('"', "").split() if w]
        if words and all(w in text for w in words):
            candidates.append(branch)
    if not candidates:
        return None
    # Prefer the most specific (most words) match, e.g. "פתח תקווה מפגש
    # השרון" over plain "פתח תקווה" if both technically match.
    candidates.sort(key=lambda b: len(b.split()), reverse=True)
    return candidates[0]
