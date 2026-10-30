"""Single-table items for bot v2 features in ``vieclambot-users``.

Sort keys (PK is always the Telegram chat id):

- ``PREF``                 alert settings + default /filter choices
- ``SAVED#<identity>``     job snapshot the user bookmarked (renders without the jobs table)
- ``HIDDEN#<identity>``    never show this job again (🙈 or 👎)
- ``FEEDBACK#<identity>``  👍/👎 vote with the employer, used for soft down-ranking
- ``EXCLUDE#<normalized>`` blacklisted keyword or company
- ``FOLLOW#<normalized>``  followed company (alerts include its new postings)
- ``BOT#ONBOARD``          short-lived onboarding wizard state

``EXCLUDE#`` < ``FEEDBACK#`` < ``FOLLOW#`` < ``HIDDEN#`` < ``PREF`` sort together,
so :meth:`UserStore.load_context` reads all of them with a single Query.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

from src.bot.filters import UserContext, normalize_term
from src.config import get_settings

PREF_DEFAULTS = {
    "frequency": "instant",  # instant | twice | daily | weekly
    "quiet_hours": os.environ.get("VIECLAMBOT_QUIET_HOURS_DEFAULT", "false").lower()
    in {"1", "true", "yes", "on"},
    "max_jobs": 0,  # 0 = settings.alert_max_jobs_per_user
    "language": "vi",
    "filter_location": "all",
    "filter_salary_min": 0,
    "filter_level": "any",
    "filter_job_type": "any",
}
HIDDEN_TTL_DAYS = int(os.environ.get("VIECLAMBOT_HIDDEN_TTL_DAYS", "180"))
MAX_EXCLUDES = int(os.environ.get("VIECLAMBOT_MAX_EXCLUDES", "30"))
MAX_FOLLOWS = int(os.environ.get("VIECLAMBOT_MAX_FOLLOWS", "20"))
_SAVED_FIELDS = {"title": 160, "company": 80, "location": 80, "salary_raw": 70,
                 "source": 30, "source_url": 900}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(value, length: int) -> str:
    value = str(value or "")
    return value if len(value) <= length else value[: length - 1] + "…"


def compact_job(job: dict) -> dict:
    """The few fields a card needs, bounded so items stay tiny."""
    item = {key: _clip(job.get(key), length) for key, length in _SAVED_FIELDS.items()}
    if len(str(job.get("source_url") or "")) > _SAVED_FIELDS["source_url"]:
        item["source_url"] = ""
    return item


def _alive(item: dict, now: float) -> bool:
    ttl = item.get("ttl")
    return ttl is None or int(ttl) > now


class UserStore:
    def __init__(self, table=None, settings=None):
        settings = settings or get_settings()
        self.table = table or boto3.resource(
            "dynamodb", region_name=settings.aws_region
        ).Table(settings.dynamodb_users_table)

    # ── Reads ─────────────────────────────────────────────────────

    def _query_prefix(self, user_id: str, prefix: str) -> list[dict]:
        items = []
        kwargs = {"KeyConditionExpression": Key("user_id").eq(user_id)
                  & Key("sk").begins_with(prefix)}
        while True:
            response = self.table.query(**kwargs)
            items.extend(response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                return items
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def load_context(self, user_id: str) -> UserContext:
        """PREF + EXCLUDE# + FEEDBACK# + FOLLOW# + HIDDEN# in one paginated Query."""
        ctx = UserContext(prefs=dict(PREF_DEFAULTS))
        now = time.time()
        kwargs = {"KeyConditionExpression": Key("user_id").eq(user_id)
                  & Key("sk").between("EXCLUDE#", "PREF")}
        while True:
            response = self.table.query(**kwargs)
            for item in response.get("Items", []):
                if not _alive(item, now):
                    continue
                sk = item["sk"]
                if sk == "PREF":
                    ctx.prefs.update({k: v for k, v in item.items()
                                      if k not in {"user_id", "sk"}})
                elif sk.startswith("HIDDEN#"):
                    ctx.hidden.add(sk[7:])
                elif sk.startswith("EXCLUDE#"):
                    ctx.excluded[sk[8:]] = str(item.get("raw") or sk[8:])
                elif sk.startswith("FOLLOW#"):
                    ctx.follows[sk[7:]] = str(item.get("name") or sk[7:])
                elif sk.startswith("FEEDBACK#") and item.get("vote") == "down":
                    company = str(item.get("company_normalized") or "")
                    if company:
                        ctx.disliked_companies.add(company)
            if not response.get("LastEvaluatedKey"):
                return ctx
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def get_prefs(self, user_id: str) -> dict:
        item = self.table.get_item(Key={"user_id": user_id, "sk": "PREF"}).get("Item", {})
        prefs = dict(PREF_DEFAULTS)
        prefs.update({k: v for k, v in item.items() if k not in {"user_id", "sk"}})
        return prefs

    def list_saved(self, user_id: str) -> list[dict]:
        items = self._query_prefix(user_id, "SAVED#")
        items.sort(key=lambda item: str(item.get("saved_at", "")), reverse=True)
        return items

    def list_excludes(self, user_id: str) -> list[dict]:
        return self._query_prefix(user_id, "EXCLUDE#")

    def list_follows(self, user_id: str) -> list[dict]:
        return self._query_prefix(user_id, "FOLLOW#")

    def has_subscriptions(self, user_id: str) -> bool:
        response = self.table.query(
            KeyConditionExpression=Key("user_id").eq(user_id) & Key("sk").begins_with("SUB#"),
            Limit=1,
        )
        return bool(response.get("Items"))

    def history(self, user_id: str, days: int = 7) -> list[dict]:
        """Jobs alerts delivered in the last ``days`` (only SENT# rows that carry a title)."""
        cutoff = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).isoformat()
        rows = [
            item for item in self._query_prefix(user_id, "SENT#")
            if item.get("title") and str(item.get("sent_at", "")) >= cutoff
        ]
        rows.sort(key=lambda item: str(item.get("sent_at", "")), reverse=True)
        return rows

    def find_job_payload(self, user_id: str, key: str) -> dict | None:
        """Card fields for a button press when the message text could not be parsed."""
        sent = self.table.get_item(Key={"user_id": user_id, "sk": f"SENT#{key}"}).get("Item")
        if sent and sent.get("title"):
            return {k: sent.get(k, "") for k in _SAVED_FIELDS}
        state = self.table.get_item(Key={"user_id": user_id, "sk": "SEARCH#LAST"}).get("Item")
        for job in (state or {}).get("jobs", []):
            if job.get("identity") == key:
                return {k: job.get(k, "") for k in _SAVED_FIELDS}
        return None

    # ── Writes (each is a single request) ─────────────────────────

    def set_pref(self, user_id: str, **values) -> dict:
        """Update PREF fields in one request and return the merged preferences."""
        names = {f"#k{i}": name for i, name in enumerate(values)}
        vals = {f":v{i}": value for i, value in enumerate(values.values())}
        expression = ", ".join(f"#k{i} = :v{i}" for i in range(len(values)))
        names["#u"] = "updated_at"
        vals[":u"] = _now_iso()
        item = self.table.update_item(
            Key={"user_id": user_id, "sk": "PREF"},
            UpdateExpression=f"SET {expression}, #u = :u",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=vals,
            ReturnValues="ALL_NEW",
        ).get("Attributes", {})
        prefs = dict(PREF_DEFAULTS)
        prefs.update({k: v for k, v in item.items() if k not in {"user_id", "sk"}})
        return prefs

    def save_job(self, user_id: str, key: str, job: dict) -> None:
        self.table.put_item(Item={
            "user_id": user_id, "sk": f"SAVED#{key}", **compact_job(job),
            "saved_at": _now_iso(),
        })

    def unsave_job(self, user_id: str, key: str) -> None:
        self.table.delete_item(Key={"user_id": user_id, "sk": f"SAVED#{key}"})

    def _hidden_item(self, user_id: str, key: str, reason: str) -> dict:
        return {"user_id": user_id, "sk": f"HIDDEN#{key}", "reason": reason,
                "hidden_at": _now_iso(), "ttl": int(time.time()) + HIDDEN_TTL_DAYS * 86400}

    def hide_job(self, user_id: str, key: str) -> None:
        self.table.put_item(Item=self._hidden_item(user_id, key, "hide"))

    def feedback(self, user_id: str, key: str, vote: str, job: dict) -> None:
        """👍 is one put; 👎 also hides the job, both rows in one BatchWriteItem."""
        company = str(job.get("company") or "")
        item = {
            "user_id": user_id, "sk": f"FEEDBACK#{key}", "vote": vote,
            "title": _clip(job.get("title"), 160), "company": _clip(company, 80),
            "company_normalized": normalize_term(company)[:80], "voted_at": _now_iso(),
        }
        if vote != "down":
            self.table.put_item(Item=item)
            return
        with self.table.batch_writer() as batch:
            batch.put_item(Item=item)
            batch.put_item(Item=self._hidden_item(user_id, key, "down"))

    def add_exclude(self, user_id: str, raw: str) -> str:
        normalized = normalize_term(raw)[:80]
        self.table.put_item(Item={"user_id": user_id, "sk": f"EXCLUDE#{normalized}",
                                  "raw": _clip(raw.strip(), 80), "created_at": _now_iso()})
        return normalized

    def remove_exclude(self, user_id: str, raw: str) -> bool:
        normalized = normalize_term(raw)[:80]
        response = self.table.delete_item(
            Key={"user_id": user_id, "sk": f"EXCLUDE#{normalized}"}, ReturnValues="ALL_OLD"
        )
        return bool(response.get("Attributes"))

    def follow(self, user_id: str, name: str) -> str:
        normalized = normalize_term(name)[:80]
        self.table.put_item(Item={"user_id": user_id, "sk": f"FOLLOW#{normalized}",
                                  "name": _clip(name.strip(), 80), "created_at": _now_iso()})
        return normalized

    def unfollow(self, user_id: str, name: str) -> bool:
        normalized = normalize_term(name)[:80]
        response = self.table.delete_item(
            Key={"user_id": user_id, "sk": f"FOLLOW#{normalized}"}, ReturnValues="ALL_OLD"
        )
        return bool(response.get("Attributes"))

    def get_onboarding(self, user_id: str) -> dict:
        item = self.table.get_item(
            Key={"user_id": user_id, "sk": "BOT#ONBOARD"}, ConsistentRead=True
        ).get("Item", {})
        return item if _alive(item, time.time()) else {}

    def set_onboarding(self, user_id: str, **values) -> None:
        names = {f"#k{i}": name for i, name in enumerate(values)}
        vals = {f":v{i}": value for i, value in enumerate(values.values())}
        expression = ", ".join(f"#k{i} = :v{i}" for i in range(len(values)))
        names["#t"] = "ttl"
        vals[":t"] = int(time.time()) + 3600
        self.table.update_item(
            Key={"user_id": user_id, "sk": "BOT#ONBOARD"},
            UpdateExpression=f"SET {expression}, #t = :t",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=vals,
        )

    def clear_onboarding(self, user_id: str) -> None:
        self.table.delete_item(Key={"user_id": user_id, "sk": "BOT#ONBOARD"})
