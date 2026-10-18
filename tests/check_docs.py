#!/usr/bin/env python3
"""Check repository-local Markdown links without reading generated/reference trees."""
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "target", "node_modules", "sources", "test-results", "playwright-report", "__pycache__"}


def main():
    errors = []
    checked = 0
    for directory, children, files in os.walk(ROOT):
        children[:] = [name for name in children if name not in SKIP]
        for name in files:
            if not name.endswith('.md'):
                continue
            path = Path(directory) / name
            text = path.read_text()
            checked += 1
            # Examples inside code fences are not actual document navigation.
            for index, section in enumerate(re.split(r'^\s*```.*$', text, flags=re.MULTILINE)):
                if index % 2:
                    continue
                for match in re.finditer(r'\[[^\]\n]*\]\(([^)\n]+)\)', section):
                    target = match.group(1).strip()
                    target = target[1:target.index('>')] if target.startswith('<') else target.split()[0]
                    url = urlsplit(target)
                    if url.scheme or url.netloc or not url.path:
                        continue
                    destination = path.parent / unquote(url.path)
                    if not destination.exists():
                        errors.append(f'{path.relative_to(ROOT)}: missing link target {target}')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print(f'PASS: local links in {checked} Markdown documents (generated/reference trees excluded)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
