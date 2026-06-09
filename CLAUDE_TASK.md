
### What the logs show step by step

1. CTA `//span[text()="Write a review"]` resolves correctly in the DOM.
2. The element reports as visible and stable, but is **outside of the viewport** on every attempt.
3. After 3 failed pointer clicks, the bot falls back to `element.evaluate("el => el.click()")` (JS click).
4. The JS click succeeds and the log says `Review form opened via CTA`.
5. The page URL stays on `https://www.trustpilot.com/review/www.fiverr.com`.
6. The code then waits 20 seconds for star control selectors — all return 0 matches.
7. Task fails.

### Key diagnostic facts from the error

- `form_container_present=False` — no form wrapper found in DOM
- `iframe_count=4` — 4 iframes present (recaptcha x2, about:blank x2)
- `modal_present=False` — no modal overlay detected
- All star selectors checked in main frame and all iframes returned 0

---

## Root Cause Hypothesis

The CTA `span` element with text "Write a review" has class `styles_srOnly__dgHHY`
which is a **screen-reader only hidden element** — it is technically visible in the DOM
but visually hidden via CSS (position absolute, width/height 1px, overflow hidden, off-screen).

This means:
- The bot is clicking the wrong element — a hidden accessibility label, not the actual button.
- The actual clickable "Write a review" button is a different element — likely a `<button>` or `<a>` wrapping that span.
- Because the wrong element is clicked, the review form never actually opens.
- The URL stays on the company page and no form is rendered.

---

## What I Need You To Do

### Step 1 — Fix the CTA selector

Do not target `//span[text()="Write a review"]` directly.
That span is a hidden accessibility label inside the real button.

Instead:
- Target the **parent button or anchor** that contains this span.
- Good candidates:
  - `//button[.//span[text()="Write a review"]]`
  - `//a[.//span[text()="Write a review"]]`
  - `[data-business-unit-action="write-review"]`
  - `a[href*="/evaluate/"]`
  - `button:has(span.styles_srOnly__dgHHY)`
- Prefer the ancestor element that is actually in the viewport and clickable.
- Before clicking, verify the element is:
  - inside the viewport,
  - not hidden via `srOnly` or `visually-hidden` CSS class,
  - the actual interactive element.

### Step 2 — Verify the form actually opened

After clicking the CTA, do not immediately proceed to star detection.
Instead:
- Check whether the page navigated to `/evaluate/` URL — that means a new review page opened.
- Or check whether a review form container appeared on the same page.
- Wait for one of these conditions before looking for star controls:
  - URL changes to `/evaluate/`,
  - a form container with star inputs appears,
  - or a modal/overlay with the review form appears.
- Add a diagnostic log for which condition was met.

### Step 3 — Handle the `/evaluate/` redirect case

Trustpilot sometimes redirects to a separate page like:
`https://www.trustpilot.com/evaluate/www.fiverr.com`

If the page navigates there after the CTA click:
- Wait for the new page to fully load.
- Then look for star controls on the new URL.
- Do not look for stars on the original `/review/` URL.

### Step 4 — Star control detection

Only start looking for stars after confirming the form is open.
Try these selectors in order:
1. `//input[@name="star-selector"]`
2. `//input[@name="star-selector" and @value="4"]`
3. `//label[contains(@class, "star") and @for]`
4. `//button[contains(@aria-label, "4 star")]`
5. `//button[contains(@aria-label, "4 stars")]`
6. `[data-testid*="star"]`
7. Any `input[type="radio"]` with a value matching the target rating
8. Any clickable element with `aria-label` containing the rating number

### Step 5 — Logging

Add clear logs for:
- which CTA element was found and its tag/class,
- whether the element is inside the viewport before clicking,
- whether the URL changed after CTA click,
- which URL the code is on when looking for stars,
- which selector finally found the star control (or all failed),
- whether the form container was detected.

### Step 6 — Failure message

If stars still cannot be found after all fallbacks, raise RuntimeError with:
- current URL,
- whether CTA was clicked,
- whether form container was found,
- which URL was expected vs actual,
- which selectors were tried.

---

## What NOT to change

- Do not touch the company page validation logic — it is working correctly.
- Do not touch `open_company_page()` or `_validate_company_page()` unless required.
- Do not touch `BrowserSession.start()`.
- Keep the JS fallback click as a last resort, but fix the selector first.

---

## Expected Outcome

After this fix:
1. The CTA click targets the real interactive button, not the hidden `srOnly` span.
2. The bot correctly detects when the review form has opened.
3. Star controls are found and clicked successfully.
4. The review is submitted.
5. If anything fails, the error message clearly explains what happened and where.