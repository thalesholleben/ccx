#!/usr/bin/env python3
"""Install the portable CCX skill without overwriting operator references."""
import argparse
import json
import shutil
from pathlib import Path


def install(target, home, force=False):
    repo = Path(__file__).resolve().parents[1]
    source = repo / 'skills' / 'ccx'
    destinations = [home / directory / 'skills' / 'ccx' for directory in
                    (('.claude', '.agents') if target == 'both' else
                     ('.claude',) if target == 'claude-code' else ('.agents',))]
    # Preflight both destinations before making any changes.
    if not force and any(path.exists() for path in destinations):
        raise FileExistsError('CCX skill already installed; use --force to update bundled files.')
    for destination in destinations:
        metadata = destination / 'installation.json'
        previous = json.loads(metadata.read_text(encoding='utf-8')) if metadata.is_file() else {}
        bundled = []
        for original in source.rglob('*'):
            relative = original.relative_to(source)
            if not original.is_file() or (destination.parents[1].name == '.claude' and relative.parts[0] == 'agents'):
                continue
            if relative.name in ('installation.json','transport.json') or '__pycache__' in relative.parts or original.suffix=='.pyc' or relative == Path('references/local.md'):
                continue
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, output)
            bundled.append(relative.as_posix())
        # Only prune files previously owned by this installer, never operator files.
        for relative in set(previous.get('files', [])) - set(bundled):
            obsolete = (destination / relative).resolve()
            if (obsolete.is_relative_to(destination.resolve()) and obsolete.is_file()
                    and obsolete != metadata.resolve()
                    and obsolete != (destination / 'references/local.md').resolve()):
                obsolete.unlink()
        metadata.write_text(json.dumps({'repo': str(repo), 'cli': str(repo / 'ccx-fleet.py'),
                                       'files': bundled}, indent=2) + '\n', encoding='utf-8')
    return destinations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', choices=('claude-code', 'codex', 'both'))
    parser.add_argument('--home', type=Path, default=Path.home(), help='Alternative installation home')
    parser.add_argument('--force', action='store_true', help='Update bundled files; preserve local.md')
    args = parser.parse_args()
    try:
        for path in install(args.target, args.home.resolve(), args.force):
            print(path)
    except FileExistsError as exc:
        parser.exit(2, str(exc) + '\n')


if __name__ == '__main__':
    main()
