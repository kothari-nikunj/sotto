"""Load installed canonical writers from immutable code paths, never tenant data."""
import importlib.util
from functools import partial
from pathlib import Path
import sys


class UnavailableError(Exception):
    pass


def _script(name, *, anchor):
    here = Path(anchor).resolve()
    candidates = (here.parents[2] / 'sotto-chief-of-staff' / '_shared' / 'scripts' / name,
                  Path('/app/sotto-skills/_shared/scripts') / name)
    for path in candidates:
        if path.is_file():
            library = str(path.parent.parent / 'lib')
            if library not in sys.path:
                sys.path.insert(0, library)
            spec = importlib.util.spec_from_file_location('sotto_installed_' + path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise UnavailableError('installed writer unavailable')


def preferences(*, anchor):
    return _script('preferences.py', anchor=anchor)


def google_action(*, anchor):
    return _script('google_action.py', anchor=anchor)


def preferences_for(anchor):
    return partial(preferences, anchor=anchor)
