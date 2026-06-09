from actions import (
    assert_authenticated,
    generate_review_text_openai,
    get_reviews,
    login_to_site,
    open_company_page,
    post_review as _post_review,
    read_company_longer_reviews,
    read_company_reviews,
)
from services.task_logger import TaskLogger


class SitePage:
    def __init__(self, page, logger: TaskLogger):
        self.page = page
        self.logger = logger

    async def login(self, task):
        self.page = await login_to_site(self.page, task)

    async def assert_session(self, task):
        """Verify the browser has an active Trustpilot session.

        Raises SessionExpiredError if no session is detected, which causes
        task_runner to pause the profile and require manual relogin.
        Can be called before any action that requires authentication.
        """
        await assert_authenticated(self.page, self.logger)

    async def open_company_page(self, company_query, task=None, restricted_queries=None, multiplier_delay=1.0):
        await open_company_page(
            self.page,
            company_query,
            restricted_queries=restricted_queries,
            multiplier_delay=multiplier_delay,
            task=task,
        )

    async def read_reviews(self, task=None, interactions_count_limit=0, interactions_count_current=0,
                           multiplier_delay=1.0, limit_on_reviews_duration=None):
        return await read_company_reviews(
            self.page,
            method="see_all",
            interactions_count_limit=interactions_count_limit,
            interactions_count_current=interactions_count_current,
            maximum_interactions=1 if interactions_count_limit > 0 else 0,
            multiplier_delay=multiplier_delay,
            task=task,
        )

    async def read_longer_reviews(self, task=None, interactions_count_limit=0, interactions_count_current=0,
                                  multiplier_delay=1.0, maximum_longer_reviews=None, maximum_review_pages=None,
                                  limit_on_reviews_duration=None):
        return await read_company_longer_reviews(
            self.page,
            maximum_longer_reviews=maximum_longer_reviews,
            maximum_review_pages=maximum_review_pages,
            multiplier_delay=multiplier_delay,
            reviews_duration_limit=limit_on_reviews_duration,
            task=task,
        )

    async def post_review(self, task, target_company, review_length, prompt_parameters, prompts, multiplier_delay=1.0):
        await open_company_page(
            self.page,
            target_company,
            multiplier_delay=multiplier_delay,
            task=task,
        )

        # Capture the canonical Trustpilot URL resolved by open_company_page.
        # target_company is a display name (e.g. "Blue Tomato"); the actual slug
        # ("www.blue-tomato.com") lives in the URL Trustpilot redirected to.
        # Using target_company directly would produce /review/Blue%20Tomato — a 404.
        canonical_review_url = self.page.url
        self.logger.console(
            f"SitePage.post_review:: target='{target_company}', "
            f"canonical URL: {canonical_review_url}"
        )

        title, href, reviews_example = await get_reviews(page=self.page, task=task)

        title, text = await generate_review_text_openai(
            prompts=prompts,
            company_details=[title, href] if not reviews_example else None,
            reviews=reviews_example,
            prompt_parameters=prompt_parameters,
            review_length=review_length,
            task=task,
        )

        # Open a dedicated tab for the review submission flow.
        # self.page is shared across concurrent tasks; any other task can navigate it
        # at any moment.  A new tab in the same browser context is fully isolated
        # while still sharing session cookies, so authentication carries over.
        review_tab = await self.page.context.new_page()
        self.logger.console(
            f"SitePage.post_review:: Opened dedicated review tab for '{target_company}'."
        )
        try:
            # Navigate to the canonical URL, not a URL built from the display name.
            await review_tab.goto(
                canonical_review_url,
                wait_until="domcontentloaded",
            )
            return await _post_review(
                page=review_tab,
                review_title=title,
                review_text=text,
                rating=4,
                target_company=target_company,
                multiplier_delay=multiplier_delay,
                task=task,
                skip_preflight=True,
            )
        finally:
            try:
                await review_tab.close()
                self.logger.console(
                    f"SitePage.post_review:: Closed dedicated review tab for '{target_company}'."
                )
            except Exception:
                pass
