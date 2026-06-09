from scenarios.base import BaseScenario, ScenarioResult


class ReadReviewsTemplate(BaseScenario):
    """Login, browse queries, read standard reviews. Optionally time-limited and post a review."""
    with_duration_limit: bool = False
    with_post_review: bool = False

    async def run(self, site_page, task) -> ScenarioResult:
        await site_page.login(task)

        current_interactions = 0
        current_reviews = 0
        limit = self.get_random_interaction_limit()

        for query in self.pick_queries():
            await site_page.open_company_page(
                company_query=query,
                task=task,
                restricted_queries=self.params.restricted_company_queries,
                multiplier_delay=self.params.pause_multiplier,
            )
            kwargs = dict(
                task=task,
                interactions_count_limit=limit,
                interactions_count_current=current_interactions,
                multiplier_delay=self.params.pause_multiplier,
            )
            if self.with_duration_limit:
                kwargs["limit_on_reviews_duration"] = self.params.limit_on_reviews_duration_seconds
            made = await site_page.read_reviews(**kwargs)
            current_interactions += made or 0

        if self.with_post_review and self.params.target_company:
            await site_page.assert_session(task)
            posted = await site_page.post_review(
                task=task,
                target_company=self.params.target_company,
                review_length=self.params.review_length,
                prompt_parameters=self.params.prompt_parameters,
                prompts=self.params.prompts,
                multiplier_delay=self.params.pause_multiplier,
            )
            if not posted:
                return ScenarioResult(
                    success=False,
                    interactions_count=current_interactions,
                    reviews_count=0,
                    error="Review submission was not confirmed.",
                )
            current_reviews += 1

        return ScenarioResult(
            success=True,
            interactions_count=current_interactions,
            reviews_count=current_reviews,
        )


class ReadLongerReviewsTemplate(BaseScenario):
    """Login, browse queries, read longer reviews. Optionally time-limited and post a review."""
    with_duration_limit: bool = False
    with_post_review: bool = False

    async def run(self, site_page, task) -> ScenarioResult:
        await site_page.login(task)

        current_interactions = 0
        current_reviews = 0
        limit = self.get_random_interaction_limit()

        for query in self.pick_queries():
            await site_page.open_company_page(
                company_query=query,
                task=task,
                restricted_queries=self.params.restricted_company_queries,
                multiplier_delay=self.params.pause_multiplier,
            )
            kwargs = dict(
                task=task,
                interactions_count_limit=limit,
                interactions_count_current=current_interactions,
                multiplier_delay=self.params.pause_multiplier,
                maximum_longer_reviews=self.params.maximum_longer_reviews,
                maximum_review_pages=self.params.maximum_review_pages,
            )
            if self.with_duration_limit:
                kwargs["limit_on_reviews_duration"] = self.params.limit_on_reviews_duration_seconds
            made = await site_page.read_longer_reviews(**kwargs)
            current_interactions += made or 0

        if self.with_post_review and self.params.target_company:
            await site_page.assert_session(task)
            posted = await site_page.post_review(
                task=task,
                target_company=self.params.target_company,
                review_length=self.params.review_length,
                prompt_parameters=self.params.prompt_parameters,
                prompts=self.params.prompts,
                multiplier_delay=self.params.pause_multiplier,
            )
            if not posted:
                return ScenarioResult(
                    success=False,
                    interactions_count=current_interactions,
                    reviews_count=0,
                    error="Review submission was not confirmed.",
                )
            current_reviews += 1

        return ScenarioResult(
            success=True,
            interactions_count=current_interactions,
            reviews_count=current_reviews,
        )
