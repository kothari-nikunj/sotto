"""Configured public-research providers and capability order; no execution or credential reads."""
KEY_PROVIDERS = {
    "exa": {"label": "Exa", "env": "EXA_API_KEY",
            "does": "web search, and deep research when Parallel isn't set"},
    "parallel": {"label": "Parallel", "env": "PARALLEL_API_KEY",
                 "does": "deep research (first choice when set)"},
    "browseruse": {"label": "Browser Use Cloud", "env": "BROWSER_USE_API_KEY",
                   "does": "a real hosted browser for JS-heavy link reads — their cloud sees the "
                           "page; read-only, never your logins"},
    "gemini": {"label": "Gemini grounding", "env": "GOOGLE_AI_API_KEY",
               "does": "the built-in fallback for the others — already set if briefs work"},
}

KEY_ENV = {provider: metadata["env"] for provider, metadata in KEY_PROVIDERS.items()}

# Existing precedence; hosted browsing is the final escalation for crawler failures.
CAPABILITIES = {
    "web_search": ("exa", "gemini"),
    "deep_research": ("parallel", "exa", "gemini"),
    "fetch_url": ("exa", "gemini", "browseruse"),
}

