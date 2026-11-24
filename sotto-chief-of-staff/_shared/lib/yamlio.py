"""The one YAML reader for ledger and person frontmatter: libyaml's safe loader when PyYAML
was built with it (4-7x faster), the pure-Python safe loader otherwise. Every host gets the
pure-Python answer: libyaml reads a tab as separation and nests deeper than Python recursion
allows, so text with a tab, or a result nested past MAX_FAST_DEPTH, is parsed by the pure loader.
tests/test_yamlio.py holds the two to the same objects and the same rejections."""
import yaml

SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
MAX_FAST_DEPTH = 64


def _nested_past(value, limit) -> bool:
    stack, seen = [(value, 1)], set()
    while stack:
        node, depth = stack.pop()
        if not isinstance(node, (dict, list, tuple, set)) or id(node) in seen:
            continue
        if depth > limit:
            return True
        seen.add(id(node))
        children = [*node, *node.values()] if isinstance(node, dict) else node
        stack.extend((child, depth + 1) for child in children)
    return False


def safe_load(text, variant=None):
    """Parse one document. `variant(base)` may return a stricter subclass of the safe loader."""
    if SAFE_LOADER is not yaml.SafeLoader and "\t" not in text:
        loader = variant(SAFE_LOADER) if variant else SAFE_LOADER
        value = yaml.load(text, Loader=loader)  # noqa: S506 - always a safe loader
        if not _nested_past(value, MAX_FAST_DEPTH):
            return value
    loader = variant(yaml.SafeLoader) if variant else yaml.SafeLoader
    return yaml.load(text, Loader=loader)  # noqa: S506 - always a safe loader
