"""Real installer, temporary home, no changes to installed agent configuration."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='ccx-skill-') as temporary:
    home = Path(temporary) / '.claude' / 'operator-home'
    command = [sys.executable, str(repo / 'scripts/install-skill.py'), 'both', '--home', str(home)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    claude, codex = [home / directory / 'skills/ccx' for directory in ('.claude', '.agents')]
    assert (claude / 'SKILL.md').read_bytes() == (codex / 'SKILL.md').read_bytes()
    assert not (claude / 'agents').exists() and (codex / 'agents/openai.yaml').is_file()
    assert Path(json.loads((codex / 'installation.json').read_text())['cli']).is_file()
    for destination in (claude, codex):
        (destination / 'references/local.md').write_text('Operator settings', encoding='utf-8')
        (destination / 'references/extra.md').write_text('Operator reference', encoding='utf-8')
        (destination / 'references/retired.md').write_text('Old bundled reference', encoding='utf-8')
        metadata = destination / 'installation.json'
        previous = json.loads(metadata.read_text())
        previous['files'].append('references/retired.md')
        metadata.write_text(json.dumps(previous), encoding='utf-8')
        (destination / 'SKILL.md').write_text('Customized entry', encoding='utf-8')
    assert subprocess.run(command, capture_output=True).returncode == 2
    assert (claude / 'SKILL.md').read_text() == 'Customized entry'
    assert subprocess.run(command + ['--force'], capture_output=True).returncode == 0
    for destination in (claude, codex):
        assert (destination / 'SKILL.md').read_bytes() == (repo / 'skills/ccx/SKILL.md').read_bytes()
        assert (destination / 'references/local.md').read_text() == 'Operator settings'
        assert (destination / 'references/extra.md').read_text() == 'Operator reference'
        assert not (destination / 'references/retired.md').exists()
print('PASS: dual installation, refusal without force, update and private reference preservation')
