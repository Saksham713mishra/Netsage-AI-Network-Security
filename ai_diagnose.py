"""
ai_diagnose.py
--------------
Calls the Claude API with the structured prompt in
prompts/diagnose_prompt.md and gets back a JSON diagnosis for one case.

Needs ANTHROPIC_API_KEY set (see .env.example). Get a key from
https://console.anthropic.com/settings/keys - the free trial credits
are enough to run this for a class project.
"""

import os
import json
import re
from anthropic import Anthropic

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "diagnose_prompt.md")

# claude-sonnet-5 gives the best reasoning for connecting evidence to a root
# cause. If you're burning through free credits fast, swap this for
# "claude-haiku-4-5-20251001" - it's cheaper/faster and still fine for this
# kind of structured, evidence-grounded task.
MODEL = "claude-sonnet-5"


def _get_api_key():
    for key_name in ("ANTHROPIC_API_KEY", "\ufeffANTHROPIC_API_KEY"):
        value = os.environ.get(key_name)
        if value:
            if key_name.startswith("\ufeff"):
                os.environ["ANTHROPIC_API_KEY"] = value
            return value
    return None


def _load_system_prompt():
    """The markdown file has a 'System Prompt' section and worked examples.
    We pass the whole file as the system prompt - Claude reads markdown fine,
    and it keeps the prompt and its examples in exactly one place."""
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _strip_json_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def diagnose_case(symptom, topology_note, show_output, api_key=None):
    """
    Returns (parsed_dict, raw_response_text, model_used).
    parsed_dict is None if the model's response couldn't be parsed as JSON -
    in that case raw_response_text still gets saved so nothing is lost.
    """
    key = api_key or _get_api_key()
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )

    client = Anthropic(api_key=key)
    system_prompt = _load_system_prompt()

    user_message = (
        "Diagnose this case. Respond with ONLY the JSON object described in the system prompt.\n\n"
        f"Symptom:\n{symptom}\n\n"
        f"Topology note:\n{topology_note or '(none provided)'}\n\n"
        f"Show-command output:\n{show_output}"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    raw_text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )

    cleaned = _strip_json_fences(raw_text)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = None

    return parsed, raw_text, MODEL
