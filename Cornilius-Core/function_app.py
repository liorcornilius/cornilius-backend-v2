import azure.functions as func
import logging
import os
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone, date

from supabase import create_client
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient


app = func.FunctionApp()


# ---------------------------------------------------------------------
# Key Vault / Supabase connection
# ---------------------------------------------------------------------
def _get_secret_from_keyvault(secret_names):
    vault_name = os.getenv("KEY_VAULT_NAME") or "cornilkeychain"
    vault_url = os.getenv("KEY_VAULT_URL") or f"https://{vault_name}.vault.azure.net"

    try:
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_url, credential=credential)
    except Exception:
        return None

    for name in secret_names:
        try:
            secret = client.get_secret(name)
            if secret and secret.value:
                return secret.value
        except Exception:
            continue
    return None


def get_supabase_client():
    url = _get_secret_from_keyvault(["SUPABASE-URL"])
    key = _get_secret_from_keyvault(["SUPABASE-SERVICE-ROLE-KEY"])

    if not url:
        url = os.getenv("SUPABASE_URL") or os.getenv("SUPABASE-URL")
    if not key:
        key = (
            os.getenv("SUPABASE_SERVICE_ROLE_KEY")
            or os.getenv("SUPABASE-SERVICE-ROLE-KEY")
            or os.getenv("SUPABASE_KEY")
        )

    if not url or not key:
        raise RuntimeError("Supabase URL / key not found")

    return create_client(url, key)


# ---------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------
def fetch_active_goals(supabase, user_id: str):
    resp = (
        supabase.table("goals")
        .select(
            "id,tracker_id,description,"
            "frequency,frequency_unit,"
            "threshold_min,threshold_max,threshold_unit,"
            "goal_start_date,target_value,conditions,metadata"
        )
        .eq("user_id", user_id)
        .eq("is_active", True)
        .execute()
    )
    return resp.data or []


def fetch_active_goal_meta(supabase, user_id: str):
    resp = (
        supabase.table("goals")
        .select("meta")
        .eq("user_id", user_id)
        .eq("status", "active")
        .execute()
    )
    return resp.data or []


def fetch_logs(supabase, user_id: str, tracker_id: str, start_iso: str, end_iso: str):
    resp = (
        supabase.table("logs")
        .select("value_number,timestamp")
        .eq("user_id", user_id)
        .eq("tracker_id", tracker_id)
        .gte("timestamp", start_iso)
        .lt("timestamp", end_iso)
        .execute()
    )
    return resp.data or []


def fetch_last_full_run(supabase, goal_id: str):
    resp = (
        supabase.table("goal_period_results")
        .select("period_end,period_index")
        .eq("goal_id", goal_id)
        .eq("is_full_run", True)
        .order("period_end", desc=True)
        .limit(1)
        .execute()
    )
    data = resp.data or []
    return data[0] if data else None


def upsert_goal_period_result(supabase, record: dict):
    return (
        supabase.table("goal_period_results")
        .upsert(record, on_conflict="goal_id,period_start,period_end,run_day,is_full_run")
        .execute()
    )


# ---------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------
def parse_dt(x):
    if isinstance(x, datetime):
        return x.astimezone(timezone.utc)
    if isinstance(x, date):
        return datetime(x.year, x.month, x.day, tzinfo=timezone.utc)
    if not x:
        return None
    return datetime.fromisoformat(str(x).replace("Z", "+00:00")).astimezone(timezone.utc)


def monday_utc(dt: datetime):
    dt = dt.astimezone(timezone.utc)
    m = dt - timedelta(days=dt.weekday())
    return datetime(m.year, m.month, m.day, tzinfo=timezone.utc)


def week_period(anchor_dt: datetime):
    start = monday_utc(anchor_dt)
    end = start + timedelta(days=7)
    return start, end


def iso_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    return value.isoformat()


def iso_datetime(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        value = parse_dt(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


# ---------------------------------------------------------------------
# Azure OpenAI helpers
# ---------------------------------------------------------------------
def get_azure_openai_config():
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("AZURE-OPENAI-ENDPOINT")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT") or os.getenv("AZURE-OPENAI-DEPLOYMENT")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION") or "2024-02-15-preview"

    api_key = _get_secret_from_keyvault(
        [
            "AZURE-OPENAI-KEY",
            "AZURE_OPENAI_KEY",
            "AZURE-OPENAI-API-KEY",
            "AZURE_OPENAI_API_KEY",
            "OPENAI_API_KEY",
        ]
    )
    if not api_key:
        api_key = (
            os.getenv("AZURE_OPENAI_KEY")
            or os.getenv("AZURE_OPENAI_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )

    return endpoint, deployment, api_version, api_key


def build_goal_extraction_system_prompt(user_goals):
    goals_json = json.dumps(user_goals)
    return "\n".join(
        [
            "You are a strict JSON extraction engine for user goal logging.",
            "",
            "USER GOALS (ONLY THESE ARE ALLOWED MATCHES):",
            goals_json,
            "",
            "NON-NEGOTIABLE RULES:",
            "1) The user may describe ONE OR MORE activities in USER_TEXT. You MUST extract all clearly performed activities.",
            "2) For EACH extracted activity, output ONE item in items[].",
            "3) You MUST match ONLY against USER GOALS listed above. Never invent goals.",
            "4) If an activity matches a goal:",
            "   - set status = \"matched\"",
            "   - copy goal.id EXACTLY from USER GOALS (verbatim UUID)",
            "   - copy goal.name EXACTLY from USER GOALS",
            "   - copy goal.domain EXACTLY from USER GOALS",
            "5) If an activity does NOT match any goal:",
            "   - set status = \"goal_not_found\"",
            "   - set goal = null",
            "   - set activity_text to the activity phrase exactly as written by the user",
            "6) Spelling tolerance:",
            "   - Minor spelling mistakes and tense changes are allowed if intent is obvious",
            "   - If intent is not obvious, treat as goal_not_found",
            "7) Only log activities the user clearly DID.",
            "   - Do NOT log future plans, wishes, hypotheticals, or negations",
            "8) Value/unit extraction:",
            "   - If value and unit are present, extract them",
            "   - If missing, set value_number = null and unit = null",
            "",
            "9) Timestamp rule:",
            "   - CURRENT_TIME will be provided in the user message",
            "   - occurred_at is REQUIRED but MAY be null",
            "   - If an explicit time is present, convert it to ISO-8601",
            "   - If NO explicit time is present, occurred_at = null",
            "   - If only \"today\" is mentioned, occurred_at = null",
            "",
            "10) Output JSON only. No prose. No markdown.",
            "",
            "OUTPUT FORMAT (MUST MATCH EXACTLY):",
            "{",
            "  \"items\": [",
            "    {",
            "      \"status\": \"matched\" | \"goal_not_found\",",
            "      \"activity_text\": \"<string>\",",
            "      \"goal\": {",
            "        \"id\": \"<uuid>\",",
            "        \"name\": \"<string>\",",
            "        \"domain\": \"<string>\"",
            "      } | null,",
            "      \"value_number\": <number|null>,",
            "      \"unit\": <string|null>,",
            "      \"occurred_at\": <\"ISO-8601 timestamp\" | null>,",
            "      \"confidence\": <number 0..1>",
            "    }",
            "  ]",
            "}",
        ]
    )


def call_azure_openai_chat(system_prompt: str, user_text: str, current_time: str):
    endpoint, deployment, api_version, api_key = get_azure_openai_config()
    if not endpoint or not deployment or not api_key:
        raise RuntimeError("Azure OpenAI configuration missing")

    url = (
        f"{endpoint}/openai/deployments/{deployment}/chat/completions"
        f"?api-version={api_version}"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"USER_TEXT: {user_text}\nCURRENT_TIME: {current_time}"},
    ]
    payload = {
        "messages": messages,
        "temperature": 0,
        "top_p": 1,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "api-key": api_key},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Azure OpenAI error: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Azure OpenAI connection error: {e}") from e

    response_payload = json.loads(body)
    choices = response_payload.get("choices") or []
    if not choices:
        raise RuntimeError("Azure OpenAI returned no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not content:
        raise RuntimeError("Azure OpenAI returned empty content")
    return content


# ---------------------------------------------------------------------
# Evaluation logic
# ---------------------------------------------------------------------
def evaluate_logs(logs, threshold_min, threshold_max):
    hits = 0
    agg = 0.0

    for log in logs:
        v = log.get("value_number")
        if v is None:
            continue
        try:
            v = float(v)
        except Exception:
            continue

        if threshold_min is not None and v < float(threshold_min):
            continue
        if threshold_max is not None and v > float(threshold_max):
            continue

        hits += 1
        agg += v

    return hits, agg


def evaluate_goal_weekly(supabase, user_id: str, goal: dict, now: datetime):
    goal_id = goal["id"]
    tracker_id = goal["tracker_id"]

    frequency = int(goal.get("frequency") or 0)
    threshold_min = goal.get("threshold_min")
    threshold_max = goal.get("threshold_max")

    goal_start = parse_dt(goal.get("goal_start_date")) or now

    last_full = fetch_last_full_run(supabase, goal_id)
    if last_full:
        next_start = parse_dt(last_full["period_end"])  # period_end is DATE in DB
        period_index = int(last_full["period_index"]) + 1
    else:
        next_start = goal_start
        period_index = 1

    run_day_date = now.date()
    run_day_str = run_day_date.isoformat()  # for response only

    rows_for_response = []

    while next_start < now:
        period_start_dt, period_end_dt = week_period(next_start)

        is_full_run = now >= period_end_dt
        measure_end = period_end_dt if is_full_run else now

        logs = fetch_logs(
            supabase,
            user_id,
            tracker_id,
            period_start_dt.isoformat(),
            measure_end.isoformat(),
        )

        hits, agg = evaluate_logs(logs, threshold_min, threshold_max)
        goal_reached = 1 if hits >= frequency and frequency > 0 else 0

        # DB columns only (matches your goal_period_results schema)
        record = {
            "user_id": user_id,
            "goal_id": goal_id,

            "period_type": "week",
            "period_index": period_index,
            "period_start": iso_date(period_start_dt),
            "period_end": iso_date(period_end_dt),
            "next_period_start": iso_date(period_end_dt),

            "target_success_count": frequency,
            "actual_success_count": hits,
            "target_value": goal.get("target_value"),
            "actual_value_agg": agg,

            "status": "met" if goal_reached else "not_met",
            "is_full_run": bool(is_full_run),
            "goal_reached": int(goal_reached),

            "run_date": iso_datetime(now),
            "run_day": iso_date(run_day_date),
            "updated_at": iso_datetime(now),

            "metadata": {
                "description": goal.get("description"),
                "tracker_id": tracker_id,
                "frequency": frequency,
                "frequency_unit": goal.get("frequency_unit"),
                "threshold_min": threshold_min,
                "threshold_max": threshold_max,
                "threshold_unit": goal.get("threshold_unit"),
                "conditions": goal.get("conditions"),
            },
        }

        upsert_goal_period_result(supabase, record)

        # Response payload: strings only (no Python date objects)
        rows_for_response.append({
            "period_start": record["period_start"],
            "period_end": record["period_end"],
            "run_day": run_day_str,
            "is_full_run": record["is_full_run"],
            "goal_reached": record["goal_reached"],
            "actual_success_count": hits,
            "status": record["status"],
        })

        if not is_full_run:
            break

        next_start = period_end_dt
        period_index += 1

    return rows_for_response


def evaluate_goals_for_user(user_id: str):
    now = datetime.now(timezone.utc)
    supabase = get_supabase_client()

    goals = fetch_active_goals(supabase, user_id)

    evaluated = []
    for goal in goals:
        if (goal.get("frequency_unit") or "").lower() != "week":
            continue

        rows = evaluate_goal_weekly(supabase, user_id, goal, now)
        evaluated.append({
            "goal_id": goal["id"],
            "description": goal.get("description"),
            "rows": rows,
        })

    return {
        "user_id": user_id,
        "goal_count": len(evaluated),
        "evaluated_goals": evaluated,
    }


@app.route(route="evaluate_goals", auth_level=func.AuthLevel.FUNCTION, methods=["GET", "POST"])
def evaluate_goals(req: func.HttpRequest) -> func.HttpResponse:
    user_id = req.params.get("user_id")
    if not user_id:
        try:
            user_id = req.get_json().get("user_id")
        except Exception:
            user_id = None

    if not user_id:
        return func.HttpResponse("Missing user_id", status_code=400)

    try:
        result = evaluate_goals_for_user(user_id)
        payload = {
            "success": True,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        }
        return func.HttpResponse(
            json.dumps(payload, default=str),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.exception("Error evaluating goals")
        return func.HttpResponse(
            json.dumps({"success": False, "error": str(e)}, default=str),
            mimetype="application/json",
            status_code=500,
        )


@app.route(route="log_result", auth_level=func.AuthLevel.FUNCTION, methods=["GET", "POST"])
def log_result(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = req.get_json()
    except Exception:
        return func.HttpResponse("Invalid JSON", status_code=400)

    if not isinstance(payload, dict):
        return func.HttpResponse("Invalid JSON", status_code=400)

    user_id = payload.get("user_id")
    if not user_id:
        return func.HttpResponse("Missing user_id", status_code=400)

    tracker_id = payload.get("tracker_id")
    if not tracker_id:
        return func.HttpResponse("Missing tracker_id", status_code=400)

    value_number = payload.get("value_number")
    value_text = payload.get("value_text")
    value_json = payload.get("value_json")
    metadata = payload.get("metadata")

    value_count = sum(
        1
        for value in (value_number, value_text, value_json)
        if value is not None
    )
    if value_count != 1:
        return func.HttpResponse(
            "Exactly one of value_number, value_text, value_json is required",
            status_code=400,
        )

    if value_number is not None:
        try:
            value_number = float(value_number)
        except Exception:
            return func.HttpResponse("Invalid value_number", status_code=400)

    if value_text is not None and not isinstance(value_text, str):
        return func.HttpResponse("Invalid value_text", status_code=400)

    if value_json is not None:
        try:
            json.dumps(value_json)
        except Exception:
            return func.HttpResponse("Invalid value_json", status_code=400)

    if metadata is not None:
        try:
            json.dumps(metadata)
        except Exception:
            return func.HttpResponse("Invalid metadata", status_code=400)

    if "timestamp" in payload:
        timestamp = payload.get("timestamp")
        if timestamp is None:
            return func.HttpResponse("Invalid timestamp", status_code=400)
        timestamp = parse_dt(timestamp)
        if not timestamp:
            return func.HttpResponse("Invalid timestamp", status_code=400)
        timestamp = iso_datetime(timestamp)
    else:
        timestamp = None

    try:
        supabase = get_supabase_client()
        record = {
            "user_id": user_id,
            "tracker_id": tracker_id,
            "value_number": value_number if value_number is not None else None,
            "value_text": value_text if value_text is not None else None,
            "value_json": value_json if value_json is not None else None,
        }
        if metadata is not None:
            record["metadata"] = metadata
        if "timestamp" in payload:
            record["timestamp"] = timestamp

        resp = supabase.table("logs").insert(record).execute()
        data = resp.data or []
        inserted = data[0] if data else None

        response_payload = {
            "success": True,
            "result": inserted,
        }
        return func.HttpResponse(
            json.dumps(response_payload, default=str),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.exception("Error logging result")
        return func.HttpResponse(
            json.dumps({"success": False, "error": str(e)}, default=str),
            mimetype="application/json",
            status_code=500,
        )


@app.route(route="text_to_goal_json", auth_level=func.AuthLevel.FUNCTION, methods=["GET", "POST"])
def text_to_goal_json(req: func.HttpRequest) -> func.HttpResponse:
    try:
        payload = req.get_json()
    except Exception:
        payload = {}

    user_id = req.params.get("user_id") or payload.get("user_id")
    user_text = req.params.get("user_text") or payload.get("user_text")
    current_time = req.params.get("current_time") or payload.get("current_time")

    if not user_id:
        return func.HttpResponse("Missing user_id", status_code=400)
    if not user_text:
        return func.HttpResponse("Missing user_text", status_code=400)

    if not current_time:
        current_time = datetime.now(timezone.utc).isoformat()

    try:
        supabase = get_supabase_client()
        rows = fetch_active_goal_meta(supabase, user_id)
        user_goals = [row.get("meta") for row in rows if row.get("meta") is not None]
        system_prompt = build_goal_extraction_system_prompt(user_goals)

        content = call_azure_openai_chat(system_prompt, user_text, current_time)
        return func.HttpResponse(
            content,
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.exception("Error generating goal JSON")
        return func.HttpResponse(
            json.dumps({"success": False, "error": str(e)}, default=str),
            mimetype="application/json",
            status_code=500,
        )
