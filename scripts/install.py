"""Install the client and local agent integration; never copy credentials."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def codex_binary():
    candidates = [Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'),
                  Path('/Applications/Codex.app/Contents/Resources/codex'),
                  Path(shutil.which('codex') or '/nonexistent')]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            result = subprocess.run([str(candidate), '--version'], capture_output=True, timeout=10)
            if not result.returncode:
                return str(candidate)
    return None


def timeout_setting(path):
    text = path.read_text()
    pattern = r'(?m)^\[mcp_servers\.(?:"atelier-vps"|atelier-vps)\]\s*\n'
    match = re.search(pattern, text)
    if not match:
        raise RuntimeError('MCP registration did not create the expected server block')
    end = re.search(r'(?m)^\[', text[match.end():])
    stop = match.end() + end.start() if end else len(text)
    section = text[match.end():stop]
    section = re.sub(r'(?m)^tool_timeout_sec\s*=.*\n?', '', section)
    new = text[:match.end()] + 'tool_timeout_sec = 3700\n' + section + text[stop:]
    temporary = path.with_name('.atelier-vps-config.tmp')
    with temporary.open('x') as out:
        os.chmod(temporary, path.stat().st_mode & 0o777)
        out.write(new)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='dev-vps', help='existing restricted SSH alias')
    parser.add_argument('--coolify-url', default='', help='optional personal dashboard URL')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', args.host):
        parser.error('use a configured SSH alias')
    config = Path.home() / '.config/atelier-vps/config.json'
    config.parent.mkdir(parents=True, exist_ok=True)
    value = json.loads(config.read_text()) if config.exists() else {}
    value.update(ssh_host=args.host)
    if args.coolify_url:
        if not args.coolify_url.startswith('https://'):
            parser.error('dashboard URL must use HTTPS')
        value['coolify_url'] = args.coolify_url
    config.write_text(json.dumps(value, indent=2) + '\n')
    config.chmod(0o600)
    binary = Path.home() / '.local/bin/atelier-vps'
    binary.parent.mkdir(parents=True, exist_ok=True)
    if binary.is_symlink():
        raise RuntimeError('refusing to overwrite an existing launcher symlink')
    binary.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(ROOT / 'vps.py')) + ' "$@"\n')
    binary.chmod(0o755)
    uv = shutil.which('uv')
    codex = codex_binary()
    registered = False
    if codex and uv:
        subprocess.run([uv, 'sync', '--frozen', '--project', str(ROOT), '--no-dev'], check=True)
        subprocess.run([codex, 'mcp', 'add', 'atelier-vps', '--', uv, 'run', '--frozen',
                        '--project', str(ROOT), 'python', str(ROOT / 'mcp_server.py')], check=True)
        timeout_setting(Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'config.toml')
        registered = True
    skill = Path.home() / '.agents/skills/atelier-vps/SKILL.md'
    skill.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / 'skills/atelier-vps/SKILL.md', skill)
    print(json.dumps({'client': str(binary), 'mcp_registered': registered, 'skill': str(skill),
                      'next': 'Restart the agent host to discover the new MCP tools. Verify atelier-vps status.'}))


if __name__ == '__main__':
    main()
