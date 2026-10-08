"""OpenAI Responses API adapter with per-campaign budget and usage accounting.

One client per process. Stable instructions go first so prompt caching can
reuse the prefix; per-person content goes last.
"""

import asyncio
import json
import os
import time

from .operational_metrics import metrics


class BudgetExceeded(Exception):
    pass


class ModelError(Exception):
    pass


class OpenAIModel:
    demo = False

    def __init__(self, cache, api_key=None, model=None):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"], timeout=90, max_retries=0)
        self.model = model or os.environ.get("OPENAI_MODEL") or "gpt-5.6-terra"
        self.cache = cache
        self.sem = asyncio.Semaphore(2)  # shared by both workers: one account, one budget
        self._budget_lock = asyncio.Lock()

    async def _reserve_attempt(self, campaign_id):
        """Atomically charge one unit before an outbound provider attempt."""
        async with self._budget_lock:
            u = self.cache.usage(campaign_id)
            if u["api_calls"] >= u["budget"]:
                raise BudgetExceeded(f"campaign API budget of {u['budget']} calls used")
            self.cache.bump_usage(campaign_id, api_calls=1)

    async def structured(self, campaign_id, instructions, user_input, schema_name, schema, web_search=False):
        """Returns (parsed_json, cited_urls). Retries transient errors with backoff."""
        kwargs = dict(
            model=self.model,
            instructions=instructions,
            input=user_input,
            text={"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
        )
        if web_search:
            kwargs["tools"] = [{"type": "web_search"}]
            kwargs["include"] = ["web_search_call.action.sources"]
        delay = 2
        for attempt in range(3):
            started = None  # set only once the request goes out; a budget refusal is not a provider call
            try:
                async with self.sem:
                    await self._reserve_attempt(campaign_id)
                    started = time.perf_counter()
                    resp = await self.client.responses.create(**kwargs)
                metrics.provider_call(time.perf_counter() - started, retries=1 if attempt else 0)
                break
            except asyncio.CancelledError:
                raise
            except BudgetExceeded:
                raise
            except Exception as e:  # openai.APIError subclasses; keep message short
                if started is not None:  # every attempt after the first is a retry
                    metrics.provider_call(time.perf_counter() - started, retries=1 if attempt else 0)
                status = getattr(e, "status_code", None)
                if attempt == 2 or (status and status < 500 and status != 429):
                    raise ModelError(f"{type(e).__name__}: {str(e)[:200]}") from e
                await asyncio.sleep(delay)
                delay *= 3
        usage = getattr(resp, "usage", None)
        self.cache.bump_usage(
            campaign_id,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )
        try:
            data = json.loads(resp.output_text)
        except (json.JSONDecodeError, TypeError) as e:
            raise ModelError(f"unparseable model output: {e}") from e
        return data, _cited_urls(resp)


class Citations(set):
    """All URLs the provider returned (a plain set for old callers), split by how they were returned:
    `annotations` are claim-level url_citation annotations on the answer text; `inventory` are
    web_search result URLs the model merely had access to. Neither means the page was fetched."""

    def __init__(self, annotations=(), inventory=()):
        super().__init__(set(annotations) | set(inventory))
        self.annotations, self.inventory = set(annotations), set(inventory)


def _cited_urls(resp):
    annotations, inventory = set(), set()
    for item in getattr(resp, "output", []) or []:
        for part in getattr(item, "content", None) or []:
            for ann in getattr(part, "annotations", None) or []:
                if getattr(ann, "url", None):
                    annotations.add(ann.url)
        action = getattr(item, "action", None)
        for src in getattr(action, "sources", None) or []:
            if getattr(src, "url", None):
                inventory.add(src.url)
    return Citations(annotations, inventory)
