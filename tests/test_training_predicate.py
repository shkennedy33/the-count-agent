"""Adversarial tests for is_safe_kgraph_invocation."""
import pytest

from training_predicate import is_safe_kgraph_invocation


# (cmd_string, expected_safe)
SAFE_CASES = [
    "python ~/.count/tools/kgraph.py stats",
    "python ~/.count/tools/kgraph.py search Blavatsky",
    'python ~/.count/tools/kgraph.py similar "Saint Germain Theosophy" --k 5',
    "python /home/shken/.count/tools/kgraph.py similar concept --type Concept",
    "python3 ~/.count/tools/kgraph.py node 'I AM Activity'",
    "py ~/.count/tools/kgraph.py facts theosophy",
    "python C:/Users/shken/.count/tools/kgraph.py random --type Person",
    "python ~/.count/tools/kgraph.py neighbors 'node name' --depth 2",
    "python ~/.count/tools/kgraph.py path Blavatsky 'MK-ULTRA'",
]

ADVERSARIAL_CASES = [
    # cypher subcommand denied
    'python ~/.count/tools/kgraph.py cypher "MATCH (n) RETURN n"',
    'python ~/.count/tools/kgraph.py cypher "MATCH (n) DETACH DELETE n"',
    # shell metacharacters
    "python ~/.count/tools/kgraph.py stats && rm -rf /",
    "python ~/.count/tools/kgraph.py stats; ls",
    "python ~/.count/tools/kgraph.py stats | cat /etc/passwd",
    "python ~/.count/tools/kgraph.py stats || true",
    "python ~/.count/tools/kgraph.py stats > /tmp/out",
    "python ~/.count/tools/kgraph.py stats < /etc/passwd",
    'python ~/.count/tools/kgraph.py stats `id`',
    'python ~/.count/tools/kgraph.py stats $(id)',
    # wrong interpreter
    "bash ~/.count/tools/kgraph.py stats",
    "sh -c 'python ~/.count/tools/kgraph.py stats'",
    # wrong script
    "python ~/.count/tools/evil.py stats",
    "python -c 'import os; os.system(\"id\")'",
    "python -m something stats",
    # unknown subcommand
    "python ~/.count/tools/kgraph.py exec something",
    "python ~/.count/tools/kgraph.py write something",
    # empty / malformed
    "",
    "python",
    "python ~/.count/tools/kgraph.py",
    # quote mismatch / shlex failure
    'python ~/.count/tools/kgraph.py search "unclosed',
]


@pytest.mark.parametrize("cmd", SAFE_CASES)
def test_safe_commands_allowed(cmd):
    assert is_safe_kgraph_invocation(cmd) is True, f"should be safe: {cmd!r}"


@pytest.mark.parametrize("cmd", ADVERSARIAL_CASES)
def test_adversarial_commands_denied(cmd):
    assert is_safe_kgraph_invocation(cmd) is False, f"should be denied: {cmd!r}"
