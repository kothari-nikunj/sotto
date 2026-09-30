"""Refresh Sotto skills from the immutable image copy on every container start.

The launcher replaces /root/.hermes with a volume symlink during boot. Railway
can restart the same container filesystem, so that path is not a safe seed.
"""
import filecmp
from pathlib import Path
import shutil
import sys


def _equal_tree(source, target):
    comparison = filecmp.dircmp(source, target)
    return not (comparison.left_only or comparison.right_only or comparison.funny_files) and all(
        filecmp.cmp(source / name, target / name, shallow=False) for name in comparison.common_files) and all(
        _equal_tree(source / name, target / name) for name in comparison.common_dirs)


def sync(home, source, bundle):
    home, source, bundle = Path(home), Path(source), Path(bundle)
    compose = source / '_shared/scripts/compose_brief.py'
    if (not compose.is_file() or compose.stat().st_size == 0
            or not bundle.is_file() or bundle.stat().st_size == 0):
        raise RuntimeError('Immutable Sotto skills or bundle are missing from image')
    skills = home / 'skills'
    bundles = home / 'skill-bundles'
    for directory in (skills, bundles):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise RuntimeError('Hermes skills path is not a real directory')
        directory.mkdir(parents=True, exist_ok=True)
    target = skills / 'sotto'
    if source.resolve() == target.resolve() or bundle.resolve() == (bundles / 'sotto.yaml').resolve():
        raise RuntimeError('Sotto skill source aliases the writable Hermes home')
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, symlinks=True)
    bundled = bundles / 'sotto.yaml'
    if bundled.is_symlink() or bundled.is_file():
        bundled.unlink()
    shutil.copy2(bundle, bundled)
    if not _equal_tree(source, target) or not filecmp.cmp(bundle, bundles / 'sotto.yaml', shallow=False):
        raise RuntimeError('Sotto skill initialization differs from immutable image')


if __name__ == '__main__':
    try:
        sync(*sys.argv[1:])
    except (OSError, RuntimeError, TypeError) as error:
        sys.exit('[sotto] skill initialization failed: ' + str(error))
