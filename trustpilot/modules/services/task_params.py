from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TaskParams:
    pause_multiplier: float = 1.0
    interactions_count_from: int = 1
    interactions_count_to: int = 2
    queries_from: int = 2
    queries_to: int = 3
    company_queries: List[str] = field(default_factory=list)
    restricted_company_queries: Optional[List[str]] = None
    target_company: Optional[str] = None
    review_length: Optional[int] = None
    prompt_parameters: Optional[dict] = None
    prompts: Optional[List[str]] = None
    limit_on_reviews_duration: Optional[int] = None
    maximum_longer_reviews: int = 4
    maximum_review_pages: int = 4

    @property
    def limit_on_reviews_duration_seconds(self) -> Optional[int]:
        return self.limit_on_reviews_duration


class TaskParamsFactory:
    @staticmethod
    def from_task(task) -> TaskParams:
        def _value(field_name, default):
            v = getattr(task, field_name, None)
            return default if v is None else v

        def _optional(field_name):
            v = getattr(task, field_name, None)
            if isinstance(v, str):
                v = v.strip()
            if v in (None, "", [], {}):
                return None
            return v

        return TaskParams(
            pause_multiplier=_value("pause_multiplier", 1.0),
            interactions_count_from=_value("interactions_count_from", 1),
            interactions_count_to=_value("interactions_count_to", 2),
            queries_from=_value("company_queries_count_from", 2),
            queries_to=_value("company_queries_count_to", 3),
            company_queries=_value("company_queries", []) or [],
            restricted_company_queries=_optional("restricted_company_queries"),
            target_company=_optional("target_company"),
            review_length=_optional("review_length"),
            prompt_parameters=_optional("prompt_parameters"),
            prompts=_optional("prompts") or [],
            limit_on_reviews_duration=_optional("limit_on_reviews_duration"),
            maximum_longer_reviews=_value("maximum_longer_reviews", 4),
            maximum_review_pages=_value("maximum_review_pages", 4),
        )
