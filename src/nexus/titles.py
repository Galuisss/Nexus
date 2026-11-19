"""Short task titles generated through Codex's ephemeral structured turns."""

import json

TITLE_MAX_CHARS = 36
TITLE_MODEL = "gpt-5.6-luna"
TITLE_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string", "minLength": 1, "maxLength": TITLE_MAX_CHARS}},
    "required": ["title"],
    "additionalProperties": False,
}


def title_prompt(text: str) -> str:
    prefix = (
        f"Generate a concise, single-line task title of at most {TITLE_MAX_CHARS} characters "
        "and under five words where possible. Start with an imperative verb. "
        "Write in the user's language. Preserve ticket references and code terms. "
        "Do not use quotes, markdown, or trailing punctuation. Do not answer the request."
        "\n\nUser prompt:\n"
    )
    excerpt = text.strip().encode()[: 960 - len(prefix.encode())].decode(errors="ignore")
    return prefix + excerpt


def parse_title(response: str) -> str:
    title = json.loads(response).get("title")
    if not isinstance(title, str):
        raise ValueError("Codex returned an empty task title")
    title = " ".join(title.strip().strip("\"'`“”‘’").split()).rstrip(".?!。？！")[:TITLE_MAX_CHARS]
    if not title:
        raise ValueError("Codex returned an empty task title")
    return title


def title_config(mcp_servers: dict) -> dict:
    # Match the CLI's temporary structured request: no tools, skills, or hooks.
    features = (
        "apps",
        "code_mode",
        "code_mode_only",
        "context_management",
        "current_time_reminder",
        "deferred_executor",
        "goals",
        "hooks",
        "image_generation",
        "memories",
        "multi_agent",
        "multi_agent_v2",
        "plugins",
        "request_permissions_tool",
        "shell_snapshot",
        "shell_tool",
        "standalone_web_search",
        "token_budget",
        "tool_suggest",
        "unified_exec",
        "view_image",
    )
    return {
        **{f"features.{feature}": False for feature in features},
        "cloud.skills.enabled": False,
        "skills.include_instructions": False,
        "tools.experimental_request_user_input.enabled": False,
        "tools.update_plan.enabled": False,
        "web_search": "disabled",
        "default_permissions": ":read-only",
        "mcp_servers": {name: {"enabled": False} for name in mcp_servers},
    }
