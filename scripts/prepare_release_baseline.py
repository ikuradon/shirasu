#!/usr/bin/env python3
"""Export the published image allowlist from a Git commit for local comparison."""

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = 'legacy-manifest.json'
# Routing changes require an explicit opt-in and separate runtime comparisons.
# Every renderer, asset and dependency lock remains byte-identical.
PROFILE_FILES = {'image_protocol.py', 'image_service/app.py', 'image_versions.py'}


def _same_logic(name, old, new):
    if name == 'image_versions.py':
        # The candidate builder separately pins the complete generated profile.
        return True
    moved = {'image_protocol.py': 'SUPPORTED_VERSIONS', 'image_service/app.py': 'RENDERERS'}[name]
    def logic(data):
        tree = ast.parse(data)
        tree.body = [node for node in tree.body
                     if not (isinstance(node, ast.ImportFrom) and node.module == 'image_versions')
                     and not (isinstance(node, ast.Assign) and len(node.targets) == 1
                              and isinstance(node.targets[0], ast.Name) and node.targets[0].id == moved)]
        return ast.dump(tree, include_attributes=False)
    return logic(old) == logic(new)


def _git(*args):
    return subprocess.run(['git', '--no-optional-locks', '-C', str(ROOT), *args],
                          check=True, capture_output=True, timeout=30).stdout


def _allowed(name):
    return (isinstance(name, str) and not PurePosixPath(name).is_absolute()
            and re.fullmatch(r'[A-Za-z0-9_./-]+', name) is not None
            and all(part not in ('', '.', '..') for part in name.split('/'))
            and (name in ('image_protocol.py', 'image_versions.py', 'licenses/pyserial-3.5-LICENSE.txt')
                 or name.startswith('image_service/')))


def prepare(destination, *, ref='HEAD'):
    """Copy only committed public renderer inputs; never execute old build code."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Legacy reference destination already exists')
    commit = _git('rev-parse', '--verify', '--end-of-options', ref + '^{commit}').decode().strip()
    if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit) is None:
        raise ValueError('Invalid legacy commit')
    tree = ast.parse(_git('show', commit + ':scripts/prepare_image_service.py').decode())
    declarations = [node.value for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'FILES' for target in node.targets)]
    if len(declarations) != 1:
        raise ValueError('Legacy public file allowlist is missing or ambiguous')
    names = ast.literal_eval(declarations[0])
    if (not isinstance(names, tuple) or not names or len(names) > 200
            or not all(_allowed(name) for name in names) or len(set(names)) != len(names)):
        raise ValueError('Invalid legacy public file allowlist')
    sources = {name: name for name in names}
    sources['requirements.txt'] = 'image_service/requirements.txt'
    entries = {}
    for target, source in sources.items():
        mode = _git('ls-tree', commit, '--', source).decode().split(' ', 1)[0]
        if mode not in ('100644', '100755'):
            raise ValueError('Legacy reference is not a regular tracked file: ' + source)
        entries[target] = _git('show', commit + ':' + source)
    destination.mkdir(parents=True, exist_ok=False)
    for name, data in entries.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {'schema_version': 1, 'commit': commit,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}}
    (destination / MANIFEST).write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')
    return destination


def compare_sources(baseline, candidate_images, *, allow_routing_changes=False):
    """Verify the reference and all frozen code/assets against the candidate."""
    baseline, candidate_images = Path(baseline), Path(candidate_images)
    manifest = json.loads((baseline / MANIFEST).read_text())
    files = manifest['files']
    if (manifest.get('schema_version') != 1 or not isinstance(files, dict)
            or not all(name == 'requirements.txt' or _allowed(name) for name in files)):
        raise ValueError('Invalid legacy reference manifest')
    actual = {str(path.relative_to(baseline)) for path in baseline.rglob('*') if path.is_file()}
    if actual != set(files) | {MANIFEST} or any(p.is_symlink() for p in baseline.rglob('*')):
        raise ValueError('Legacy reference contents differ from its manifest')
    unchanged, profile_changes, routing_changes = [], [], []
    for name, digest in files.items():
        old = hashlib.sha256((baseline / name).read_bytes()).hexdigest()
        if old != digest:
            raise ValueError('Legacy reference file was modified: ' + name)
        current = candidate_images / name
        if not current.is_file() or current.is_symlink():
            raise ValueError('Candidate is missing a legacy file: ' + name)
        if hashlib.sha256(current.read_bytes()).hexdigest() == old:
            unchanged.append(name)
        elif name in PROFILE_FILES and _same_logic(name, (baseline / name).read_bytes(), current.read_bytes()):
            profile_changes.append(name)
        elif allow_routing_changes and name in PROFILE_FILES:
            routing_changes.append(name)
        else:
            raise ValueError('Frozen renderer, asset or dependency changed: ' + name)
    return {'commit': manifest['commit'], 'unchanged_files': sorted(unchanged),
            'profile_changes_verified': sorted(profile_changes),
            'routing_changes_require_runtime_checks': sorted(routing_changes)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--ref', default='HEAD', help='committed reference, never a worktree')
    parser.add_argument('--candidate-images', type=Path,
                        help='compare an existing reference with this candidate image package')
    parser.add_argument('--allow-routing-changes', action='store_true',
                        help='report protocol/HTTP changes separately; requires legacy HTTP regression checks')
    args = parser.parse_args(argv)
    try:
        if args.candidate_images:
            result = compare_sources(args.destination, args.candidate_images,
                                     allow_routing_changes=args.allow_routing_changes)
        else:
            prepare(args.destination, ref=args.ref)
            result = json.loads((args.destination / MANIFEST).read_text())
            result = {'baseline': str(args.destination.absolute()), 'commit': result['commit'],
                      'files': len(result['files'])}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print('Legacy reference failed: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
