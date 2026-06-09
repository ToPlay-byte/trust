import random
from dataclasses import dataclass, field


@dataclass
class ScenarioResult:
    success: bool
    interactions_count: int = 0
    reviews_count: int = 0
    comment: str | None = None
    error: str | None = None
    extra: dict | None = field(default_factory=dict)


class BaseScenario:
    name = "base"

    def __init__(self, params, logger):
        self.params = params
        self.logger = logger

    async def run(self, site_page, task) -> ScenarioResult:
        raise NotImplementedError

    def pick_queries(self) -> list[str]:
        queries = self.params.company_queries or []
        count = min(
            self.params.queries_to,
            len(queries)
        )
        if count <= 0 or not queries:
            return []
        return random.sample(queries, count)

    def get_random_interaction_limit(self) -> int:
        return random.randint(
            self.params.interactions_count_from,
            self.params.interactions_count_to,
        )
