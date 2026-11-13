#!/usr/bin/env python3
"""Check that a built image retains application and dependency license notices.

This checks installed distribution files, not legal compatibility or a complete
native/OS dependency inventory. Run it in the image that will be distributed.
"""

import argparse
import base64
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re


PROJECT_NOTICES = ('LICENSE', 'licenses/README.md',
                   'licenses/ACKNOWLEDGEMENTS.txt', 'licenses/pyserial-3.5-LICENSE.txt')
NOTICE_NAME = re.compile(r'^(?:LICEN[CS]E|COPYING|NOTICE)(?:$|[._-])', re.IGNORECASE)
FALLBACKS = {('pyserial', '3.5'): 'licenses/pyserial-3.5-LICENSE.txt'}


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def locked_dependencies(paths):
    dependencies = {}
    for path in paths:
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith(('#', '--hash=', '--only-binary')):
                continue
            match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^\s;\\]+)(?:\s*\\)?', line)
            if not match:
                raise ValueError(f'Expected a pinned dependency in {path}: {line}')
            name, version = normalized(match[1]), match[2]
            if name in dependencies and dependencies[name] != version:
                raise ValueError(f'Conflicting locked versions: {name}')
            dependencies[name] = version
    if not dependencies:
        raise ValueError('No pinned dependencies found')
    return dependencies


def require_text(path):
    if not path.is_file() or not path.read_bytes().strip():
        raise ValueError(f'Missing or empty license notice: {path}')


def inspect_distribution(name, version, root, distribution=metadata.distribution):
    installed = distribution(name)
    if installed.version != version:
        raise ValueError(f'Installed version differs from lock: {name}')
    files = tuple(installed.files or ())
    notices = [path for path in files
               if any(part.endswith('.dist-info') for part in path.parts)
               and (NOTICE_NAME.match(path.name) or 'licenses' in path.parts)]
    for path in notices:
        location = Path(installed.locate_file(path))
        require_text(location)
        recorded_hash = getattr(path, 'hash', None)
        if recorded_hash:
            digest = hashlib.new(recorded_hash.mode, location.read_bytes()).digest()
            actual = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
            if actual != recorded_hash.value:
                raise ValueError(f'License text differs from installed RECORD: {name}: {path}')
    # PEP 639 declarations must point to retained files, even when another
    # license file happens to be present in the same wheel.
    declared = installed.metadata.get_all('License-File', ())
    for filename in declared:
        if not any(str(path).endswith('/' + filename) for path in notices):
            raise ValueError(f'Declared license file missing: {name}: {filename}')
    fallback = FALLBACKS.get((name, version))
    if fallback:
        require_text(root / fallback)
    if not notices and not fallback:
        raise ValueError(f'No retained license text for {name}=={version}')
    return {'name': name, 'version': version,
            'notices': [str(path) for path in notices], 'supplement': fallback}


def check(root, requirements, distribution=metadata.distribution):
    root = Path(root)
    for filename in PROJECT_NOTICES:
        require_text(root / filename)
    dependencies = locked_dependencies(requirements)
    return {'scope': 'application notices and installed Python distribution notices',
            'project_notices': list(PROJECT_NOTICES),
            'dependencies': [inspect_distribution(name, version, root, distribution)
                             for name, version in sorted(dependencies.items())]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/app'))
    parser.add_argument('--requirements', type=Path, action='append', required=True)
    args = parser.parse_args()
    try:
        result = check(args.root, args.requirements)
    except (OSError, ValueError, metadata.PackageNotFoundError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
